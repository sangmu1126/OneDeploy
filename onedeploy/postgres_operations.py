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
        self.cleanup_plans: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.cleaned: set[str] = set()
        self.cleaned_records: dict[str, dict] = {}
        self.recovery_warnings: list[str] = []
        self.archive = self.root / 'cleaned-attempts'
        self.archive.mkdir(mode=0o700, exist_ok=True)
        self.archive.chmod(0o700)
        for path in self.archive.glob('*.json'):
            try:
                record = json.loads(path.read_text())
                request = record['request']
                if (record['status'] != 'failed_cleaned'
                        or path.stem != request['application_id'] + '-' + record['attempt_id']
                        or request['account'] != settings.expected_account
                        or request['region'] != settings.region
                        or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', request['application_id'])
                        or not re.fullmatch(r'[a-f0-9]{16}', record['attempt_id'])):
                    raise ValueError('Unexpected archive')
                self.cleaned.add(request['application_id'])
                previous = self.cleaned_records.get(request['application_id'])
                if not previous or record.get('cleaned_at', '') > previous.get('cleaned_at', ''):
                    self.cleaned_records[request['application_id']] = record
            except (OSError, ValueError, KeyError, TypeError):
                self.recovery_warnings.append('PostgreSQL 정리 기록을 불러오지 못했습니다: ' + path.stem)
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
                        or operation['status'] not in {'running', 'recovering', 'succeeded', 'needs_attention', 'failed_cleaned'}):
                    raise ValueError('Unexpected database operation')
                if operation['status'] == 'failed_cleaned':
                    os.replace(path, self.archive / (request.application_id + '-'
                        + operation['attempt_id'] + '.json'))
                    self.cleaned.add(request.application_id)
                    self.cleaned_records[request.application_id] = operation
                    continue
                if operation['status'] in {'running', 'recovering'}:
                    operation['status'] = 'needs_attention'
                    operation['message'] = '서버 재시작으로 AWS 작업 결과가 불확실합니다. 상태를 재확인하세요.'
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
        provisioner.assert_stack_available(allow_deleted=True) if request.application_id in self.cleaned else provisioner.assert_stack_available()
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
        provisioner.assert_stack_available(allow_deleted=True) if application_id in self.cleaned else provisioner.assert_stack_available()
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
            operation = self.operations.get(application_id) or self.cleaned_records.get(application_id)
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
            if operation['status'] in {'running', 'recovering'}:
                raise ValueError('PostgreSQL 작업이 실행 중입니다. 완료 후 다시 확인하세요.')
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
                if operation['status'] in {'running', 'recovering'}:
                    raise ValueError('PostgreSQL 작업이 실행 중입니다.')
                operation['status'] = 'succeeded' if database else 'needs_attention'
                operation['message'] = message
                if database:
                    operation['database_id'] = database['database_id']
                self._save(operation)
        except (AwsConfigurationError, ValueError) as exc:
            with self.lock:
                operation = self.operations[application_id]
                if operation['status'] == 'recovering':
                    raise ValueError('실패 스택 정리가 실행 중입니다.') from None
                operation['status'] = 'needs_attention'
                operation['message'] = 'AWS 생성 상태를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
        return self.get(application_id)

    def _inspect_failed_create(self, request: PostgresRequest, previous_stack_id: str | None = None) -> dict:
        """Fail closed if rollback left any resource, DB, snapshot, or ownership doubt."""
        adapter = AwsPostgresProvisioner(request).adapter
        def aws(args):
            return json.loads(adapter.aws(args, private=True, quiet=True))
        if aws(['sts', 'get-caller-identity']).get('Account') != request.account:
            raise AwsConfigurationError('AWS 계정이 생성 기록과 다릅니다.')
        stacks = aws(['cloudformation', 'describe-stacks', '--stack-name',
                      previous_stack_id or request.stack_name]).get('Stacks', [])
        stack = stacks[0] if len(stacks) == 1 else {}
        stack_id = stack.get('StackId', '')
        prefix = f'arn:aws:cloudformation:{request.region}:{request.account}:stack/{request.stack_name}/'
        tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
        status = stack.get('StackStatus')
        if (not isinstance(stack_id, str) or not stack_id.startswith(prefix)
                or (previous_stack_id and stack_id != previous_stack_id)
                or status not in {'ROLLBACK_COMPLETE', 'DELETE_COMPLETE'}
                or (status == 'DELETE_COMPLETE' and not previous_stack_id)
                or tags.get('onedeploy-managed') != 'true'
                or tags.get('onedeploy-app') != request.application_id):
            raise AwsConfigurationError('앱 소유 실패 스택의 종료 상태를 확인하지 못했습니다.')
        if status == 'ROLLBACK_COMPLETE':
            listed = aws(['cloudformation', 'list-stack-resources', '--stack-name', stack_id])
            resources = listed.get('StackResourceSummaries')
            if (listed.get('NextToken') or not isinstance(resources, list)
                    or not resources or any(not isinstance(item, dict)
                    or item.get('ResourceStatus') not in {'CREATE_FAILED', 'DELETE_COMPLETE'}
                    or (item.get('ResourceStatus') == 'CREATE_FAILED'
                        and item.get('PhysicalResourceId'))
                    for item in resources)):
                raise AwsConfigurationError('롤백 스택에 남거나 확인되지 않은 리소스가 있습니다.')
        databases = aws(['rds', 'describe-db-instances'])
        instances = databases.get('DBInstances')
        if (databases.get('Marker') or not isinstance(instances, list)
                or any(not isinstance(item, dict) or
                       item.get('DBInstanceIdentifier') == request.database_id for item in instances)):
            raise AwsConfigurationError('앱 DB가 없음을 완전히 확인하지 못했습니다.')
        snapshots = aws(['rds', 'describe-db-snapshots', '--snapshot-type', 'manual'])
        items = snapshots.get('DBSnapshots')
        if (snapshots.get('Marker') or not isinstance(items, list)
                or any(not isinstance(item, dict) or
                       item.get('DBInstanceIdentifier') == request.database_id for item in items)):
            raise AwsConfigurationError('앱 수동 스냅샷이 없음을 완전히 확인하지 못했습니다.')
        return {'stack_id': stack_id, 'stack_status': status, 'database_id': request.database_id,
                'account': request.account, 'region': request.region}

    def cleanup_plan(self, application_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(application_id)
            if not operation or operation['status'] != 'needs_attention':
                raise ValueError('재확인이 필요한 PostgreSQL 생성 기록이 없습니다.')
            request = PostgresRequest(**{**operation['request'],
                'subnet_ids': tuple(operation['request']['subnet_ids'])})
            previous = operation.get('recovery_stack_id')
        result = self._inspect_failed_create(request, previous)
        token = secrets.token_urlsafe(24)
        with self.lock:
            if self.operations.get(application_id) is not operation or operation['status'] != 'needs_attention':
                raise ValueError('생성 상태가 변경됐습니다. 다시 확인하세요.')
            self.cleanup_plans[application_id] = {'id': token, 'result': result,
                                                   'expires': time.monotonic() + 900}
        return {**result, 'plan_id': token,
                'message': '앱 DB와 수동 스냅샷이 없음을 확인했습니다. 이 실패 스택만 정리합니다.'}

    def cleanup_start(self, application_id: str, plan_id: str, confirm_stack_id: str) -> dict:
        with self.lock:
            plan = self.cleanup_plans.get(application_id)
            operation = self.operations.get(application_id)
            if (not plan or plan['id'] != plan_id or time.monotonic() > plan['expires']
                    or plan['result']['stack_id'] != confirm_stack_id
                    or not operation or operation['status'] != 'needs_attention'):
                raise ValueError('실패 스택 정리 계획이 없거나 확인 값이 다릅니다.')
            request = PostgresRequest(**{**operation['request'],
                'subnet_ids': tuple(operation['request']['subnet_ids'])})
        current = self._inspect_failed_create(request, operation.get('recovery_stack_id'))
        if current != plan['result']:
            raise ValueError('실패 스택 상태가 변경됐습니다. 계획을 다시 확인하세요.')
        with self.lock:
            if self.operations.get(application_id) is not operation or operation['status'] != 'needs_attention':
                raise ValueError('생성 상태가 변경됐습니다.')
            operation['status'] = 'recovering'
            operation['recovery_stack_id'] = confirm_stack_id
            operation['message'] = '실패한 RDS 스택을 정리 중입니다.'
            self._save(operation)
            del self.cleanup_plans[application_id]
        try:
            threading.Thread(target=self._cleanup_run, args=(application_id,), daemon=True).start()
        except Exception:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '정리 작업을 시작하지 못했습니다. AWS 상태를 재확인하세요.'
                self._save(operation)
            raise
        return self.get(application_id)

    def _cleanup_run(self, application_id: str) -> None:
        with self.lock:
            operation = self.operations[application_id]
            request = PostgresRequest(**{**operation['request'],
                'subnet_ids': tuple(operation['request']['subnet_ids'])})
            stack_id = operation['recovery_stack_id']
        try:
            checked = self._inspect_failed_create(request, stack_id)
            adapter = AwsPostgresProvisioner(request).adapter
            if checked['stack_status'] == 'ROLLBACK_COMPLETE':
                adapter.aws(['cloudformation', 'update-termination-protection',
                    '--no-enable-termination-protection', '--stack-name', stack_id],
                    private=True, quiet=True)
                adapter.aws(['cloudformation', 'delete-stack', '--stack-name', stack_id],
                            private=True, quiet=True)
                adapter.aws(['cloudformation', 'wait', 'stack-delete-complete',
                             '--stack-name', stack_id], timeout=900, private=True, quiet=True)
            checked = self._inspect_failed_create(request, stack_id)
            if checked['stack_status'] != 'DELETE_COMPLETE':
                raise AwsConfigurationError('실패 스택 삭제 완료를 확인하지 못했습니다.')
            with self.lock:
                operation['status'] = 'failed_cleaned'
                operation['message'] = '실패 스택 정리 완료. 같은 앱 ID로 새 생성 계획을 시작할 수 있습니다.'
                operation['cleaned_at'] = datetime.now(timezone.utc).isoformat()
                operation['attempt_id'] = secrets.token_hex(8)
                self._save(operation)
                os.replace(self.root / (application_id + '.json'),
                           self.archive / (application_id + '-' + operation['attempt_id'] + '.json'))
                self.cleaned.add(application_id)
                self.cleaned_records[application_id] = operation
                del self.operations[application_id]
        except Exception as exc:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '실패 스택 정리 결과를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
