"""Opt-in, retained RDS PostgreSQL provisioning primitive for the AWS data path."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from onedeploy.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings


TEMPLATE = Path(__file__).parent / 'infra' / 'aws-postgres.json'
MANAGED_POSTGRES_ENV = frozenset({'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE'})


def policy_document(value):
    """AWS CLI versions may return IAM policy documents as JSON or URL-encoded JSON."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(urllib.parse.unquote(value))
            return decoded if isinstance(decoded, dict) else {}
        except ValueError:
            pass
    return {}


@dataclass(frozen=True)
class PostgresRequest:
    application_id: str
    account: str
    region: str
    vpc_id: str
    subnet_ids: tuple[str, ...]
    service_security_group: str

    @property
    def stack_name(self) -> str:
        return 'onedeploy-db-' + self.application_id

    @property
    def database_id(self) -> str:
        return 'onedeploy-' + self.application_id

    def validate(self) -> None:
        if (not 3 <= len(self.application_id) <= 31
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.application_id)):
            raise ValueError('DB 앱 ID는 소문자로 시작하는 3–31자리 영문·숫자와 내부 하이픈이어야 합니다.')
        if not re.fullmatch(r'\d{12}', self.account):
            raise ValueError('AWS 계정 ID는 12자리 숫자여야 합니다.')
        if not re.fullmatch(r'vpc-[a-f0-9]{8,17}', self.vpc_id):
            raise ValueError('VPC ID가 올바르지 않습니다.')
        if (not 2 <= len(self.subnet_ids) <= 8 or len(set(self.subnet_ids)) != len(self.subnet_ids)
                or any(not re.fullmatch(r'subnet-[a-f0-9]{8,17}', subnet) for subnet in self.subnet_ids)):
            raise ValueError('서로 다른 DB 서브넷 ID를 2–8개 지정하세요.')
        AwsSettings(self.region, expected_account=self.account, account_pin_required=True,
                    service_security_group=self.service_security_group).validate()
        if not self.service_security_group:
            raise ValueError('ECS 태스크에 추가할 서비스 보안 그룹 ID가 필요합니다.')


class AwsPostgresProvisioner:
    def __init__(self, request: PostgresRequest, event=lambda *_: None):
        request.validate()
        self.request = request
        self.adapter = AwsExpressAdapter(event, AwsSettings(
            request.region, expected_account=request.account, account_pin_required=True,
            service_security_group=request.service_security_group))

    def preflight(self) -> dict:
        """Read-only account and network checks; no DB or stack mutations."""
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        vpcs = json.loads(self.adapter.aws(['ec2', 'describe-vpcs', '--vpc-ids', req.vpc_id],
                                           private=True, quiet=True)).get('Vpcs', [])
        if len(vpcs) != 1 or vpcs[0].get('VpcId') != req.vpc_id or not vpcs[0].get('IsDefault'):
            raise AwsConfigurationError('ECS Express 공개 경로와 같은 기본 VPC가 필요합니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups', '--group-ids',
                                              req.service_security_group], private=True, quiet=True)).get('SecurityGroups', [])
        if (len(groups) != 1 or groups[0].get('GroupId') != req.service_security_group
                or groups[0].get('VpcId') != req.vpc_id or groups[0].get('IpPermissions') != []):
            raise AwsConfigurationError('서비스 보안 그룹은 지정한 기본 VPC에 있고 인바운드 규칙이 없어야 합니다.')
        subnets = json.loads(self.adapter.aws(['ec2', 'describe-subnets', '--subnet-ids', *req.subnet_ids],
                                              private=True, quiet=True)).get('Subnets', [])
        by_id = {item.get('SubnetId'): item for item in subnets}
        if (len(by_id) != len(req.subnet_ids) or set(by_id) != set(req.subnet_ids)
                or any(item.get('VpcId') != req.vpc_id or item.get('State') != 'available'
                       or not item.get('AvailabilityZone') for item in subnets)
                or len({item['AvailabilityZone'] for item in subnets}) < 2):
            raise AwsConfigurationError('DB 서브넷은 같은 VPC의 사용 가능한 서로 다른 가용 영역 두 곳 이상에 있어야 합니다.')
        orderable = json.loads(self.adapter.aws(['rds', 'describe-orderable-db-instance-options',
            '--engine', 'postgres', '--db-instance-class', 'db.t4g.micro', '--max-items', '1'],
            private=True, quiet=True)).get('OrderableDBInstanceOptions', [])
        if not orderable or orderable[0].get('DBInstanceClass') != 'db.t4g.micro':
            raise AwsConfigurationError('이 리전에서 PostgreSQL db.t4g.micro 구성을 확인하지 못했습니다.')
        return {'account': req.account, 'region': req.region, 'vpc_id': req.vpc_id,
                'subnet_ids': list(req.subnet_ids), 'availability_zones': sorted({
                    item['AvailabilityZone'] for item in subnets}),
                'service_security_group': req.service_security_group,
                'stack_name': req.stack_name, 'database_id': req.database_id,
                'instance_class': 'db.t4g.micro', 'storage_gib': 20,
                'publicly_accessible': False, 'deletion_protection': True,
                'retained_on_stack_delete': True}

    def create(self) -> dict:
        """Create one new stack only; never update or auto-delete a database."""
        self.preflight()
        req = self.request
        template = TEMPLATE.read_text(encoding='utf-8')
        payload = {'StackName': req.stack_name, 'TemplateBody': template,
                   'EnableTerminationProtection': True,
                   'Capabilities': ['CAPABILITY_IAM'],
                   'Parameters': [
                       {'ParameterKey': 'ApplicationId', 'ParameterValue': req.application_id},
                       {'ParameterKey': 'VpcId', 'ParameterValue': req.vpc_id},
                       {'ParameterKey': 'SubnetIds', 'ParameterValue': ','.join(req.subnet_ids)},
                       {'ParameterKey': 'ServiceSecurityGroupId', 'ParameterValue': req.service_security_group}],
                   'Tags': [{'Key': 'onedeploy-managed', 'Value': 'true'},
                            {'Key': 'onedeploy-app', 'Value': req.application_id}]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='onedeploy-rds-',
                                         encoding='utf-8') as spec:
            json.dump(payload, spec)
            spec.flush()
            created = json.loads(self.adapter.aws(['cloudformation', 'create-stack',
                '--cli-input-json', 'file://' + spec.name], timeout=60, private=True))
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack_id = created.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('생성 요청의 CloudFormation 스택 ARN이 예상과 다릅니다. AWS 리소스를 직접 확인하세요.')
        try:
            self.adapter.aws(['cloudformation', 'wait', 'stack-create-complete', '--stack-name', stack_id],
                             timeout=3600, private=True, quiet=True)
            return self.inspect(stack_id)
        except Exception as exc:
            raise RuntimeError(f'PostgreSQL 생성 결과를 확인하지 못했습니다. {stack_id} 상태와 비용을 확인하세요: {exc}') from None

    def inspect_current(self) -> dict:
        """Read and verify the existing application database without changing resources."""
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', req.stack_name], private=True, quiet=True)).get('Stacks', [])
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if len(stacks) != 1 or not isinstance(stacks[0].get('StackId'), str) or not stacks[0]['StackId'].startswith(prefix):
            raise AwsConfigurationError('지정한 앱의 PostgreSQL 스택을 확인하지 못했습니다.')
        return self.inspect(stacks[0]['StackId'])

    def verify_execution_role(self, role_arn: str, secret_arn: str) -> None:
        """Reject a role whose trust or grants drifted beyond the managed secret."""
        req = self.request
        role_name = role_arn.rsplit('/', 1)[-1]
        role = json.loads(self.adapter.aws(['iam', 'get-role', '--role-name', role_name],
                                           private=True, quiet=True)).get('Role', {})
        tags = {item.get('Key'): item.get('Value') for item in role.get('Tags', [])}
        trust = policy_document(role.get('AssumeRolePolicyDocument'))
        trust_statements = trust.get('Statement', [])
        if isinstance(trust_statements, dict):
            trust_statements = [trust_statements]
        expected_trust = {'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
                          'Action': 'sts:AssumeRole'}
        if (role.get('Arn') != role_arn or role.get('RoleName') != role_name
                or tags.get('onedeploy-managed') != 'true'
                or tags.get('onedeploy-app') != req.application_id
                or trust_statements != [expected_trust]):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할의 소유권 또는 신뢰 정책이 예상과 다릅니다.')
        managed = json.loads(self.adapter.aws(['iam', 'list-attached-role-policies', '--role-name', role_name],
                                              private=True, quiet=True))
        inline = json.loads(self.adapter.aws(['iam', 'list-role-policies', '--role-name', role_name],
                                             private=True, quiet=True))
        if (managed.get('IsTruncated') or inline.get('IsTruncated')
                or {item.get('PolicyArn') for item in managed.get('AttachedPolicies', [])}
                   != {'arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'}
                or inline.get('PolicyNames') != ['ReadManagedDatabaseSecret']):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할에 예상 밖의 정책이 연결됐습니다.')
        document = json.loads(self.adapter.aws(['iam', 'get-role-policy', '--role-name', role_name,
                                                '--policy-name', 'ReadManagedDatabaseSecret'],
                                               private=True, quiet=True))
        policy = policy_document(document.get('PolicyDocument'))
        expected_statement = {'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
                              'Resource': secret_arn}
        statements = policy.get('Statement', [])
        if isinstance(statements, dict):
            statements = [statements]
        if (document.get('RoleName') != role_name
                or document.get('PolicyName') != 'ReadManagedDatabaseSecret'
                or statements != [expected_statement]):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할의 비밀 조회 권한이 예상과 다릅니다.')

    def inspect(self, stack_id: str) -> dict:
        """Verify owned stack, nonpublic instance, network, and secret ARN; never read secret value."""
        req = self.request
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('PostgreSQL 스택 ARN이 예상과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks', '--stack-name', stack_id],
                                             private=True, quiet=True)).get('Stacks', [])
        if len(stacks) != 1 or stacks[0].get('StackId') != stack_id or stacks[0].get('StackStatus') != 'CREATE_COMPLETE':
            raise AwsConfigurationError('PostgreSQL 스택이 생성 완료 상태가 아닙니다.')
        tags = {item.get('Key'): item.get('Value') for item in stacks[0].get('Tags', [])}
        outputs = {item.get('OutputKey'): item.get('OutputValue') for item in stacks[0].get('Outputs', [])}
        db_arn = f'arn:aws:rds:{req.region}:{req.account}:db:{req.database_id}'
        secret_arn = outputs.get('SecretArn', '')
        execution_role_arn = outputs.get('DatabaseExecutionRoleArn', '')
        group_id = outputs.get('DatabaseSecurityGroupId', '')
        if (tags.get('onedeploy-managed') != 'true' or tags.get('onedeploy-app') != req.application_id
                or outputs.get('DatabaseIdentifier') != req.database_id or outputs.get('DatabaseArn') != db_arn
                or not isinstance(secret_arn, str) or not secret_arn.startswith(
                    f'arn:aws:secretsmanager:{req.region}:{req.account}:secret:')
                or not isinstance(execution_role_arn, str) or not re.fullmatch(
                    rf'arn:aws:iam::{req.account}:role/[A-Za-z0-9_+=,.@/-]+', execution_role_arn)
                or not isinstance(group_id, str) or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id)
                or not isinstance(outputs.get('EndpointAddress'), str)
                or not outputs['EndpointAddress'].endswith(f'.{req.region}.rds.amazonaws.com')
                or outputs.get('EndpointPort') != '5432'):
            raise AwsConfigurationError('PostgreSQL 스택 출력 또는 소유 태그가 예상과 다릅니다.')
        instances = json.loads(self.adapter.aws(['rds', 'describe-db-instances', '--db-instance-identifier',
                                                 req.database_id], private=True, quiet=True)).get('DBInstances', [])
        if len(instances) != 1:
            raise AwsConfigurationError('PostgreSQL 인스턴스를 하나로 확인하지 못했습니다.')
        db = instances[0]
        attached = {item.get('VpcSecurityGroupId') for item in db.get('VpcSecurityGroups', [])}
        if (db.get('DBInstanceIdentifier') != req.database_id or db.get('DBInstanceArn') != db_arn
                or db.get('DBInstanceStatus') != 'available' or db.get('Engine') != 'postgres'
                or db.get('DBName') != 'appdb' or db.get('PubliclyAccessible') is not False
                or db.get('DeletionProtection') is not True or db.get('StorageEncrypted') is not True
                or db.get('DBSubnetGroup', {}).get('VpcId') != req.vpc_id
                or {item.get('SubnetIdentifier') for item in db.get('DBSubnetGroup', {}).get('Subnets', [])}
                   != set(req.subnet_ids)
                or attached != {group_id}
                or db.get('MasterUserSecret', {}).get('SecretArn') != secret_arn
                or db.get('Endpoint', {}).get('Address') != outputs['EndpointAddress']
                or db.get('Endpoint', {}).get('Port') != 5432):
            raise AwsConfigurationError('PostgreSQL 실제 인스턴스가 비공개·보호 구성과 다릅니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups', '--group-ids', group_id],
                                             private=True, quiet=True)).get('SecurityGroups', [])
        rules = groups[0].get('IpPermissions', []) if len(groups) == 1 else []
        rule = rules[0] if len(rules) == 1 else {}
        pairs = rule.get('UserIdGroupPairs', [])
        if (len(groups) != 1 or groups[0].get('GroupId') != group_id
                or groups[0].get('VpcId') != req.vpc_id
                or rule.get('IpProtocol') != 'tcp' or rule.get('FromPort') != 5432
                or rule.get('ToPort') != 5432 or len(pairs) != 1
                or pairs[0].get('GroupId') != req.service_security_group
                or pairs[0].get('UserId', req.account) != req.account
                or pairs[0].get('VpcId', req.vpc_id) != req.vpc_id
                or rule.get('IpRanges', []) or rule.get('Ipv6Ranges', [])
                or rule.get('PrefixListIds', [])):
            raise AwsConfigurationError('PostgreSQL 보안 그룹의 인바운드 허용 범위가 예상과 다릅니다.')
        self.verify_execution_role(execution_role_arn, secret_arn)
        return {'stack_id': stack_id, 'database_arn': db_arn, 'database_id': req.database_id,
                'endpoint': outputs['EndpointAddress'], 'port': 5432,
                'secret_arn': secret_arn, 'database_security_group': group_id,
                'execution_role_arn': execution_role_arn,
                'service_security_group': req.service_security_group,
                'vpc_id': req.vpc_id, 'subnet_ids': list(req.subnet_ids),
                'status': 'available', 'deletion_protection': True, 'retained_on_stack_delete': True}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Provision a retained private RDS PostgreSQL instance')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create a billable RDS instance and managed secret')
    mode.add_argument('--inspect', action='store_true', help='Verify an existing database without changes')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    args = parser.parse_args(argv)
    try:
        req = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
        provisioner = AwsPostgresProvisioner(req)
        if args.inspect:
            result = provisioner.inspect_current()
        elif args.apply:
            print('RDS 생성 요청을 시작합니다. DB와 비밀은 앱 종료 시 자동 삭제하지 않습니다.', flush=True)
            result = provisioner.create()
        else:
            result = provisioner.preflight()
            print('읽기 전용 확인 완료. --apply 없이는 RDS 리소스를 생성하지 않습니다.', flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, AwsConfigurationError) as exc:
        parser.exit(2, f'PostgreSQL 사전 점검 실패: {exc}\n')


if __name__ == '__main__':
    main()
