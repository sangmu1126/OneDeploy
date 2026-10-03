import io
import json
import tempfile
import time
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.postgres import PostgresRequest
from onedeploy.server import App, handler_for, postgres_request_from_job


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
GROUP = 'sg-33333333'


def archive(include_migration=True, database=True):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as bundle:
        bundle.writestr('package.json', json.dumps({
            'scripts': {'start': 'node server.js'},
            'dependencies': {'pg': '8.23.0'} if database else {}}))
        bundle.writestr('server.js', 'const {Pool} = require("pg");' if database
                        else 'require("node:http").createServer().listen(3000);')
        if include_migration:
            bundle.writestr('migrations/0001_init.sql', 'CREATE TABLE demo (id int);')
    return content.getvalue()


class PostgresServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    service_security_group=GROUP)
        self.app = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0,
                       agent_factory=lambda _: object())

    def test_retirement_plan_is_authenticated_and_read_only(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/retirement/plan'
        handler.headers = {'X-OneDeploy-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.postgres_retirement_operations, 'plan',
                          return_value={'plan_id': 'token', 'database_id': 'onedeploy-demo-app'}) as planned:
            handler.do_POST()
        planned.assert_called_once_with('demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.headers = {'Content-Length': '0'}
        handler.json_response.reset_mock()
        with patch.object(self.app.postgres_retirement_operations, 'plan') as planned:
            handler.do_POST()
        planned.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_failed_create_cleanup_routes_require_auth_and_exact_confirmation(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/failed-create/plan'
        handler.headers = {'X-OneDeploy-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.postgres_operations, 'cleanup_plan',
                          return_value={'plan_id': 'a' * 32}) as planned:
            handler.do_POST()
        planned.assert_called_once_with('demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.path = '/api/applications/demo-app/postgres/failed-create/start'
        payload = json.dumps({'plan_id': 'a' * 32,
            'confirm_stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/onedeploy-db-demo-app/id'}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.postgres_operations, 'cleanup_start',
                          return_value={'status': 'recovering'}) as started:
            handler.do_POST()
        started.assert_called_once_with('demo-app', 'a' * 32,
            'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/onedeploy-db-demo-app/id')
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        handler.headers = {'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.postgres_operations, 'cleanup_start') as started:
            handler.do_POST()
        started.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_retirement_start_requires_plan_and_exact_database_id(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/retirement/start'
        payload = json.dumps({'plan_id': 'a' * 32,
                              'confirm_database_id': 'onedeploy-demo-app'}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch.object(self.app.postgres_retirement_operations, 'start',
                          return_value={'status': 'running'}) as started:
            handler.do_POST()
        started.assert_called_once_with('demo-app', 'a' * 32, 'onedeploy-demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 202)

    def test_retirement_record_blocks_new_aws_deployment(self):
        with patch.object(self.app.postgres_retirement_operations,
                          'blocks_deployment', return_value=True):
            with self.assertRaisesRegex(ValueError, '폐기 기록'):
                self.app.ensure_application_available('demo-app', 'aws-ecs-express')

    def upload(self, content, *, postgres=True, target='aws-ecs-express', public=True,
               network_headers=True, partial_network=False, create_plan_id=None):
        handler_class = handler_for(self.app)
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/deployments'
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'X-Deploy-Target': target,
                           'X-Public-Access': str(public).lower(), 'X-Application-Id': 'demo-app',
                           'Content-Length': str(len(content))}
        if postgres:
            handler.headers['X-Postgres-Existing'] = 'true'
            if network_headers:
                handler.headers['X-Postgres-Vpc-Id'] = 'vpc-12345678'
                if not partial_network:
                    handler.headers['X-Postgres-Subnet-Ids'] = 'subnet-11111111,subnet-22222222'
        if create_plan_id is not None:
            handler.headers['X-Postgres-Create-Plan'] = create_plan_id
        handler.rfile = io.BytesIO(content)
        handler.json_response = Mock()
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None), \
                patch('onedeploy.server.threading.Thread.start'):
            handler.do_POST()
        return handler.json_response.call_args.args

    def test_lookup_existing_database_returns_only_verified_network(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres'
        handler.headers = {'X-OneDeploy-Token': self.app.token}
        handler.json_response = Mock()
        database = {'database_id': 'onedeploy-demo-app', 'account': ACCOUNT,
                    'region': REGION, 'vpc_id': 'vpc-12345678',
                    'subnet_ids': ['subnet-11111111', 'subnet-22222222'],
                    'status': 'available'}
        with patch('onedeploy.server.discover_existing_postgres', return_value=database) as discover:
            handler.do_GET()
        discover.assert_called_once_with('demo-app', self.settings)
        self.assertEqual(handler.json_response.call_args.args, (200, database))

    def test_lookup_requires_session_token(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres'
        handler.headers = {}
        handler.json_response = Mock()
        with patch('onedeploy.server.discover_existing_postgres') as discover:
            handler.do_GET()
        discover.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_backup_status_requires_session_and_returns_verified_summary(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/backups'
        handler.headers = {}
        handler.json_response = Mock()
        with patch('onedeploy.server.inspect_postgres_backup_status') as inspect:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        inspect.assert_not_called()
        handler.headers = {'X-OneDeploy-Token': self.app.token}
        summary = {'database_id': 'onedeploy-demo-app', 'backup_retention_days': 7,
                   'manual_snapshot_count': 0, 'manual_snapshots': []}
        with patch('onedeploy.server.inspect_postgres_backup_status',
                   return_value=summary) as inspect:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args, (200, summary))
        inspect.assert_called_once_with('demo-app', self.settings)

    def test_manual_snapshot_plan_create_and_reconcile_routes(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.json_response = Mock()
        handler.path = '/api/applications/demo-app/snapshots/plan'
        payload = json.dumps({'snapshot_id': 'onedeploy-demo-app-before-migration'}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.snapshot_operations, 'plan',
                          return_value={'plan_id': 'a' * 32}) as plan:
            handler.do_POST()
        plan.assert_called_once_with('demo-app', 'onedeploy-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

        handler.path = '/api/applications/demo-app/snapshots/create'
        payload = json.dumps({'plan_id': 'a' * 32}).encode()
        handler.headers['Content-Length'] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.snapshot_operations, 'start',
                          return_value={'status': 'running'}) as start:
            handler.do_POST()
        start.assert_called_once_with('demo-app', 'a' * 32)
        self.assertEqual(handler.json_response.call_args.args[0], 202)

        handler.path = '/api/applications/demo-app/snapshots/onedeploy-demo-app-before-migration/reconcile'
        handler.headers['Content-Length'] = '0'
        with patch.object(self.app.snapshot_operations, 'reconcile',
                          return_value={'status': 'succeeded'}) as reconcile:
            handler.do_POST()
        reconcile.assert_called_once_with('demo-app', 'onedeploy-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

        handler.path = '/api/applications/demo-app/snapshots/onedeploy-demo-app-before-migration/operation'
        with patch.object(self.app.snapshot_operations, 'get',
                          return_value={'status': 'succeeded'}) as get:
            handler.do_GET()
        get.assert_called_once_with('demo-app', 'onedeploy-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

    def test_manual_snapshot_create_requires_session(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/snapshots/create'
        handler.headers = {'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.snapshot_operations, 'start') as start:
            handler.do_POST()
        start.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_creation_plan_runs_read_only_preflight(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-12345678',
                              'subnet_ids': ['subnet-11111111', 'subnet-22222222']}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        plan = {'account': ACCOUNT, 'pricing': {'baseline_730h_usd': '20.87'}}
        with patch('onedeploy.server.AwsPostgresProvisioner.preflight',
                   return_value=plan) as preflight, \
                patch('onedeploy.server.AwsPostgresProvisioner.assert_stack_available'), \
                patch('onedeploy.server.AwsPostgresProvisioner.create') as create:
            handler.do_POST()
        status, result = handler.json_response.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['pricing'], plan['pricing'])
        self.assertTrue(result['plan_id'])
        preflight.assert_called_once_with()
        create.assert_not_called()

    def test_plan_resolves_verified_application_network_without_global_group(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        self.app.postgres_operations.settings = self.app.aws_settings
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-12345678',
                              'subnet_ids': ['subnet-11111111', 'subnet-22222222']}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch('onedeploy.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   return_value={'service_security_group': GROUP}) as network, \
                patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                      return_value={'account': ACCOUNT}) as preflight, \
                patch('onedeploy.postgres_operations.AwsPostgresProvisioner.assert_stack_available'):
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        self.assertEqual(preflight.call_args.args, ())
        self.assertEqual(self.app.postgres_operations.plans['demo-app']['request'].service_security_group, GROUP)
        self.assertEqual(network.call_count, 1)

    def test_upload_uses_verified_application_network_in_persisted_aws_settings(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('onedeploy.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   return_value={'service_security_group': GROUP}), \
                patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                      return_value={'database_id': 'onedeploy-demo-app'}):
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['aws']['service_security_group'], GROUP)
        self.assertEqual(postgres_request_from_job(job).service_security_group, GROUP)
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertFalse(restored.recovery_warnings)
        self.assertEqual(restored.jobs[payload['id']]['aws']['service_security_group'], GROUP)

    def test_upload_rejects_missing_application_network_before_job(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('onedeploy.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   side_effect=RuntimeError('앱 네트워크 스택 없음')):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('앱 네트워크 스택 없음', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_creation_requires_preview_then_journals_async_operation(self):
        plan = {'account': ACCOUNT, 'pricing': {'baseline_730h_usd': '20.87'}}
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
            ('subnet-11111111', 'subnet-22222222'), GROUP)
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=plan), \
                patch('onedeploy.postgres_operations.AwsPostgresProvisioner.assert_stack_available'), \
                patch('onedeploy.postgres_operations.threading.Thread.start'):
            planned = self.app.postgres_operations.plan(request)
            payload = json.dumps({'plan_id': planned['plan_id']}).encode()
            handler = handler_for(self.app).__new__(handler_for(self.app))
            handler.path = '/api/applications/demo-app/postgres/create'
            handler.headers = {'X-OneDeploy-Token': self.app.token,
                               'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            handler.do_POST()
        status, operation = handler.json_response.call_args.args
        self.assertEqual(status, 202)
        self.assertEqual(operation['status'], 'running')
        self.assertTrue((self.root / 'database-operations' / 'demo-app.json').is_file())
        handler.path = '/api/applications/demo-app/postgres/operation'
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[1]['status'], 'running')

    def test_creation_plan_rejects_invalid_network_before_aws(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-invalid', 'subnet_ids': []}).encode()
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch('onedeploy.server.AwsPostgresProvisioner.preflight') as preflight:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        preflight.assert_not_called()

    def reviewed_create_plan(self):
        token = 'a' * 32
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
            ('subnet-11111111', 'subnet-22222222'), GROUP)
        self.app.postgres_operations.plans['demo-app'] = {
            'id': token, 'request': request, 'result': {}, 'expires': time.monotonic() + 900}
        return token

    def created_operation(self, status, database_id=None):
        request = self.app.postgres_operations.plans['demo-app']['request']
        return {'application_id': 'demo-app', 'status': status,
                'creation_id': 'a' * 16,
                'request': asdict(request), 'expected_plan': {
                    'pricing': {'baseline_730h_usd': '20.87'}},
                'database_id': database_id, 'created_at': '2026-10-03T00:00:00+00:00',
                'message': 'test operation'}

    def test_reviewed_rds_plan_upload_creates_one_provisioning_job(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}) as start, \
                patch('onedeploy.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'provisioning'))
        start.assert_called_once_with('demo-app', token)
        inspect.assert_not_called()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['infrastructure_plan']['database'],
                         {'binding': 'create', 'database_id': 'onedeploy-demo-app'})
        self.assertEqual(job['status'], 'provisioning')
        self.assertEqual(job['postgres_creation_id'], 'a' * 16)
        with self.assertRaisesRegex(ValueError, '이미 진행 중'):
            self.app.ensure_application_available('demo-app', 'aws-ecs-express')

    def test_reviewed_rds_upload_validates_source_before_creation(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start') as start:
            status, payload = self.upload(archive(include_migration=False),
                                          postgres=False, create_plan_id=token)
        self.assertEqual(status, 400)
        self.assertIn('migrations/', payload['error'])
        start.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_changed_rds_plan_fails_before_creation_without_agent_start(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          side_effect=ValueError('생성 계획이 변경됐습니다.')) as start, \
                patch.object(self.app, 'run_agent') as deploy:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'failed'))
        start.assert_called_once_with('demo-app', token)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'failed')
        self.assertIn('가격 계획을 다시 확인', self.app.jobs[payload['id']]['events'][-1]['message'])
        self.assertEqual(self.app.jobs[payload['id']]['events'][-1]['stage'], 'database_plan_rejected')

    def test_recorded_creation_start_failure_stays_interrupted(self):
        token = self.reviewed_create_plan()
        def record_then_fail(*_args):
            self.app.postgres_operations.operations['demo-app'] = self.created_operation('running')
            raise RuntimeError('worker start failed')
        with patch.object(self.app.postgres_operations, 'start', side_effect=record_then_fail), \
                patch.object(self.app, 'run_agent') as deploy:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'interrupted'))
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['events'][-1]['stage'], 'database_attention')

    def test_create_plan_rejects_existing_database_selection(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start') as start:
            status, payload = self.upload(archive(), create_plan_id=token)
        self.assertEqual(status, 400)
        self.assertIn('동시에', payload['error'])
        start.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_creation_result_controls_same_job_deployment(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'onedeploy-demo-app')
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'onedeploy-demo-app'}), \
                patch('onedeploy.server.DeploymentAgent.run',
                      return_value={'url': 'https://example.test', 'target': 'aws-ecs-express'}) as deploy:
            self.app.run_postgres_then_agent(job_id)
        deploy.assert_called_once_with()
        self.assertEqual(self.app.jobs[job_id]['status'], 'succeeded')
        self.assertEqual(self.app.jobs[job_id]['result']['url'], 'https://example.test')
        self.assertIn('database_ready', [event['stage'] for event in self.app.jobs[job_id]['events']])

    def test_provisioning_waits_for_confirmed_database_before_agent(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        operation = self.created_operation('running')
        self.app.postgres_operations.operations['demo-app'] = operation
        def finish_create(_seconds):
            operation['status'] = 'succeeded'
            operation['database_id'] = 'onedeploy-demo-app'
        with patch('onedeploy.server.time.sleep', side_effect=finish_create) as sleep, \
                patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                      return_value={'database_id': 'onedeploy-demo-app'}), \
                patch('onedeploy.server.DeploymentAgent.run',
                      return_value={'url': 'https://example.test', 'target': 'aws-ecs-express'}):
            self.app.run_postgres_then_agent(payload['id'])
        sleep.assert_called_once_with(3)
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'succeeded')

    def test_old_job_does_not_deploy_from_a_later_creation_attempt(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        operation = self.created_operation('succeeded', 'onedeploy-demo-app')
        operation['creation_id'] = 'b' * 16
        self.app.postgres_operations.operations['demo-app'] = operation
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current') as inspect, \
                patch.object(self.app, 'run_agent') as deploy:
            self.app.run_postgres_then_agent(payload['id'])
        inspect.assert_not_called()
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'interrupted')
        self.assertIn('다릅니다', self.app.jobs[payload['id']]['events'][-1]['message'])

    def test_uncertain_creation_never_starts_agent_and_restart_does_not_retry(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[job_id]['status'], 'interrupted')
        self.app.postgres_operations.operations['demo-app'] = self.created_operation('needs_attention')
        with patch.object(self.app, 'run_agent') as deploy:
            self.app.run_postgres_then_agent(job_id)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]['status'], 'interrupted')

    def test_opt_in_upload_persists_and_restores_postgres_request(self):
        database = {'database_id': 'onedeploy-demo-app'}
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database) as inspect:
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        inspect.assert_called_once_with()
        job_id = payload['id']
        job = self.app.jobs[job_id]
        self.assertEqual(job['postgres']['application_id'], 'demo-app')
        self.assertEqual(job['postgres']['subnet_ids'], ['subnet-11111111', 'subnet-22222222'])
        infrastructure = job['infrastructure_plan']
        self.assertEqual(infrastructure['workload'], 'postgresql-http')
        self.assertEqual(infrastructure['database'],
                         {'binding': 'existing', 'database_id': 'onedeploy-demo-app'})
        self.assertIn('existing RDS PostgreSQL', infrastructure['resources'])
        self.assertIn('one-off SQL migration task', infrastructure['resources'])
        self.assertIn('package.json', infrastructure['detected_files'])
        self.assertNotIn('password', json.dumps(job).lower())
        with patch('onedeploy.server.DeploymentTools') as tools, \
                patch('onedeploy.server.DeploymentAgent') as agent:
            agent.return_value.run.return_value = {'url': 'https://example.test'}
            self.app.run_agent(job_id)
        self.assertEqual(tools.call_args.kwargs['postgres_request'].application_id, 'demo-app')
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.recovery_warnings, [])
        self.assertEqual(postgres_request_from_job(restored.jobs[job_id]).subnet_ids,
                         ('subnet-11111111', 'subnet-22222222'))

    def test_auto_target_uses_only_supported_owned_postgres_path(self):
        database = {'database_id': 'onedeploy-demo-app'}
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database), \
                patch.object(self.app, 'infrastructure_planner_factory') as planner:
            status, payload = self.upload(archive(), target='auto')
        self.assertEqual(status, 202)
        planner.assert_not_called()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['requested_target'], 'auto')
        self.assertEqual(job['target'], 'aws-ecs-express')
        self.assertEqual(job['infrastructure_plan']['planner'], 'policy')
        self.assertEqual(job['infrastructure_plan']['database']['binding'], 'existing')

    def test_single_action_existing_rds_upload_resolves_owned_network(self):
        database = {'database_id': 'onedeploy-demo-app', 'account': ACCOUNT,
                    'region': REGION, 'vpc_id': 'vpc-12345678',
                    'subnet_ids': ['subnet-11111111', 'subnet-22222222']}
        with patch('onedeploy.server.discover_existing_postgres',
                   return_value=database) as discover, \
                patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                      return_value=database) as inspect:
            status, payload = self.upload(archive(), target='auto', network_headers=False)
        self.assertEqual(status, 202)
        discover.assert_called_once_with('demo-app', self.settings)
        inspect.assert_called_once_with()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['postgres']['vpc_id'], 'vpc-12345678')
        self.assertEqual(job['postgres']['subnet_ids'], database['subnet_ids'])
        self.assertEqual(job['target'], 'aws-ecs-express')

    def test_upload_waits_for_confirmed_database_creation(self):
        database = {'database_id': 'onedeploy-demo-app'}
        operation = {'status': 'running', 'database_id': 'onedeploy-demo-app'}
        self.app.postgres_operations.operations['demo-app'] = operation
        for status_name in ('running', 'needs_attention', 'recovering'):
            with self.subTest(status=status_name), patch(
                    'onedeploy.server.AwsPostgresProvisioner.inspect_current',
                    return_value=database):
                operation['status'] = status_name
                status, payload = self.upload(archive())
                self.assertEqual(status, 400)
                self.assertIn('재확인', payload['error'])
                self.assertFalse(self.app.jobs)

        operation['status'] = 'succeeded'
        operation['database_id'] = 'onedeploy-other-app'
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('식별자', payload['error'])
        self.assertFalse(self.app.jobs)

        operation['database_id'] = database['database_id']
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database):
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        self.assertEqual(self.app.jobs[payload['id']]['infrastructure_plan']['database']['database_id'],
                         database['database_id'])

    def test_upload_rejects_untrusted_creation_journal(self):
        self.app.postgres_operations.untrusted_applications.add('demo-app')
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'onedeploy-demo-app'}):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('기록', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_partial_postgres_network_headers_are_rejected_before_discovery(self):
        with patch('onedeploy.server.discover_existing_postgres') as discover:
            status, payload = self.upload(archive(), partial_network=True)
        self.assertEqual(status, 400)
        self.assertIn('함께', payload['error'])
        discover.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_auto_target_without_explicit_binding_still_rejects_postgres(self):
        status, payload = self.upload(archive(), postgres=False, target='auto')
        self.assertEqual(status, 400)
        self.assertIn('데이터베이스', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_auto_postgres_requires_public_aws_path_before_db_inspection(self):
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(), target='auto', public=False)
        self.assertEqual(status, 400)
        self.assertIn('공개 AWS', payload['error'])
        inspect.assert_not_called()

    def test_missing_sql_bundle_rejects_before_db_inspection(self):
        with patch('onedeploy.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(include_migration=False))
        self.assertEqual(status, 400)
        self.assertIn('migrations/', payload['error'])
        inspect.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_existing_database_release_cannot_be_replaced_without_binding(self):
        prior_id = 'f' * 16
        self.app.jobs[prior_id] = {'id': prior_id, 'application_id': 'demo-app',
            'target': 'aws-ecs-express', 'status': 'succeeded',
            'deployment_state': 'active', 'created_at': '2026-01-01T00:00:00+00:00',
            'result': {'database': {'database_id': 'onedeploy-demo-app'}}}
        status, payload = self.upload(archive(include_migration=False, database=False),
                                      postgres=False)
        self.assertEqual(status, 400)
        self.assertIn('동일한 DB 연결', payload['error'])
        self.assertEqual(list(self.app.jobs), [prior_id])


if __name__ == '__main__':
    unittest.main()
