import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from onedeploy.aws_migrations import AwsMigrationRunner
from onedeploy.migrations import collect_sql_migrations
from onedeploy.postgres import PostgresRequest


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
ATTEMPT = 'a' * 16 + '-a1'
TASK_DEF = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/onedeploy-migrate-{ATTEMPT}:1'
TASK = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/1234567890abcdef'


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
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION))
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
        tags = [{'key': 'onedeploy-managed', 'value': 'true'},
                {'key': 'onedeploy-app', 'value': 'demo-app'},
                {'key': 'onedeploy-attempt', 'value': ATTEMPT}]
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF, 'lastStatus': 'STOPPED',
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
            task['containers'][0]['exitCode'] = 1
            with self.assertRaisesRegex(AwsConfigurationError, '마이그레이션이 실패'):
                self.runner.run_task()
            self.assertFalse(self.runner.completed)

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
