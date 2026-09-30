import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from onedeploy.aws_migrations import AwsMigrationRunner, inspect_migration_task, main
from onedeploy.migrations import collect_sql_migrations
from onedeploy.postgres import PostgresRequest


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
ATTEMPT = 'a' * 16 + '-a1'
TASK_DEF = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/onedeploy-migrate-{ATTEMPT}:1'
TASK = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + '1234567890abcdef' * 2


class AwsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        project = Path(self.temp.name)
        (project / 'migrations').mkdir()
        (project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo (id int);')
        self.request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                       ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
        self.database = {'service_security_group': self.request.service_security_group,
                         'migration_log_group': '/onedeploy/migrations/demo-app',
                         'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-role',
                         'endpoint': f'db.{REGION}.rds.amazonaws.com', 'port': 5432,
                         'secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id'}
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION, expected_account=ACCOUNT))
        self.runner = AwsMigrationRunner(self.adapter, self.request, self.database,
            collect_sql_migrations(project),
            f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/onedeploy-managed', ATTEMPT)

    def test_preflight_selects_public_subnet_and_checks_log_retention(self):
        def aws(args, **_kwargs):
            if args[:2] == ['ec2', 'describe-route-tables']:
                return json.dumps({'RouteTables': [
                    {'Associations': [{'Main': True}], 'Routes': []},
                    {'Associations': [{'SubnetId': self.request.subnet_ids[1]}], 'Routes': [
                        {'DestinationCidrBlock': '0.0.0.0/0', 'GatewayId': 'igw-12345678',
                         'State': 'active'}]}]})
            return json.dumps({'logGroups': [{'logGroupName': self.database['migration_log_group'],
                                              'retentionInDays': 14}]})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.runner.preflight(), self.request.subnet_ids[1])
        self.assertEqual(self.runner.task_definition()['containerDefinitions'][0]['secrets'][1],
                         {'name': 'PGPASSWORD', 'valueFrom': self.database['secret_arn'] + ':password::'})
        self.assertNotIn('password', json.dumps(self.runner.task_definition()['containerDefinitions'][0]['environment']))

    def test_run_task_verifies_owned_exit_zero(self):
        self.runner.subnet = self.request.subnet_ids[0]
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        tags = [{'key': 'onedeploy-managed', 'value': 'true'},
                {'key': 'onedeploy-app', 'value': 'demo-app'},
                {'key': 'onedeploy-attempt', 'value': ATTEMPT}]
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF, 'lastStatus': 'STOPPED',
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE',
                'tags': tags, 'containers': [{'name': 'migration', 'exitCode': 0}]}
        def aws(args, **_kwargs):
            if '--cli-input-json' in args:
                file = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                payload = json.loads(file.read_text())
                if args[:2] == ['ecs', 'register-task-definition']:
                    self.assertEqual(payload['executionRoleArn'], self.database['execution_role_arn'])
                    return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF}})
                self.assertEqual(payload['networkConfiguration']['awsvpcConfiguration'], {
                    'subnets': [self.request.subnet_ids[0]],
                    'securityGroups': [self.request.service_security_group], 'assignPublicIp': 'ENABLED'})
                return json.dumps({'tasks': [{'taskArn': TASK}], 'failures': []})
            if args[:2] == ['ecs', 'wait']:
                return ''
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [task]})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws):
            result = self.runner.run_task()
            self.assertEqual(result['task_arn'], TASK)
            self.assertTrue(self.runner.completed)
            self.assertEqual([item['aws_migration_status'] for item in checkpoints],
                             ['registered', 'running'])
            self.assertEqual(checkpoints[1]['aws_migration_task_arn'], TASK)
            task['containers'][0]['exitCode'] = 1
            with self.assertRaisesRegex(AwsConfigurationError, '마이그레이션이 실패'):
                self.runner.run_task()
            self.assertFalse(self.runner.completed)

    def test_read_only_inspection_distinguishes_running_failed_and_expired(self):
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF,
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE', 'lastStatus': 'RUNNING',
                'tags': [{'key': 'onedeploy-managed', 'value': 'true'},
                         {'key': 'onedeploy-app', 'value': 'demo-app'},
                         {'key': 'onedeploy-attempt', 'value': ATTEMPT}],
                'containers': [{'name': 'migration'}]}
        args = (self.adapter, 'demo-app', ACCOUNT, REGION, ATTEMPT, TASK, TASK_DEF)
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})) as aws:
            self.assertEqual(inspect_migration_task(*args)['status'], 'running')
            aws.assert_called_once()
            self.assertEqual(aws.call_args.args[0][:2], ['ecs', 'describe-tasks'])
        task['lastStatus'] = 'STOPPED'
        task['containers'][0]['exitCode'] = 1
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'failed')
        task['containers'] = []
        task['stopCode'] = 'TaskFailedToStart'
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'failed')
        task['tags'][1]['value'] = 'another-app'
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            with self.assertRaisesRegex(AwsConfigurationError, '소유권'):
                inspect_migration_task(*args)
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [], 'failures': [
                {'arn': TASK, 'reason': 'MISSING'}]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'unknown')
        with patch.object(self.adapter, 'aws') as aws:
            with self.assertRaisesRegex(ValueError, 'ARN'):
                inspect_migration_task(*args[:-2], TASK + '-wrong', TASK_DEF)
            aws.assert_not_called()

    def test_inspect_command_pins_account_before_describing_task(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--attempt', ATTEMPT, '--task-arn', TASK,
                     '--task-definition-arn', TASK_DEF]
        with patch.object(AwsExpressAdapter, 'aws', return_value=json.dumps({'Account': '000000000000'})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '현재 AWS 계정'):
                main(arguments)
            self.assertEqual(aws.call_count, 1)
        def aws(args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'tasks': [], 'failures': [{'arn': TASK, 'reason': 'MISSING'}]})
        output = io.StringIO()
        with patch.object(AwsExpressAdapter, 'aws', side_effect=aws), redirect_stdout(output):
            main(arguments)
        self.assertEqual(json.loads(output.getvalue())['status'], 'unknown')

    def test_uncertain_task_result_is_recorded_and_stops_retry(self):
        self.runner.subnet = self.request.subnet_ids[0]
        self.runner.registered_arn = TASK_DEF
        self.runner.task_arn = TASK
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def command(args, **_kwargs):
            return 'unix:///var/run/docker.sock' if args[:3] == ['docker', 'context', 'inspect'] else ''
        def aws(args, **_kwargs):
            self.assertEqual(args[:2], ['ecr', 'get-login-password'])
            return 'synthetic-password'
        with patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', side_effect=aws), \
                patch.object(self.runner, 'run_task', side_effect=RuntimeError('wait timed out')):
            with self.assertRaises(AwsConfigurationError) as error:
                self.runner.build_and_run()
        self.assertFalse(error.exception.retryable)
        self.assertIn(TASK, str(error.exception))
        self.assertEqual(checkpoints[-1]['aws_migration_status'], 'needs_attention')

    def test_success_is_checkpointed_before_cleanup(self):
        self.runner.subnet = self.request.subnet_ids[0]
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def command(args, **_kwargs):
            return 'unix:///var/run/docker.sock' if args[:3] == ['docker', 'context', 'inspect'] else ''
        with patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', return_value='synthetic-password'), \
                patch.object(self.runner, 'run_task', return_value={'task_arn': TASK,
                    'bundle_digest': self.runner.bundle.digest}) as run, \
                patch.object(self.runner, 'cleanup_completed', return_value=True) as cleanup:
            result = self.runner.build_and_run()
        run.assert_called_once_with()
        cleanup.assert_called_once_with()
        self.assertEqual(result['cleanup_complete'], True)
        self.assertEqual(checkpoints[0]['aws_migration_status'], 'succeeded')
        self.assertEqual(checkpoints[0]['aws_migration_result']['task_arn'], TASK)

    def test_cleanup_only_known_completed_task_artifacts(self):
        with patch.object(self.adapter, 'aws') as aws:
            self.assertFalse(self.runner.cleanup_completed())
            aws.assert_not_called()
        self.runner.registered_arn = TASK_DEF
        self.runner.task_arn = TASK
        with patch.object(self.adapter, 'aws') as aws:
            self.assertFalse(self.runner.cleanup_completed())
            aws.assert_not_called()
        self.runner.completed = True
        def aws(args, **_kwargs):
            if args[:2] == ['ecs', 'deregister-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF,
                                                       'status': 'INACTIVE'}})
            return json.dumps({'imageIds': [{'imageTag': ATTEMPT + '-db'}], 'failures': []})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.assertTrue(self.runner.cleanup_completed())
            self.assertFalse(self.runner.completed)


if __name__ == '__main__':
    unittest.main()
