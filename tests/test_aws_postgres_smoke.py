import json
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.core import source_digest
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

    def test_apply_migrates_before_probe_and_reuses_schema_on_update(self):
        arguments = ['--apply', '--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333']
        service_arn = 'arn:aws:ecs:ap-northeast-2:123456789012:service/default/probe'
        calls = []
        class Adapter:
            def __init__(self, image):
                self.image = image
                self.image_pushed = True
            def deploy(self, project, plan, attempt, environment, postgres=None, migrations=None):
                self.assertions(project, migrations)
                assert plan.source_digest == source_digest(project)
                if self.image == 'v1':
                    (Path(project) / '.dockerignore').write_text('node_modules\n')
                calls.append(('deploy', self.image, migrations.digest))
                return {'url': 'https://example.test', 'service_arn': service_arn,
                        'image': self.image, 'migration': {
                            'bundle_digest': migrations.digest, 'cleanup_complete': True}}
            def assertions(self, project, migrations):
                assert migrations.migrations[0].name == '9000_onedeploy_probe.sql'
                assert 'CREATE TABLE' not in (Path(project) / 'server.js').read_text()
            def aws(self, args, **_kwargs):
                return json.dumps({'service': {'currentDeployment': None,
                    'activeConfigurations': [{'primaryContainer': {'image': 'v2'}}]}})
            def retire(self, result, attempt):
                calls.append(('retire', result['image']))
        first, second = Adapter('v1'), Adapter('v2')
        def probe(_url, _key, _record_id, method):
            calls.append(('probe', method))
        with patch('tests.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'onedeploy-demo-app',
                                 'stack_id': 'owned-stack', 'status': 'available'}), \
                patch('tests.smoke_aws_postgres.AwsExpressAdapter', side_effect=[first, second]), \
                patch('tests.smoke_aws_postgres.probe', side_effect=probe):
            main(arguments)
        self.assertEqual([item[:2] for item in calls], [
            ('deploy', 'v1'), ('probe', 'POST'), ('probe', 'GET'),
            ('deploy', 'v2'), ('probe', 'GET'), ('probe', 'DELETE'), ('retire', 'v2')])
        self.assertEqual(calls[0][2], calls[3][2])


if __name__ == '__main__':
    unittest.main()
