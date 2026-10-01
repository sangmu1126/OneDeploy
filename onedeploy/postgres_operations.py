"""Persistent, opt-in PostgreSQL creation requests; never retry an uncertain create."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from onedeploy.aws import AwsConfigurationError, AwsSettings
from onedeploy.postgres import AwsPostgresProvisioner, PostgresRequest


class PostgresOperations:
    def __init__(self, root: Path, settings: AwsSettings):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.settings = settings
        self.lock = threading.Lock()
        self.plans: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.recovery_warnings: list[str] = []
        for path in self.root.glob('*.json'):
            try:
                operation = json.loads(path.read_text())
                request = PostgresRequest(**{**operation['request'],
                    'subnet_ids': tuple(operation['request']['subnet_ids'])})
                request.validate()
                if (path.stem != request.application_id
                        or request.account != settings.expected_account
                        or request.region != settings.region
                        or (settings.service_security_group and
                            request.service_security_group != settings.service_security_group)
                        or operation['status'] not in {'running', 'succeeded', 'needs_attention'}):
                    raise ValueError('Unexpected database operation')
                if operation['status'] == 'running':
                    operation['status'] = 'needs_attention'
                    operation['message'] = '서버 재시작으로 생성 결과가 불확실합니다. AWS 상태를 재확인하세요.'
                    self._save(operation)
                self.operations[request.application_id] = operation
            except (OSError, ValueError, KeyError, TypeError, AwsConfigurationError):
                self.recovery_warnings.append('PostgreSQL 생성 기록을 불러오지 못했습니다: ' + path.stem)

    def _save(self, operation: dict) -> None:
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.postgres-',
                                         suffix='.json', delete=False) as file:
            try:
                json.dump(operation, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                temporary = Path(file.name)
            except Exception:
                Path(file.name).unlink(missing_ok=True)
                raise
        os.replace(temporary, self.root / (operation['application_id'] + '.json'))

    def plan(self, request: PostgresRequest) -> dict:
        request.validate()
        if (request.account != self.settings.expected_account or request.region != self.settings.region
                or (self.settings.service_security_group and
                    request.service_security_group != self.settings.service_security_group)):
            raise AwsConfigurationError('RDS 생성 대상이 서버의 AWS 설정과 다릅니다.')
        with self.lock:
            if request.application_id in self.operations:
                raise ValueError('이 앱에는 이미 PostgreSQL 생성 기록이 있습니다. 먼저 생성 상태를 확인하세요.')
        provisioner = AwsPostgresProvisioner(request)
        result = provisioner.preflight()
        provisioner.assert_stack_available()
        token = secrets.token_urlsafe(24)
        with self.lock:
            if request.application_id in self.operations:
                raise ValueError('이 앱에는 이미 PostgreSQL 생성 기록이 있습니다.')
            self.plans[request.application_id] = {'id': token, 'request': request,
                                                   'result': result, 'expires': time.monotonic() + 900}
        return {**result, 'plan_id': token}

    def start(self, application_id: str, plan_id: str) -> dict:
        with self.lock:
            plan = self.plans.get(application_id)
            if (not plan or plan['id'] != plan_id or time.monotonic() > plan['expires']):
                raise ValueError('생성 계획이 없거나 만료됐습니다. 가격을 다시 확인하세요.')
            if application_id in self.operations:
                raise ValueError('이 앱의 PostgreSQL 생성 요청이 이미 기록돼 있습니다.')
            request, expected = plan['request'], plan['result']
        # Recheck account, network, and current price before writing an operation.
        provisioner = AwsPostgresProvisioner(request)
        current = provisioner.preflight()
        if current != expected:
            raise ValueError('생성 계획이 변경됐습니다. 가격을 다시 확인하세요.')
        provisioner.assert_stack_available()
        operation = {'application_id': application_id, 'request': asdict(request),
                     'expected_plan': expected, 'status': 'running',
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'message': 'RDS 스택 생성과 완료 확인을 진행 중입니다.'}
        with self.lock:
            if application_id in self.operations:
                raise ValueError('이 앱의 PostgreSQL 생성 요청이 이미 기록돼 있습니다.')
            self._save(operation)
            self.operations[application_id] = operation
            del self.plans[application_id]
        try:
            threading.Thread(target=self._run, args=(application_id,), daemon=True).start()
        except Exception:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '생성 작업을 시작하지 못했습니다. AWS 상태를 재확인하세요.'
                self._save(operation)
            raise
        return self.get(application_id)

    def _run(self, application_id: str) -> None:
        with self.lock:
            operation = self.operations[application_id]
            request = PostgresRequest(**{**operation['request'],
                'subnet_ids': tuple(operation['request']['subnet_ids'])})
            expected = operation['expected_plan']
        try:
            database = AwsPostgresProvisioner(request).create(expected_plan=expected)
        except Exception as exc:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = 'RDS 생성 결과를 확인하지 못했습니다: ' + str(exc)
                self._save(operation)
            return
        with self.lock:
            operation['status'] = 'succeeded'
            operation['message'] = '기존 RDS 조회 후 앱 배포를 진행할 수 있습니다.'
            operation['database_id'] = database['database_id']
            self._save(operation)

    def get(self, application_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(application_id)
            if not operation:
                raise ValueError('이 앱의 PostgreSQL 생성 요청 기록이 없습니다.')
            request = operation['request']
            return {'application_id': application_id, 'status': operation['status'],
                    'message': operation['message'], 'created_at': operation['created_at'],
                    'account': request['account'], 'region': request['region'],
                    'vpc_id': request['vpc_id'], 'subnet_ids': request['subnet_ids'],
                    'database_id': operation.get('database_id'),
                    'baseline_730h_usd': operation['expected_plan']['pricing']['baseline_730h_usd']}

    def reconcile(self, application_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(application_id)
            if not operation:
                raise ValueError('이 앱의 PostgreSQL 생성 요청 기록이 없습니다.')
            if operation['status'] == 'running':
                raise ValueError('생성 작업이 실행 중입니다. 완료 후 다시 확인하세요.')
            request = PostgresRequest(**{**operation['request'],
                'subnet_ids': tuple(operation['request']['subnet_ids'])})
        provisioner = AwsPostgresProvisioner(request)
        try:
            identity = json.loads(provisioner.adapter.aws(['sts', 'get-caller-identity'],
                private=True, quiet=True))
            if identity.get('Account') != request.account:
                raise AwsConfigurationError('현재 AWS 계정이 생성 요청 계정과 다릅니다.')
            stacks = json.loads(provisioner.adapter.aws(['cloudformation', 'describe-stacks',
                '--stack-name', request.stack_name], private=True, quiet=True)).get('Stacks', [])
            prefix = f'arn:aws:cloudformation:{request.region}:{request.account}:stack/{request.stack_name}/'
            stack = stacks[0] if len(stacks) == 1 else {}
            tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
            if (not stack.get('StackId', '').startswith(prefix)
                    or tags.get('onedeploy-managed') != 'true'
                    or tags.get('onedeploy-app') != application_id):
                raise AwsConfigurationError('PostgreSQL 스택 소유권을 확인하지 못했습니다.')
            stack_status = stack.get('StackStatus', '')
            if not re.fullmatch(r'[A-Z_]+', stack_status):
                raise AwsConfigurationError('PostgreSQL 스택 상태를 확인하지 못했습니다.')
            database = provisioner.inspect_current() if stack_status == 'CREATE_COMPLETE' else None
            message = ('RDS 스택 생성이 완료됐습니다.' if database else
                       'CloudFormation 상태: ' + stack_status + '. AWS에서 확인 후 다시 재확인하세요.')
            with self.lock:
                operation = self.operations[application_id]
                if operation['status'] == 'running':
                    raise ValueError('생성 작업이 실행 중입니다.')
                operation['status'] = 'succeeded' if database else 'needs_attention'
                operation['message'] = message
                if database:
                    operation['database_id'] = database['database_id']
                self._save(operation)
        except (AwsConfigurationError, ValueError) as exc:
            with self.lock:
                operation = self.operations[application_id]
                operation['status'] = 'needs_attention'
                operation['message'] = 'AWS 생성 상태를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
        return self.get(application_id)
