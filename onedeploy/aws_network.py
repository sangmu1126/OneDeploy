"""Opt-in, application-owned security group for the ECS and RDS data path."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from onedeploy.aws import (AwsConfigurationError, AwsExpressAdapter, AwsSettings,
                           service_group_ingress_is_restricted)


TEMPLATE = Path(__file__).parent / 'infra' / 'aws-service-network.json'


@dataclass(frozen=True)
class ServiceNetworkRequest:
    application_id: str
    account: str
    region: str
    vpc_id: str

    @property
    def stack_name(self) -> str:
        return 'onedeploy-network-' + self.application_id

    def validate(self) -> None:
        if (not 3 <= len(self.application_id) <= 31
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.application_id)):
            raise ValueError('앱 ID는 3~31자리 소문자·숫자와 내부 하이픈이어야 합니다.')
        if not re.fullmatch(r'\d{12}', self.account):
            raise ValueError('AWS 계정 ID는 12자리 숫자여야 합니다.')
        if not re.fullmatch(r'vpc-[a-f0-9]{8,17}', self.vpc_id):
            raise ValueError('VPC ID가 올바르지 않습니다.')
        AwsSettings(self.region, expected_account=self.account,
                    account_pin_required=True).validate()


class AwsServiceNetworkProvisioner:
    def __init__(self, request: ServiceNetworkRequest):
        request.validate()
        self.request = request
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(
            request.region, expected_account=request.account, account_pin_required=True))

    def preflight(self) -> dict:
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                              private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        vpcs = json.loads(self.adapter.aws(['ec2', 'describe-vpcs', '--vpc-ids', req.vpc_id],
                                          private=True, quiet=True)).get('Vpcs', [])
        if len(vpcs) != 1 or vpcs[0].get('VpcId') != req.vpc_id or not vpcs[0].get('IsDefault'):
            raise AwsConfigurationError('ECS Express와 같은 기본 VPC가 필요합니다.')
        listed = json.loads(self.adapter.aws(['cloudformation', 'list-stacks'],
                                            private=True, quiet=True))
        stacks = listed.get('StackSummaries')
        if (not isinstance(stacks, list) or listed.get('NextToken')
                or any(not isinstance(item, dict) for item in stacks)):
            raise AwsConfigurationError('CloudFormation 스택 목록을 완전히 확인하지 못했습니다.')
        if any(item.get('StackName') == req.stack_name for item in stacks):
            raise AwsConfigurationError('이 앱의 서비스 네트워크 스택 기록이 이미 있습니다. --inspect를 사용하세요.')
        return {'application_id': req.application_id, 'account': req.account,
                'region': req.region, 'vpc_id': req.vpc_id,
                'stack_name': req.stack_name, 'initial_ingress': []}

    def create(self) -> dict:
        plan = self.preflight()
        req = self.request
        payload = {'StackName': req.stack_name, 'TemplateBody': TEMPLATE.read_text(),
                   'EnableTerminationProtection': True,
                   'Parameters': [
                       {'ParameterKey': 'ApplicationId', 'ParameterValue': req.application_id},
                       {'ParameterKey': 'VpcId', 'ParameterValue': req.vpc_id}],
                   'Tags': [{'Key': 'onedeploy-managed', 'Value': 'true'},
                            {'Key': 'onedeploy-app', 'Value': req.application_id}]}
        with tempfile.NamedTemporaryFile('w', suffix='.json', prefix='onedeploy-network-') as file:
            json.dump(payload, file)
            file.flush()
            created = json.loads(self.adapter.aws(['cloudformation', 'create-stack',
                '--cli-input-json', 'file://' + file.name], timeout=60, private=True))
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack_id = created.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('서비스 네트워크 생성 결과를 확인하지 못했습니다. 스택 상태를 확인하세요.')
        try:
            self.adapter.aws(['cloudformation', 'wait', 'stack-create-complete',
                              '--stack-name', stack_id], timeout=900, private=True, quiet=True)
            return self.inspect(stack_id)
        except Exception as exc:
            raise RuntimeError(f'서비스 네트워크 생성 결과가 불확실합니다. {stack_id} 상태를 확인하세요: {exc}') from None

    def inspect_current(self) -> dict:
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                              private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', req.stack_name], private=True, quiet=True)).get('Stacks', [])
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack = stacks[0] if len(stacks) == 1 else {}
        stack_id = stack.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('앱 전용 서비스 네트워크 스택을 확인하지 못했습니다.')
        return self.inspect(stack_id)

    def inspect(self, stack_id: str) -> dict:
        req = self.request
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('서비스 네트워크 스택 ARN이 예상과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', stack_id], private=True, quiet=True)).get('Stacks', [])
        stack = stacks[0] if len(stacks) == 1 else {}
        tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
        outputs = {item.get('OutputKey'): item.get('OutputValue') for item in stack.get('Outputs', [])}
        group_id = outputs.get('ServiceSecurityGroupId', '')
        if (stack.get('StackId') != stack_id or stack.get('StackStatus') != 'CREATE_COMPLETE'
                or tags.get('onedeploy-managed') != 'true'
                or tags.get('onedeploy-app') != req.application_id
                or outputs.get('VpcId') != req.vpc_id
                or not isinstance(group_id, str)
                or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id)):
            raise AwsConfigurationError('서비스 네트워크 스택 상태·출력·소유권이 예상과 다릅니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--group-ids', group_id], private=True, quiet=True)).get('SecurityGroups', [])
        group = groups[0] if len(groups) == 1 else {}
        group_tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])}
        if (group.get('GroupId') != group_id or group.get('OwnerId') != req.account
                or group.get('VpcId') != req.vpc_id
                or group_tags.get('onedeploy-managed') != 'true'
                or group_tags.get('onedeploy-app') != req.application_id
                or not service_group_ingress_is_restricted(group)):
            raise AwsConfigurationError('서비스 보안 그룹의 소유권·VPC·인바운드 규칙이 예상과 다릅니다.')
        return {'application_id': req.application_id, 'account': req.account,
                'region': req.region, 'vpc_id': req.vpc_id,
                'stack_id': stack_id, 'service_security_group': group_id,
                'status': 'available'}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Create an application-owned ECS security group')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create the CloudFormation stack')
    mode.add_argument('--inspect', action='store_true', help='Read and verify an existing stack')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    args = parser.parse_args(argv)
    request = ServiceNetworkRequest(args.application, args.account, args.region, args.vpc_id)
    provisioner = AwsServiceNetworkProvisioner(request)
    result = (provisioner.create() if args.apply else
              provisioner.inspect_current() if args.inspect else provisioner.preflight())
    if not args.apply and not args.inspect:
        print('읽기 전용 확인 완료. --apply 없이는 서비스 보안 그룹을 생성하지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
