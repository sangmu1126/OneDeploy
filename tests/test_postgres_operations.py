import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError, AwsSettings
from onedeploy.postgres import PostgresRequest
from onedeploy.postgres_operations import PostgresOperations


class PostgresOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'database-operations'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    service_security_group='sg-33333333')
        self.request = PostgresRequest('demo-app', '123456789012', 'ap-northeast-2',
                                       'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                                       'sg-33333333')
        self.manager = PostgresOperations(self.root, self.settings)
        self.quote = {'account': '123456789012', 'pricing': {'baseline_730h_usd': '20.87'}}
        guard = patch('onedeploy.postgres_operations.AwsPostgresProvisioner.assert_stack_available')
        guard.start()
        self.addCleanup(guard.stop)

    def test_accepted_create_is_journaled_before_worker_and_not_repeated_after_restart(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('onedeploy.postgres_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            operation = self.manager.start('demo-app', plan['plan_id'])
        self.assertEqual(operation['status'], 'running')
        self.assertEqual(start.call_count, 1)
        saved = json.loads((self.root / 'demo-app.json').read_text())
        self.assertEqual(saved['request']['subnet_ids'], list(self.request.subnet_ids))
        restored = PostgresOperations(self.root, self.settings)
        self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')
        with self.assertRaisesRegex(ValueError, '이미'):
            restored.plan(self.request)

    def test_changed_quote_rejects_before_creation_record(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   side_effect=[self.quote, {'account': '123456789012',
                       'pricing': {'baseline_730h_usd': '22.00'}}]), \
                patch('onedeploy.postgres_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            with self.assertRaisesRegex(ValueError, '변경'):
                self.manager.start('demo-app', plan['plan_id'])
        self.assertFalse((self.root / 'demo-app.json').exists())
        start.assert_not_called()

    def test_existing_stack_blocks_plan_before_any_creation_record(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('onedeploy.postgres_operations.AwsPostgresProvisioner.assert_stack_available',
                      side_effect=AwsConfigurationError('스택 기록이 이미 있습니다.')):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.manager.plan(self.request)
        self.assertFalse((self.root / 'demo-app.json').exists())

    def test_successful_worker_records_database_without_secret(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('onedeploy.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.create',
                   return_value={'database_id': 'onedeploy-demo-app',
                                 'secret_arn': 'secret'}) as create:
            self.manager._run('demo-app')
        create.assert_called_once_with(expected_plan=self.quote)
        operation = self.manager.get('demo-app')
        self.assertEqual(operation['status'], 'succeeded')
        self.assertNotIn('secret_arn', json.dumps(operation))
        self.assertEqual(operation['database_id'], 'onedeploy-demo-app')

    def test_reconcile_checks_owned_stack_and_completed_database(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('onedeploy.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        stack_id = ('arn:aws:cloudformation:ap-northeast-2:123456789012:'
                    'stack/onedeploy-db-demo-app/stack-id')
        def aws(args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': '123456789012'})
            return json.dumps({'Stacks': [{'StackId': stack_id,
                'StackStatus': 'CREATE_COMPLETE', 'Tags': [
                    {'Key': 'onedeploy-managed', 'Value': 'true'},
                    {'Key': 'onedeploy-app', 'Value': 'demo-app'}]}]})
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'onedeploy-demo-app'}) as inspect, \
                patch('onedeploy.postgres.AwsExpressAdapter.aws', autospec=True,
                      side_effect=lambda _self, args, **kwargs: aws(args, **kwargs)):
            result = self.manager.reconcile('demo-app')
        inspect.assert_called_once_with()
        self.assertEqual(result['status'], 'succeeded')

    def test_reconcile_refuses_unowned_stack(self):
        with patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('onedeploy.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        wrong = {'Stacks': [{'StackId': ('arn:aws:cloudformation:ap-northeast-2:'
            '123456789012:stack/onedeploy-db-demo-app/stack-id'),
            'StackStatus': 'CREATE_COMPLETE', 'Tags': []}]}
        with patch('onedeploy.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=lambda _self, args, **_kwargs: json.dumps(
                       {'Account': '123456789012'} if args[0] == 'sts' else wrong)), \
                patch('onedeploy.postgres_operations.AwsPostgresProvisioner.inspect_current') as inspect:
            result = self.manager.reconcile('demo-app')
        self.assertEqual(result['status'], 'needs_attention')
        inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
