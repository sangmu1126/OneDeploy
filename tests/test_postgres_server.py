import io
import json
import tempfile
import unittest
import zipfile
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

    def upload(self, content, *, postgres=True):
        handler_class = handler_for(self.app)
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/deployments'
        handler.headers = {'X-OneDeploy-Token': self.app.token,
                           'X-Deploy-Target': 'aws-ecs-express',
                           'X-Public-Access': 'true', 'X-Application-Id': 'demo-app',
                           'Content-Length': str(len(content))}
        if postgres:
            handler.headers.update({'X-Postgres-Existing': 'true',
                'X-Postgres-Vpc-Id': 'vpc-12345678',
                'X-Postgres-Subnet-Ids': 'subnet-11111111,subnet-22222222'})
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
                patch('onedeploy.server.AwsPostgresProvisioner.create') as create:
            handler.do_POST()
        status, result = handler.json_response.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['pricing'], plan['pricing'])
        self.assertTrue(result['plan_id'])
        preflight.assert_called_once_with()
        create.assert_not_called()

    def test_creation_requires_preview_then_journals_async_operation(self):
        plan = {'account': ACCOUNT, 'pricing': {'baseline_730h_usd': '20.87'}}
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
            ('subnet-11111111', 'subnet-22222222'), GROUP)
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=plan), \
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
