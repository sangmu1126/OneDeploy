import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError, AwsSettings
from onedeploy.postgres_restore_drill_operations import RestoreDrillOperations


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
TARGET = 'onedeploy-restore-demo-app-drill-20261002'
SNAPSHOT = 'onedeploy-demo-app-backup-20261002'
GROUP = 'sg-12345678'
ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:db:{TARGET}'


class RestoreDrillOperationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'journal'
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               account_pin_required=True)
        self.operation = RestoreDrillOperations(
            self.root, 'demo-app', SNAPSHOT, TARGET, settings, 'vpc-12345678')

    def test_journal_precedes_resource_creation_and_blocks_duplicate_start(self):
        def create_group(_network):
            self.assertEqual(self.operation.get()['stage'], 'creating_group')
            return {'group_id': GROUP}

        def create_database(_instance):
            current = self.operation.get()
            self.assertEqual(current['stage'], 'requesting_restore')
            self.assertEqual(current['db_group_id'], GROUP)
            return {'target_arn': ARN, 'status': 'creating'}

        with patch.object(self.operation, 'plan', return_value={'vpc_id': 'vpc-12345678'}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.create',
                      autospec=True, side_effect=create_group), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.create',
                      autospec=True, side_effect=create_database):
            result = self.operation.start()
        self.assertEqual(result['status'], 'restoring')
        self.assertEqual(result['target_arn'], ARN)
        self.assertEqual(os.stat(self.root / (TARGET + '.json')).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, '이미'):
            self.operation.start()

    def test_uncertain_restore_is_recovered_by_read_only_reconcile(self):
        with patch.object(self.operation, 'plan', return_value={'vpc_id': 'vpc-12345678'}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.create',
                      return_value={'group_id': GROUP}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.create',
                      side_effect=AwsConfigurationError('timeout')):
            with self.assertRaisesRegex(AwsConfigurationError, 'timeout'):
                self.operation.start()
        self.assertEqual(self.operation.get()['stage'], 'requesting_restore')
        self.assertEqual(self.operation.get()['status'], 'needs_attention')
        with patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup._account'), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.inspect',
                      return_value={'group_id': GROUP}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.inspect',
                      return_value={'target_arn': ARN, 'status': 'available'}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.create') as group_create, \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.create') as db_create:
            result = self.operation.reconcile()
        self.assertEqual(result['status'], 'ready_for_probe')
        self.assertEqual(result['database_status'], 'available')
        group_create.assert_not_called()
        db_create.assert_not_called()

    def test_group_created_before_checkpoint_is_found_but_db_not_retried(self):
        with patch.object(self.operation, 'plan', return_value={'vpc_id': 'vpc-12345678'}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.create',
                      side_effect=AwsConfigurationError('uncertain group')):
            with self.assertRaises(AwsConfigurationError):
                self.operation.start()
        self.assertIsNone(self.operation.get()['db_group_id'])
        with patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup._account'), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.inspect',
                      return_value={'group_id': GROUP}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.inspect',
                      side_effect=AwsConfigurationError('not found')):
            result = self.operation.reconcile()
        self.assertEqual(result['db_group_id'], GROUP)
        self.assertEqual(result['status'], 'needs_attention')

    def test_reconcile_rejects_different_owned_group(self):
        with patch.object(self.operation, 'plan', return_value={'vpc_id': 'vpc-12345678'}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.create',
                      return_value={'group_id': GROUP}), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreInstance.create',
                      return_value={'target_arn': ARN, 'status': 'creating'}):
            self.operation.start()
        with patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup._account'), \
                patch('onedeploy.postgres_restore_drill_operations.RestoreSecurityGroup.inspect',
                      return_value={'group_id': 'sg-87654321'}):
            with self.assertRaisesRegex(AwsConfigurationError, 'ID'):
                self.operation.reconcile()


if __name__ == '__main__':
    unittest.main()
