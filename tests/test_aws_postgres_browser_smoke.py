import unittest
from unittest.mock import patch

from tests.smoke_aws_postgres_browser import main


class AwsPostgresBrowserSmokeTests(unittest.TestCase):
    def test_default_mode_only_discovers_existing_database(self):
        arguments = ['--account', '123456789012', '--region', 'ap-northeast-2',
                     '--service-security-group', 'sg-33333333']
        for runtime in ('node', 'python'):
            with self.subTest(runtime=runtime), \
                    patch('tests.smoke_aws_postgres_browser.discover_existing_postgres',
                          return_value={'database_id': 'onedeploy-demo-app',
                                        'status': 'available'}) as discover, \
                    patch('tests.smoke_aws_postgres_browser.subprocess.Popen') as chrome:
                main(arguments + ['--probe-runtime', runtime])
                discover.assert_called_once()
                chrome.assert_not_called()


if __name__ == '__main__':
    unittest.main()
