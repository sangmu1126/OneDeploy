"""Opt-in one-shot ECS Fargate runner for checked PostgreSQL migrations."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from onedeploy.aws import AwsConfigurationError, AwsExpressAdapter
from onedeploy.migrations import MigrationBundle, stage_migrator_context
from onedeploy.postgres import PostgresRequest


class AwsMigrationRunner:
    def __init__(self, adapter: AwsExpressAdapter, request: PostgresRequest,
                 database: dict, bundle: MigrationBundle, repository: str, attempt_id: str):
        request.validate()
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('마이그레이션 배포 시도 ID가 올바르지 않습니다.')
        if database.get('service_security_group') != request.service_security_group:
            raise AwsConfigurationError('마이그레이션 서비스 보안 그룹이 DB 기록과 다릅니다.')
        if repository != f'{request.account}.dkr.ecr.{request.region}.amazonaws.com/onedeploy-managed':
            raise AwsConfigurationError('마이그레이션 ECR 저장소가 예상과 다릅니다.')
        if database.get('migration_log_group') != f'/onedeploy/migrations/{request.application_id}':
            raise AwsConfigurationError('마이그레이션 로그 그룹이 DB 기록과 다릅니다.')
        self.adapter, self.request, self.database = adapter, request, database
        self.bundle, self.repository, self.attempt_id = bundle, repository, attempt_id
        self.image = f'{repository}:{attempt_id}-db'
        self.family = f'onedeploy-migrate-{attempt_id}'
        self.subnet = None
        self.registered_arn = None
        self.task_arn = None
        self.completed = False

    def preflight(self) -> str:
        """Require a default-VPC public subnet for image pull and Secrets Manager access."""
        req = self.request
        response = json.loads(self.adapter.aws(['ec2', 'describe-route-tables', '--filters',
            f'Name=vpc-id,Values={req.vpc_id}'], private=True, quiet=True))
        tables = response.get('RouteTables', [])
        main = [table for table in tables if any(association.get('Main') is True
                for association in table.get('Associations', []))]
        if len(main) != 1:
            raise AwsConfigurationError('마이그레이션용 기본 VPC 라우팅 테이블을 확인하지 못했습니다.')
        for subnet in req.subnet_ids:
            associated = [table for table in tables if any(association.get('SubnetId') == subnet
                          for association in table.get('Associations', []))]
            if len(associated) > 1:
                raise AwsConfigurationError('마이그레이션 서브넷의 라우팅 테이블이 중복됩니다.')
            table = associated[0] if associated else main[0]
            if any(route.get('DestinationCidrBlock') == '0.0.0.0/0'
                   and re.fullmatch(r'igw-[a-f0-9]{8,17}', route.get('GatewayId', ''))
                   and route.get('State') == 'active' for route in table.get('Routes', [])):
                self.subnet = subnet
                break
        if not self.subnet:
            raise AwsConfigurationError('일회성 ECS 태스크가 이미지를 받을 수 있는 공개 서브넷이 필요합니다.')
        logs = json.loads(self.adapter.aws(['logs', 'describe-log-groups', '--log-group-name-prefix',
            self.database['migration_log_group']], private=True, quiet=True)).get('logGroups', [])
        if (len(logs) != 1 or logs[0].get('logGroupName') != self.database['migration_log_group']
                or logs[0].get('retentionInDays') != 14):
            raise AwsConfigurationError('마이그레이션 로그 그룹의 이름·보존 기간이 예상과 다릅니다.')
        return self.subnet

    def task_definition(self) -> dict:
        req, db = self.request, self.database
        return {'family': self.family, 'networkMode': 'awsvpc',
                'requiresCompatibilities': ['FARGATE'], 'cpu': '256', 'memory': '512',
                'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
                'executionRoleArn': db['execution_role_arn'],
                'containerDefinitions': [{'name': 'migration', 'image': self.image, 'essential': True,
                    'environment': [
                        {'name': 'PGHOST', 'value': db['endpoint']},
                        {'name': 'PGPORT', 'value': str(db['port'])},
                        {'name': 'PGDATABASE', 'value': 'appdb'},
                        {'name': 'PGSSLMODE', 'value': 'require'}],
                    'secrets': [
                        {'name': 'PGUSER', 'valueFrom': db['secret_arn'] + ':username::'},
                        {'name': 'PGPASSWORD', 'valueFrom': db['secret_arn'] + ':password::'}],
                    'logConfiguration': {'logDriver': 'awslogs', 'options': {
                        'awslogs-group': db['migration_log_group'], 'awslogs-region': req.region,
                        'awslogs-stream-prefix': 'migration'}}}],
                'tags': [{'key': 'onedeploy-managed', 'value': 'true'},
                         {'key': 'onedeploy-app', 'value': req.application_id},
                         {'key': 'onedeploy-attempt', 'value': self.attempt_id}]}

    def run_task(self) -> dict:
        if not self.subnet:
            raise AwsConfigurationError('마이그레이션 네트워크 사전 점검이 필요합니다.')
        self.completed = False
        req = self.request
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='onedeploy-migrate-task-',
                                         encoding='utf-8') as file:
            json.dump(self.task_definition(), file)
            file.flush()
            registered = json.loads(self.adapter.aws(['ecs', 'register-task-definition',
                '--cli-input-json', 'file://' + file.name], private=True))
        definition = registered.get('taskDefinition', {})
        prefix = f'arn:aws:ecs:{req.region}:{req.account}:task-definition/{self.family}:'
        arn = definition.get('taskDefinitionArn', '')
        if (not isinstance(arn, str) or not arn.startswith(prefix)
                or not arn.removeprefix(prefix).isdigit()):
            raise AwsConfigurationError('마이그레이션 태스크 정의 ARN을 확인하지 못했습니다.')
        self.registered_arn = arn
        payload = {'cluster': 'default', 'launchType': 'FARGATE', 'count': 1,
                   'taskDefinition': arn,
                   'networkConfiguration': {'awsvpcConfiguration': {
                       'subnets': [self.subnet],
                       'securityGroups': [req.service_security_group],
                       'assignPublicIp': 'ENABLED'}},
                   'tags': [{'key': 'onedeploy-managed', 'value': 'true'},
                            {'key': 'onedeploy-app', 'value': req.application_id},
                            {'key': 'onedeploy-attempt', 'value': self.attempt_id}]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='onedeploy-migrate-run-',
                                         encoding='utf-8') as file:
            json.dump(payload, file)
            file.flush()
            launched = json.loads(self.adapter.aws(['ecs', 'run-task', '--cli-input-json',
                'file://' + file.name], private=True))
        tasks = launched.get('tasks', [])
        task_arn = tasks[0].get('taskArn', '') if len(tasks) == 1 else ''
        expected = f'arn:aws:ecs:{req.region}:{req.account}:task/default/'
        if (launched.get('failures') or not isinstance(task_arn, str)
                or not task_arn.startswith(expected) or len(tasks) != 1):
            raise AwsConfigurationError(f'마이그레이션 태스크 시작 상태를 확인하지 못했습니다. {arn}을 확인하세요.')
        self.task_arn = task_arn
        try:
            self.adapter.aws(['ecs', 'wait', 'tasks-stopped', '--cluster', 'default', '--tasks', task_arn],
                             timeout=900, private=True, quiet=True)
            described = json.loads(self.adapter.aws(['ecs', 'describe-tasks', '--cluster', 'default',
                '--tasks', task_arn, '--include', 'TAGS'], private=True, quiet=True)).get('tasks', [])
        except Exception as exc:
            raise AwsConfigurationError(f'마이그레이션 종료를 확인하지 못했습니다. 태스크 {task_arn}과 정의 {arn}을 확인하세요: {exc}') from None
        task = described[0] if len(described) == 1 else {}
        tags = {item.get('key'): item.get('value') for item in task.get('tags', [])}
        containers = task.get('containers', [])
        if (task.get('taskArn') != task_arn or task.get('taskDefinitionArn') != arn
                or task.get('lastStatus') != 'STOPPED'
                or tags.get('onedeploy-managed') != 'true'
                or tags.get('onedeploy-app') != req.application_id
                or tags.get('onedeploy-attempt') != self.attempt_id
                or len(containers) != 1 or containers[0].get('name') != 'migration'):
            raise AwsConfigurationError(f'마이그레이션 태스크의 종료·소유 상태를 확인하지 못했습니다: {task_arn}')
        if containers[0].get('exitCode') != 0:
            raise AwsConfigurationError(f'마이그레이션이 실패했습니다. 태스크 {task_arn}와 CloudWatch 로그 '
                                        f'{self.database["migration_log_group"]}을 확인하세요.')
        self.completed = True
        return {'task_arn': task_arn, 'task_definition_arn': arn,
                'image': self.image, 'bundle_digest': self.bundle.digest,
                'log_group': self.database['migration_log_group']}

    def cleanup_completed(self) -> bool:
        """Delete only the known stopped task's definition and migration image tag."""
        if not self.completed or not self.registered_arn or not self.task_arn:
            return False
        deregistered = json.loads(self.adapter.aws(['ecs', 'deregister-task-definition',
            '--task-definition', self.registered_arn], private=True))
        definition = deregistered.get('taskDefinition', {})
        if (definition.get('taskDefinitionArn') != self.registered_arn
                or definition.get('status') != 'INACTIVE'):
            raise AwsConfigurationError('마이그레이션 태스크 정의 정리를 확인하지 못했습니다.')
        tag = self.attempt_id + '-db'
        removed = json.loads(self.adapter.aws(['ecr', 'batch-delete-image', '--repository-name',
            'onedeploy-managed', '--image-ids', f'imageTag={tag}'], private=True))
        if (removed.get('failures') or not any(item.get('imageTag') == tag
                                             for item in removed.get('imageIds', []))):
            raise AwsConfigurationError('마이그레이션 ECR 이미지 태그 정리를 확인하지 못했습니다.')
        self.completed = False
        return True

    def build_and_run(self) -> dict:
        if not self.subnet:
            self.preflight()
        with tempfile.TemporaryDirectory(prefix='onedeploy-migrate-build-') as directory:
            context = stage_migrator_context(self.bundle, Path(directory) / 'context')
            self.adapter.command(['docker', 'build', '--platform', 'linux/amd64', '-t', self.image,
                                  str(context)], timeout=900)
        password = self.adapter.aws(['ecr', 'get-login-password'], private=True)
        if not password:
            raise AwsConfigurationError('마이그레이션 이미지용 ECR 로그인 암호를 받지 못했습니다.')
        self.adapter.sensitive.append(password)
        endpoint = os.getenv('DOCKER_HOST') or self.adapter.command(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], private=True)
        with tempfile.TemporaryDirectory(prefix='onedeploy-migrate-docker-auth-') as config:
            docker = ['docker', '--config', config, '--host', endpoint]
            self.adapter.command(docker + ['login', '--username', 'AWS', '--password-stdin',
                                           self.repository.split('/')[0]], stdin=password, private=True)
            self.adapter.command(docker + ['push', self.image], timeout=600)
        self.adapter.event('migrating', '검증된 SQL 마이그레이션을 일회성 ECS 태스크로 실행합니다.')
        try:
            result = self.run_task()
        except Exception:
            self.adapter.event('cleanup', '마이그레이션 결과가 불확실합니다. ECS 태스크·정의와 ECR 이미지 '
                               + self.image + '를 직접 확인하세요.')
            raise
        try:
            result['cleanup_complete'] = self.cleanup_completed()
        except AwsConfigurationError as exc:
            result['cleanup_complete'] = False
            self.adapter.event('cleanup', str(exc) + ' 리소스: ' + self.image)
        return result
