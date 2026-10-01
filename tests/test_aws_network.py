import json
import unittest
from pathlib import Path
from unittest.mock import patch

from onedeploy.aws import AwsConfigurationError
from onedeploy.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, TEMPLATE, main)

ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
GROUP = 'sg-33333333'
STACK = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/onedeploy-network-demo-app/stack-id'


class AwsNetworkTests(unittest.TestCase):
    def setUp(self):
        self.request = ServiceNetworkRequest('demo-app', ACCOUNT, REGION, VPC)
        self.provisioner = AwsServiceNetworkProvisioner(self.request)

    def test_template_has_app_ownership_and_no_inbound_rule(self):
        template = json.loads(TEMPLATE.read_text())
        group = template['Resources']['ServiceSecurityGroup']['Properties']
        self.assertNotIn('SecurityGroupIngress', group)
        self.assertEqual(group['VpcId'], {'Ref': 'VpcId'})
        self.assertIn({'Key': 'onedeploy-app', 'Value': {'Ref': 'ApplicationId'}},
                      group['Tags'])

    def test_preflight_checks_account_default_vpc_and_existing_stack(self):
        def aws(args, **_kwargs):
            if args[0] == 'sts':
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            return json.dumps({'StackSummaries': []})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.provisioner.preflight()['stack_name'],
                             'onedeploy-network-demo-app')
        def existing(args, **kwargs):
            if args[0] == 'cloudformation':
                return json.dumps({'StackSummaries': [{'StackName': self.request.stack_name}]})
            return aws(args, **kwargs)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=existing):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.provisioner.preflight()

    def test_create_checks_owned_output_after_stack_wait(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['cloudformation', 'create-stack']:
                file = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                payload = json.loads(file.read_text())
                self.assertTrue(payload['EnableTerminationProtection'])
                self.assertEqual(payload['StackName'], self.request.stack_name)
                return json.dumps({'StackId': STACK})
            return ''
        with patch.object(self.provisioner, 'preflight', return_value={'account': ACCOUNT}), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'service_security_group': GROUP}) as inspect:
            self.assertEqual(self.provisioner.create(), {'service_security_group': GROUP})
        self.assertEqual(calls, [['cloudformation', 'create-stack'],
                                 ['cloudformation', 'wait']])
        inspect.assert_called_once_with(STACK)

    def test_inspect_rejects_unowned_group(self):
        stack = {'StackId': STACK, 'StackStatus': 'CREATE_COMPLETE',
                 'Tags': [{'Key': 'onedeploy-managed', 'Value': 'true'},
                          {'Key': 'onedeploy-app', 'Value': 'demo-app'}],
                 'Outputs': [{'OutputKey': 'VpcId', 'OutputValue': VPC},
                             {'OutputKey': 'ServiceSecurityGroupId', 'OutputValue': GROUP}]}
        group = {'GroupId': GROUP, 'OwnerId': ACCOUNT, 'VpcId': VPC,
                 'IpPermissions': [], 'Tags': [
                     {'Key': 'onedeploy-managed', 'Value': 'true'},
                     {'Key': 'onedeploy-app', 'Value': 'demo-app'}]}
        def aws(args, **_kwargs):
            return json.dumps({'Stacks': [stack]} if args[0] == 'cloudformation'
                              else {'SecurityGroups': [group]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.provisioner.inspect(STACK)['service_security_group'], GROUP)
            group['Tags'][-1]['Value'] = 'other-app'
            with self.assertRaisesRegex(AwsConfigurationError, '소유권'):
                self.provisioner.inspect(STACK)

    def test_default_cli_mode_never_creates_group(self):
        args = ['--application', 'demo-app', '--account', ACCOUNT,
                '--region', REGION, '--vpc-id', VPC]
        with patch('onedeploy.aws_network.AwsServiceNetworkProvisioner.preflight',
                   return_value={'account': ACCOUNT}) as preflight, \
                patch('onedeploy.aws_network.AwsServiceNetworkProvisioner.create') as create:
            main(args)
        preflight.assert_called_once_with()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
