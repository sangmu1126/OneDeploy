import unittest
from unittest.mock import patch

from tests.smoke_aws_postgres import main


class AwsPostgresSmokeTests(unittest.TestCase):
    def test_default_mode_only_inspects_existing_database(self):
        arguments = ['--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333']
        with patch('tests.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'onedeploy-demo-app',
                                 'stack_id': 'owned-stack', 'status': 'available'}) as inspect, \
                patch('tests.smoke_aws_postgres.AwsExpressAdapter.deploy') as deploy:
            main(arguments)
        inspect.assert_called_once_with()
        deploy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
