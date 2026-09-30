import json
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError
from onedeploy.postgres import AwsPostgresProvisioner, PostgresRequest, TEMPLATE, main


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
SUBNETS = ('subnet-11111111', 'subnet-22222222')
SERVICE_GROUP = 'sg-33333333'
DB_GROUP = 'sg-44444444'
STACK = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/onedeploy-db-demo-app/stack-id'
DB_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:db:onedeploy-demo-app'
SECRET = f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:rds-managed-secret'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/onedeploy-db-demo-app-execution'
HOST = f'onedeploy-demo-app.abc.{REGION}.rds.amazonaws.com'


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.request = PostgresRequest('demo-app', ACCOUNT, REGION, VPC, SUBNETS, SERVICE_GROUP)
        self.provisioner = AwsPostgresProvisioner(self.request)

    def test_template_retains_private_encrypted_database_and_managed_secret(self):
        template = json.loads(TEMPLATE.read_text())
        db = template['Resources']['Database']
        self.assertEqual(db['DeletionPolicy'], 'Retain')
        self.assertEqual(db['UpdateReplacePolicy'], 'Retain')
        properties = db['Properties']
        self.assertFalse(properties['PubliclyAccessible'])
        self.assertTrue(properties['DeletionProtection'])
        self.assertTrue(properties['StorageEncrypted'])
        self.assertTrue(properties['ManageMasterUserPassword'])
        self.assertEqual(properties['EngineVersion'], {'Ref': 'EngineVersion'})
        self.assertEqual(properties['EngineLifecycleSupport'],
                         'open-source-rds-extended-support-disabled')
        self.assertNotIn('MasterUserPassword', properties)
        self.assertEqual(template['Resources']['DatabaseSecurityGroup']['Properties']
                         ['SecurityGroupIngress'][0]['SourceSecurityGroupId'],
                         {'Ref': 'ServiceSecurityGroupId'})
        role = template['Resources']['DatabaseExecutionRole']['Properties']
        self.assertEqual(role['AssumeRolePolicyDocument']['Statement'][0]['Principal'],
                         {'Service': 'ecs-tasks.amazonaws.com'})
        self.assertEqual(role['Policies'][0]['PolicyDocument']['Statement'], [{
            'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
            'Resource': {'Fn::GetAtt': ['Database', 'MasterUserSecret.SecretArn']}}])
        self.assertEqual(template['Outputs']['DatabaseExecutionRoleArn']['Value'],
                         {'Fn::GetAtt': ['DatabaseExecutionRole', 'Arn']})
        self.assertEqual(template['Resources']['MigrationLogGroup']['Properties']['RetentionInDays'], 14)

    def test_preflight_checks_account_vpc_group_and_two_azs(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [{'GroupId': SERVICE_GROUP,
                    'VpcId': VPC, 'IpPermissions': []}]})
            if args[:2] == ['ec2', 'describe-subnets']:
                return json.dumps({'Subnets': [
                    {'SubnetId': SUBNETS[0], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'a'},
                    {'SubnetId': SUBNETS[1], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'b'}]})
            if args[:2] == ['rds', 'describe-orderable-db-instance-options']:
                return json.dumps({'OrderableDBInstanceOptions': [{
                    'Engine': 'postgres', 'EngineVersion': '17.5',
                    'DBInstanceClass': 'db.t4g.micro', 'StorageType': 'gp3', 'Vpc': True,
                    'SupportsStorageEncryption': True, 'MinStorageSize': 20,
                    'MaxStorageSize': 65536,
                    'AvailabilityZones': [{'Name': 'a'}, {'Name': 'b'}]}]})
            if args[:2] == ['rds', 'describe-db-engine-versions']:
                return json.dumps({'DBEngineVersions': [{'Engine': 'postgres', 'EngineVersion': '17.5'}]})
            raise AssertionError(args)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            result = self.provisioner.preflight()
        self.assertEqual(result['availability_zones'], ['a', 'b'])
        self.assertEqual(result['engine_version'], '17.5')
        self.assertEqual(result['storage_type'], 'gp3')
        self.assertEqual(calls, [['sts', 'get-caller-identity'], ['ec2', 'describe-vpcs'],
                                 ['ec2', 'describe-security-groups'], ['ec2', 'describe-subnets'],
                                 ['rds', 'describe-db-engine-versions'],
                                 ['rds', 'describe-orderable-db-instance-options']])
        with patch.object(self.provisioner.adapter, 'aws',
                          side_effect=lambda args, **kwargs: json.dumps({'Account': '999999999999'})):
            with self.assertRaisesRegex(AwsConfigurationError, '현재 AWS 계정'):
                self.provisioner.preflight()
        def wrong_storage(args, **kwargs):
            result = json.loads(aws(args, **kwargs))
            if args[:2] == ['rds', 'describe-orderable-db-instance-options']:
                result['OrderableDBInstanceOptions'][0]['StorageType'] = 'gp2'
            return json.dumps(result)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=wrong_storage):
            with self.assertRaisesRegex(AwsConfigurationError, 'gp3'):
                self.provisioner.preflight()

    def test_create_uses_create_only_and_checks_output_after_wait(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['cloudformation', 'create-stack']:
                spec = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(spec.stat().st_mode & 0o777, 0o600)
                payload = json.loads(spec.read_text())
                self.assertEqual(payload['StackName'], self.request.stack_name)
                self.assertTrue(payload['EnableTerminationProtection'])
                self.assertEqual(payload['Capabilities'], ['CAPABILITY_IAM'])
                self.assertIn({'ParameterKey': 'EngineVersion', 'ParameterValue': '17.5'},
                              payload['Parameters'])
                self.assertEqual(json.loads(payload['TemplateBody'])['Resources']['Database']
                                 ['Properties']['PubliclyAccessible'], False)
                return json.dumps({'StackId': STACK})
            if args[:2] == ['cloudformation', 'wait']:
                return ''
            raise AssertionError(args)
        with patch.object(self.provisioner, 'preflight', return_value={'engine_version': '17.5'}), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'status': 'available'}) as inspect:
            self.assertEqual(self.provisioner.create(), {'status': 'available'})
        self.assertEqual(calls, [['cloudformation', 'create-stack'], ['cloudformation', 'wait']])
        inspect.assert_called_once_with(STACK)

    def test_inspect_rejects_public_or_misowned_database(self):
        stack = {'StackId': STACK, 'StackStatus': 'CREATE_COMPLETE',
                 'Tags': [{'Key': 'onedeploy-managed', 'Value': 'true'},
                          {'Key': 'onedeploy-app', 'Value': 'demo-app'}],
                 'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in {
                     'DatabaseIdentifier': 'onedeploy-demo-app', 'DatabaseArn': DB_ARN,
                     'RequestedEngineVersion': '17.5',
                     'EndpointAddress': HOST, 'EndpointPort': '5432',
                     'SecretArn': SECRET, 'DatabaseExecutionRoleArn': ROLE,
                     'MigrationLogGroupName': '/onedeploy/migrations/demo-app',
                     'DatabaseSecurityGroupId': DB_GROUP}.items()]}
        db = {'DBInstanceIdentifier': 'onedeploy-demo-app', 'DBInstanceArn': DB_ARN,
              'DBInstanceStatus': 'available', 'Engine': 'postgres', 'EngineVersion': '17.5',
              'DBInstanceClass': 'db.t4g.micro', 'StorageType': 'gp3', 'AllocatedStorage': 20,
              'EngineLifecycleSupport': 'open-source-rds-extended-support-disabled',
              'DBName': 'appdb',
              'PubliclyAccessible': False, 'DeletionProtection': True, 'StorageEncrypted': True,
              'DBSubnetGroup': {'VpcId': VPC, 'Subnets': [
                  {'SubnetIdentifier': subnet} for subnet in SUBNETS]},
              'VpcSecurityGroups': [{'VpcSecurityGroupId': DB_GROUP}],
              'MasterUserSecret': {'SecretArn': SECRET},
              'Endpoint': {'Address': HOST, 'Port': 5432}}
        group = {'GroupId': DB_GROUP, 'VpcId': VPC, 'IpPermissions': [{
            'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
            'UserIdGroupPairs': [{'GroupId': SERVICE_GROUP, 'UserId': ACCOUNT, 'VpcId': VPC}]}]}
        def aws(args, **_kwargs):
            if args[:2] == ['cloudformation', 'describe-stacks']:
                return json.dumps({'Stacks': [stack]})
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [group]})
            return json.dumps({'DBInstances': [db]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'verify_execution_role'):
            result = self.provisioner.inspect(STACK)
            self.assertEqual(result['secret_arn'], SECRET)
            self.assertEqual(result['execution_role_arn'], ROLE)
            self.assertEqual(result['engine_version'], '17.5')
            db['EngineLifecycleSupport'] = 'open-source-rds-extended-support'
            with self.assertRaisesRegex(AwsConfigurationError, '실제 인스턴스'):
                self.provisioner.inspect(STACK)
            db['EngineLifecycleSupport'] = 'open-source-rds-extended-support-disabled'
            db['PubliclyAccessible'] = True
            with self.assertRaisesRegex(AwsConfigurationError, '비공개'):
                self.provisioner.inspect(STACK)
            db['PubliclyAccessible'] = False
            stack['Tags'][0]['Value'] = 'false'
            with self.assertRaisesRegex(AwsConfigurationError, '소유 태그'):
                self.provisioner.inspect(STACK)
            stack['Tags'][0]['Value'] = 'true'
            group['IpPermissions'][0]['IpRanges'] = [{'CidrIp': '0.0.0.0/0'}]
            with self.assertRaisesRegex(AwsConfigurationError, '인바운드'):
                self.provisioner.inspect(STACK)
            group['IpPermissions'][0].pop('IpRanges')
            db['StorageEncrypted'] = False
            with self.assertRaisesRegex(AwsConfigurationError, '비공개'):
                self.provisioner.inspect(STACK)

    def test_execution_role_must_only_read_owned_secret(self):
        role_name = ROLE.rsplit('/', 1)[-1]
        role = {'Arn': ROLE, 'RoleName': role_name,
                'Tags': [{'Key': 'onedeploy-managed', 'Value': 'true'},
                         {'Key': 'onedeploy-app', 'Value': 'demo-app'}],
                'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
                    'Action': 'sts:AssumeRole'}]}}
        managed = {'AttachedPolicies': [{'PolicyArn':
            'arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'}],
            'IsTruncated': False}
        inline = {'PolicyNames': ['ReadManagedDatabaseSecret'], 'IsTruncated': False}
        policy = {'RoleName': role_name, 'PolicyName': 'ReadManagedDatabaseSecret',
                  'PolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                      'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
                      'Resource': SECRET}]}}
        def aws(args, **_kwargs):
            if args[:2] == ['iam', 'get-role']:
                return json.dumps({'Role': role})
            if args[:2] == ['iam', 'list-attached-role-policies']:
                return json.dumps(managed)
            if args[:2] == ['iam', 'list-role-policies']:
                return json.dumps(inline)
            if args[:2] == ['iam', 'get-role-policy']:
                return json.dumps(policy)
            raise AssertionError(args)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.provisioner.verify_execution_role(ROLE, SECRET)
            role['AssumeRolePolicyDocument']['Statement'][0]['Principal'] = {'AWS': '*'}
            with self.assertRaisesRegex(AwsConfigurationError, '신뢰 정책'):
                self.provisioner.verify_execution_role(ROLE, SECRET)
            role['AssumeRolePolicyDocument']['Statement'][0]['Principal'] = {
                'Service': 'ecs-tasks.amazonaws.com'}
            policy['PolicyDocument']['Statement'][0]['Resource'] = '*'
            with self.assertRaisesRegex(AwsConfigurationError, '비밀 조회 권한'):
                self.provisioner.verify_execution_role(ROLE, SECRET)
            policy['PolicyDocument']['Statement'][0]['Resource'] = SECRET
            managed['AttachedPolicies'].append({'PolicyArn': 'arn:aws:iam::aws:policy/AdministratorAccess'})
            with self.assertRaisesRegex(AwsConfigurationError, '예상 밖의 정책'):
                self.provisioner.verify_execution_role(ROLE, SECRET)

    def test_inspect_current_checks_account_and_stack_name(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'Stacks': [{'StackId': STACK}]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'status': 'available'}) as inspect:
            self.assertEqual(self.provisioner.inspect_current(), {'status': 'available'})
        self.assertIn(self.request.stack_name, calls[1])
        inspect.assert_called_once_with(STACK)
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({'Account': '999999999999'})), \
                patch.object(self.provisioner, 'inspect') as inspect:
            with self.assertRaisesRegex(AwsConfigurationError, '계정'):
                self.provisioner.inspect_current()
            inspect.assert_not_called()

    def test_dry_run_never_creates_stack(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--vpc-id', VPC, '--subnet-id', SUBNETS[0], '--subnet-id', SUBNETS[1],
                     '--service-security-group', SERVICE_GROUP]
        with patch('onedeploy.postgres.AwsPostgresProvisioner.preflight',
                   return_value={'account': ACCOUNT}), \
                patch('onedeploy.postgres.AwsPostgresProvisioner.create') as create:
            main(arguments)
            create.assert_not_called()

    def test_inspect_cli_never_creates_stack(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--vpc-id', VPC, '--subnet-id', SUBNETS[0], '--subnet-id', SUBNETS[1],
                     '--service-security-group', SERVICE_GROUP, '--inspect']
        with patch('onedeploy.postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'status': 'available'}) as inspect, \
                patch('onedeploy.postgres.AwsPostgresProvisioner.preflight') as preflight, \
                patch('onedeploy.postgres.AwsPostgresProvisioner.create') as create:
            main(arguments)
        inspect.assert_called_once_with()
        preflight.assert_not_called()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
