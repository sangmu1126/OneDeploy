import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from onedeploy.aws import AwsConfigurationError
from onedeploy.postgres import AwsPostgresProvisioner
from tests.smoke_aws_postgres_rollback_drill import retire_before_create


class RollbackDrillCleanupTests(unittest.TestCase):
    def setUp(self):
        self.network = Mock()
        self.created = {'stack_id': 'owned-network', 'service_security_group': 'sg-owned'}
        self.database = Mock(spec=AwsPostgresProvisioner)
        self.database.request = SimpleNamespace(application_id='dbdrill-1234abcd')
        self.manager = SimpleNamespace(operations={}, untrusted_unknown=False,
                                       untrusted_applications=set())

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_retires_only_after_confirming_database_and_stack_absence(self, retire):
        retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_database_absent.assert_called_once_with()
        self.database.assert_stack_available.assert_called_once_with()
        retire.assert_called_once_with(self.network, 'owned-network', 'sg-owned')

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_creation_was_recorded(self, retire):
        self.manager.operations['dbdrill-1234abcd'] = {'status': 'needs_attention'}
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_database_absent.assert_not_called()
        retire.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_database_absence_is_uncertain(self, retire):
        self.database.assert_database_absent.side_effect = AwsConfigurationError('unknown')
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_stack_available.assert_not_called()
        retire.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_stack_absence_is_uncertain(self, retire):
        self.database.assert_stack_available.side_effect = AwsConfigurationError('unknown')
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        retire.assert_not_called()


if __name__ == '__main__':
    unittest.main()
