"""AWS ECS Express Mode deployment with a managed CloudFormation base stack."""
from __future__ import annotations

import json
import http.client
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from onedeploy.analysis import redact
from onedeploy.core import ImageBuilder, validate_environment


class AwsConfigurationError(RuntimeError):
    retryable = False


@dataclass(frozen=True)
class AwsSettings:
    region: str = ''
    stack_name: str = 'onedeploy-core'

    @classmethod
    def from_environment(cls):
        region = os.getenv('ONEDEPLOY_AWS_REGION') or os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION')
        if not region and shutil.which('aws'):
            try:
                result = subprocess.run(['aws', 'configure', 'get', 'region'], capture_output=True, text=True, timeout=5)
                region = result.stdout.strip() if result.returncode == 0 else ''
            except (OSError, subprocess.TimeoutExpired):
                region = ''
        return cls(region or '')

    def validate(self):
        if not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', self.region):
            raise AwsConfigurationError('ONEDEPLOY_AWS_REGION에 유효한 AWS 리전을 설정하세요.')
        if self.stack_name != 'onedeploy-core':
            raise AwsConfigurationError('AWS 기반 스택 이름이 예상과 다릅니다.')

    def unavailable_reason(self):
        try:
            self.validate()
            if not shutil.which('aws'):
                return 'AWS CLI 설치와 로그인이 필요합니다.'
            if not shutil.which('docker'):
                return 'Docker CLI 설치가 필요합니다.'
        except AwsConfigurationError as exc:
            return str(exc)
        return None


class AwsExpressAdapter:
    def __init__(self, event, settings: AwsSettings, existing=None, checkpoint=None):
        self.output, self.settings = event, settings
        self.existing = existing
        self.checkpoint = checkpoint
        self.sensitive = []
        self.service_arn = None
        self.service_created = False
        self.image = None
        self.image_pushed = False
        self.image_built = False
        self.updated_existing = False
        self.previous_deployment_arn = None

    def event(self, stage, message):
        for value in sorted(set(self.sensitive), key=len, reverse=True):
            if value:
                message = message.replace(value, '[REDACTED]')
        self.output(stage, redact(message))

    def command(self, args, timeout=300, stdin=None, private=False, quiet=False):
        if not quiet:
            self.event('command', ' '.join(args))
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        if not private and not quiet:
            if result.stdout:
                self.event('output', result.stdout[-12000:])
            if result.stderr:
                self.event('output', result.stderr[-12000:])
        if result.returncode:
            if args[0] == 'docker' and 'push' in args and any(
                    marker in (result.stderr + result.stdout).lower()
                    for marker in ('timeout awaiting response headers', 'i/o timeout', 'connection reset')):
                raise AwsConfigurationError('ECR 이미지 업로드 연결이 시간 초과됐습니다.')
            raise AwsConfigurationError(f'{args[0]} 명령 실패 (종료 코드 {result.returncode}). AWS 권한·리전·계정 설정을 확인하세요.')
        return result.stdout.strip()

    def aws(self, args, **kwargs):
        return self.command(['aws', *args, '--region', self.settings.region, '--no-cli-pager', '--output', 'json'], **kwargs)

    def prepare_infrastructure(self):
        self.settings.validate()
        if not shutil.which('aws') or not shutil.which('docker'):
            raise AwsConfigurationError('AWS CLI와 Docker CLI가 필요합니다.')
        account = json.loads(self.aws(['sts', 'get-caller-identity'], private=True)).get('Account', '')
        if not re.fullmatch(r'\d{12}', account):
            raise AwsConfigurationError('AWS 계정 ID를 확인하지 못했습니다.')
        template = Path(__file__).parent / 'infra' / 'aws-ecs-express.yaml'
        self.event('infrastructure', 'CloudFormation으로 ECR 저장소와 ECS Express 역할 준비')
        self.aws(['cloudformation', 'deploy', '--template-file', str(template), '--stack-name', self.settings.stack_name,
                  '--capabilities', 'CAPABILITY_IAM', '--tags', 'onedeploy-managed=true'], timeout=900)
        described = json.loads(self.aws(['cloudformation', 'describe-stacks', '--stack-name', self.settings.stack_name], private=True))
        stacks = described.get('Stacks', [])
        if len(stacks) != 1 or stacks[0].get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            raise AwsConfigurationError('OneDeploy 기반 스택이 준비 상태가 아닙니다.')
        outputs = {item['OutputKey']: item['OutputValue'] for item in stacks[0].get('Outputs', [])}
        repository = outputs.get('RepositoryUri', '')
        execution = outputs.get('ExecutionRoleArn', '')
        infrastructure = outputs.get('InfrastructureRoleArn', '')
        if repository != f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/onedeploy-managed':
            raise AwsConfigurationError('ECR 저장소 URI가 예상과 다릅니다.')
        if not re.fullmatch(r'arn:aws:iam::' + account + r':role/[A-Za-z0-9_+=,.@/-]+', execution):
            raise AwsConfigurationError('ECS 실행 역할 ARN이 예상과 다릅니다.')
        if not re.fullmatch(r'arn:aws:iam::' + account + r':role/[A-Za-z0-9_+=,.@/-]+', infrastructure):
            raise AwsConfigurationError('ECS 인프라 역할 ARN이 예상과 다릅니다.')
        return account, repository, execution, infrastructure

    def deploy(self, project, plan, attempt_id, environment=None):
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('Invalid deployment attempt ID')
        if plan.target != 'aws-ecs-express':
            raise ValueError('AWS ECS Express adapter requires an aws-ecs-express plan')
        environment = validate_environment(environment, plan.required_env)
        self.sensitive.extend(environment.values())
        account, repository, execution, infrastructure = self.prepare_infrastructure()
        self.image = f'{repository}:{attempt_id}'
        owner_attempt = attempt_id
        service = f'onedeploy-{attempt_id}'
        previous_images = []
        previous_deployment = None
        previous_task_definition = None
        if self.existing is not None:
            prior = self.existing
            owner_attempt = prior.get('owner_attempt') or prior.get('service', '').removeprefix('onedeploy-')
            service = f'onedeploy-{owner_attempt}'
            expected_arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
            if (not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                    or prior.get('service') != service or prior.get('service_arn') != expected_arn
                    or prior.get('account') != account or prior.get('region') != self.settings.region
                    or prior.get('target') != 'aws-ecs-express'):
                raise AwsConfigurationError('기존 AWS 배포의 리소스 정보가 예상과 다릅니다.')
            self.validate_url(prior.get('url', ''), service, self.settings.region)
            described = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                                              '--service-arn', expected_arn, '--include', 'TAGS'], private=True))['service']
            tags = {item['key']: item['value'] for item in described.get('tags', [])}
            images = [config.get('primaryContainer', {}).get('image')
                      for config in described.get('activeConfigurations', [])]
            if (described.get('serviceArn') != expected_arn
                    or described.get('status', {}).get('statusCode') != 'ACTIVE'
                    or tags.get('onedeploy-managed') != 'true'
                    or tags.get('onedeploy-attempt') != owner_attempt
                    or prior.get('image') not in images):
                raise AwsConfigurationError('기존 AWS 서비스의 소유권 또는 실행 이미지가 변경됐습니다.')
            previous_configs = [config for config in described.get('activeConfigurations', [])
                                if config.get('primaryContainer', {}).get('image') == prior['image']]
            if len(previous_configs) != 1:
                raise AwsConfigurationError('이전 ECS 실행 구성을 하나로 확인할 수 없습니다.')
            previous_task_definition = previous_configs[0].get('taskDefinitionArn')
            deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                               '--service', service], private=True, quiet=True))
            items = deployments.get('serviceDeployments', [])
            latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
            previous_deployment = latest.get('serviceDeploymentArn')
            self.previous_deployment_arn = previous_deployment
            if not previous_deployment or latest.get('status') not in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}:
                raise AwsConfigurationError('이전 ECS 배포가 아직 완료되지 않았습니다. 잠시 후 다시 시도하세요.')
            previous_images = prior.get('images') or [prior['image']]
            if not isinstance(previous_images, list) or prior['image'] not in previous_images:
                raise AwsConfigurationError('기존 이미지 이력이 올바르지 않습니다.')
            self.service_arn = expected_arn
        ImageBuilder(self.command, self.event).build(project, plan, self.image, platform='linux/amd64')
        self.image_built = True
        registry = repository.split('/')[0]
        password = self.aws(['ecr', 'get-login-password'], private=True)
        if not password:
            raise AwsConfigurationError('ECR 로그인 암호를 받지 못했습니다.')
        self.sensitive.append(password)
        endpoint = os.getenv('DOCKER_HOST') or self.command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], private=True)
        with tempfile.TemporaryDirectory(prefix='onedeploy-aws-docker-auth-') as config:
            docker = ['docker', '--config', config, '--host', endpoint]
            self.command(docker + ['login', '--username', 'AWS', '--password-stdin', registry], stdin=password, private=True)
            self.image_pushed = True
            self.event('uploading', '이미지를 ECR에 업로드합니다.')
            for push_attempt in range(3):
                try:
                    self.command(docker + ['push', self.image], timeout=600)
                    break
                except AwsConfigurationError as exc:
                    if '시간 초과' not in str(exc) or push_attempt == 2:
                        raise
                    self.event('retry', f'ECR 업로드 시간 초과, {push_attempt + 2}/3 재시도')
                    time.sleep(5)
        payload = {'healthCheckPath': plan.health_path,
                   'primaryContainer': {'image': self.image, 'containerPort': plan.port,
                                        'environment': [{'name': 'PORT', 'value': str(plan.port)}] +
                                                       [{'name': key, 'value': value} for key, value in environment.items()]}}
        if self.existing is None:
            payload.update({'serviceName': service, 'executionRoleArn': execution,
                            'infrastructureRoleArn': infrastructure,
                            'scalingTarget': {'minTaskCount': 1, 'maxTaskCount': 1},
                            'tags': [{'key': 'onedeploy-managed', 'value': 'true'},
                                     {'key': 'onedeploy-attempt', 'value': attempt_id}]})
        else:
            payload['serviceArn'] = self.service_arn
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='onedeploy-aws-service-', encoding='utf-8') as spec:
            json.dump(payload, spec)
            spec.flush()
            if self.existing is None:
                self.event('deploying', 'ECS Express Mode 서비스를 생성합니다.')
                self.service_arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
                self.service_created = True
                operation = 'create-express-gateway-service'
            else:
                self.event('deploying', '기존 ECS Express Mode 서비스에 새 리비전을 배포합니다.')
                operation = 'update-express-gateway-service'
            if self.existing is not None:
                if self.checkpoint:
                    self.checkpoint(aws_update_submitted=True,
                                    aws_update_submitted_at=datetime.now(timezone.utc).isoformat(),
                                    aws_previous_deployment_arn=self.previous_deployment_arn,
                                    aws_candidate_image=self.image)
                self.updated_existing = True
                self.event('update_submitting', 'AWS 기존 서비스 업데이트 명령을 호출합니다.')
            created = json.loads(self.aws(['ecs', operation, '--cli-input-json', 'file://' + spec.name],
                                         timeout=600, private=True))
        if created.get('service', {}).get('serviceArn') != self.service_arn:
            raise AwsConfigurationError('ECS 서비스 ARN이 예상과 다릅니다. 생성된 리소스를 확인하세요.')
        if self.updated_existing:
            self.event('update_accepted', 'AWS가 기존 서비스 업데이트 요청을 수락했습니다.')
        ready = None
        new_deployment = created.get('service', {}).get('currentDeployment')
        last_deployment_status = None
        for _ in range(360 if self.existing is not None else 180):
            described = json.loads(self.aws(['ecs', 'describe-express-gateway-service', '--service-arn', self.service_arn], private=True, quiet=True))
            ready = described.get('service', {})
            state = ready.get('status', {}).get('statusCode')
            paths = [path.get('endpoint') for config in ready.get('activeConfigurations', [])
                     for path in config.get('ingressPaths', [])
                     if path.get('accessType') == 'PUBLIC' and path.get('endpoint')
                     and (self.existing is None or config.get('primaryContainer', {}).get('image') == self.image)]
            deployment_ready = False
            deployment_arn = ready.get('currentDeployment') or new_deployment
            if not deployment_arn:
                deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                                   '--service', service], private=True, quiet=True))
                items = deployments.get('serviceDeployments', [])
                latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
                deployment_arn = latest.get('serviceDeploymentArn')
            if deployment_arn and (self.existing is None or deployment_arn != previous_deployment):
                new_deployment = deployment_arn
                deployments = json.loads(self.aws(['ecs', 'describe-service-deployments',
                                                   '--service-deployment-arns', deployment_arn], private=True, quiet=True))
                items = deployments.get('serviceDeployments', [])
                deployment_status = items[0].get('status') if len(items) == 1 else None
                if deployment_status and deployment_status != last_deployment_status:
                    self.event('deploying', 'ECS 새 리비전 상태: ' + deployment_status)
                    last_deployment_status = deployment_status
                deployment_ready = deployment_status == 'SUCCESSFUL'
                if deployment_status in {'STOPPED', 'ROLLBACK_SUCCESSFUL', 'ROLLBACK_FAILED'}:
                    raise RuntimeError(f'ECS 새 리비전 배포에 실패했습니다: {deployment_status}')
            active = ready.get('activeConfigurations', [])
            settled = (not ready.get('currentDeployment') and len(active) == 1
                       and active[0].get('primaryContainer', {}).get('image') == self.image)
            if state == 'ACTIVE' and paths and deployment_ready and settled:
                break
            if state in {'FAILED', 'INACTIVE'}:
                raise RuntimeError('ECS Express 서비스 준비에 실패했습니다: ' + ready.get('status', {}).get('statusReason', '')[:300])
            time.sleep(5)
        else:
            raise RuntimeError('ECS Express 서비스 준비 시간 제한을 초과했습니다.')
        endpoint = paths[0].rstrip('/')
        url = endpoint if endpoint.startswith('https://') else 'https://' + endpoint
        self.validate_url(url, service, self.settings.region)
        if self.existing is not None and url != self.existing['url'].rstrip('/'):
            raise AwsConfigurationError('기존 ECS 서비스의 공개 URL이 변경됐습니다.')
        self.event('verifying', f'ECS Express HTTPS URL에서 실제 HTTP 응답 확인: {url}')
        self.verify(url + plan.health_path)
        active_configs = [config for config in ready.get('activeConfigurations', [])
                          if config.get('primaryContainer', {}).get('image') == self.image]
        task_definition = active_configs[0].get('taskDefinitionArn') if len(active_configs) == 1 else None
        expected_task_prefix = f'arn:aws:ecs:{self.settings.region}:{account}:task-definition/'
        if (not isinstance(task_definition, str) or not task_definition.startswith(expected_task_prefix)
                or not re.fullmatch(r'[A-Za-z0-9_-]+:\d+', task_definition.removeprefix(expected_task_prefix))):
            raise AwsConfigurationError('배포된 ECS 태스크 정의 ARN을 확인할 수 없습니다.')
        return {'url': url, 'health_url': url + plan.health_path, 'service': service,
                'service_arn': self.service_arn, 'image': self.image, 'target': 'aws-ecs-express',
                'region': self.settings.region, 'account': account, 'public': True,
                'owner_attempt': owner_attempt, 'images': [*previous_images, self.image],
                'task_definition_arn': task_definition,
                'previous_task_definition_arn': previous_task_definition}

    @staticmethod
    def validate_url(url, service, region):
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ''
        label = hostname.removesuffix(f'.ecs.{region}.on.aws')
        if (parsed.scheme != 'https' or hostname == label
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', label)
                or parsed.username or parsed.password or parsed.port or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise AwsConfigurationError('ECS Express가 예상하지 못한 서비스 URL을 반환했습니다.')

    def verify(self, url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        last_error = '응답 없음'
        for attempt in range(300):
            try:
                with opener.open(url, timeout=5) as response:
                    if response.status == 200:
                        return
                    last_error = f'HTTP {response.status}'
            except urllib.error.HTTPError as exc:
                last_error = f'HTTP {exc.code}'
            except urllib.error.URLError as exc:
                last_error = str(exc)[:200]
                if isinstance(exc.reason, socket.gaierror):
                    status = self.probe_with_dns_fallback(url)
                    if status == 200:
                        return
                    if status is not None:
                        last_error = f'HTTP {status}'
            except OSError as exc:
                last_error = str(exc)[:200]
            if attempt % 10 == 9:
                self.event('verifying', f'공개 주소 준비 대기 중 ({attempt + 1}/300): {last_error}')
            time.sleep(3)
        raise RuntimeError(f'ECS Express 앱이 HTTP 200을 반환하지 않았습니다: {last_error}')

    @staticmethod
    def probe_with_dns_fallback(url):
        """Bypass stale local DNS only; keep TLS SNI and hostname validation."""
        if not shutil.which('dig'):
            return None
        parsed = urllib.parse.urlsplit(url)
        addresses = []
        for resolver in (None, '@1.1.1.1', '@8.8.8.8'):
            args = ['dig', '+short', '+time=2', '+tries=1']
            if resolver:
                args.append(resolver)
            try:
                answer = subprocess.run([*args, parsed.hostname, 'A'], capture_output=True,
                                        text=True, timeout=5, check=True)
            except (OSError, subprocess.SubprocessError):
                continue
            for line in answer.stdout.splitlines():
                try:
                    address = ipaddress.ip_address(line.strip())
                except ValueError:
                    continue
                if address.version == 4 and address.is_global:
                    addresses.append(str(address))
            if addresses:
                break
        last_status = None
        for address in addresses[:4]:
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=5, context=ssl.create_default_context())
            connection._create_connection = lambda _target, timeout, source_address=None: socket.create_connection(
                (address, 443), timeout, source_address)
            try:
                connection.request('GET', parsed.path or '/')
                last_status = connection.getresponse().status
                if last_status == 200:
                    return 200
            except (OSError, ssl.SSLError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        return last_status

