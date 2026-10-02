"""Safety checks for the opt-in Chrome RDS retirement drill cleanup."""
import json
import unittest
from unittest.mock import patch

from onedeploy.aws import AwsSettings
from onedeploy.postgres import PostgresRequest
from tests.smoke_aws_postgres_create_browser import retire_final_snapshot


class BrowserRetirementCleanupTests(unittest.TestCase):
    def setUp(self):
        self.application = 'dbdrill-1234abcd'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    account_pin_required=True)
        self.request = PostgresRequest(self.application, '123456789012', 'ap-northeast-2',
                                       'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                                       'sg-33333333')
        self.operation = {'application_id': self.application,
                          'database_id': 'onedeploy-' + self.application,
                          'snapshot_id': 'onedeploy-' + self.application + '-final-123456abcdef',
                          'stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/onedeploy-db-dbdrill-1234abcd/id',
                          'status': 'succeeded', 'stage': 'stack_deleted'}

    def test_refuses_incomplete_retirement_before_any_aws_call(self):
        self.operation['status'] = 'needs_attention'
        with patch('tests.smoke_aws_postgres_create_browser.inspect_snapshot') as inspect:
            with self.assertRaises(AssertionError):
                retire_final_snapshot(self.application, self.operation,
                                      self.settings, self.request)
        inspect.assert_not_called()

    def test_refuses_unconfirmed_stack_before_deleting_snapshot(self):
        class FakeAdapter:
            def aws(self, args, **_kwargs):
                if args[:2] == ['cloudformation', 'describe-stacks']:
                    return json.dumps({'Stacks': [{'StackId': self_stack,
                                                   'StackStatus': 'DELETE_FAILED'}]})
                raise AssertionError('unexpected AWS call')

        class FakeProvisioner:
            def __init__(self, _request):
                self.adapter = FakeAdapter()

        self_stack = self.operation['stack_id']
        snapshot = {'snapshot_id': self.operation['snapshot_id'], 'status': 'available',
                    'encrypted': True, 'snapshot_arn': 'arn:aws:rds:example'}
        with patch('tests.smoke_aws_postgres_create_browser.inspect_snapshot',
                   return_value=snapshot), \
             patch('tests.smoke_aws_postgres_create_browser.AwsPostgresProvisioner',
                   FakeProvisioner):
            with self.assertRaisesRegex(AssertionError, 'DELETE_COMPLETE'):
                retire_final_snapshot(self.application, self.operation,
                                      self.settings, self.request)


if __name__ == '__main__':
    unittest.main()
