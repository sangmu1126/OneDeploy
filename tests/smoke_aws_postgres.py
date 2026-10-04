"""Opt-in live DB data-path smoke using an existing, retained RDS stack."""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path

from onedeploy.aws import AwsExpressAdapter, AwsSettings
from onedeploy.core import analyze
from onedeploy.migrations import collect_sql_migrations
from onedeploy.postgres import AwsPostgresProvisioner, PostgresRequest


def probe(url: str, key: str, record_id: str, method: str) -> dict:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url + '/records/' + record_id, method=method,
                                     headers={'X-Probe-Key': key})
    last_error = None
    for _ in range(24):
        try:
            with opener.open(request, timeout=10) as response:
                body = json.load(response)
                expected = {'deleted': True} if method == 'DELETE' else {'value': record_id}
                if response.status == 200 and body == expected:
                    return body
                last_error = f'HTTP {response.status}: unexpected body'
        except (OSError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(5)
    raise AssertionError(f'PostgreSQL {method} probe failed: {last_error}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify ECS PostgreSQL write/read across a service update')
    parser.add_argument('--apply', action='store_true', help='Create temporary billable ECS services and images')
    parser.add_argument('--application', required=True, help='Existing OneDeploy PostgreSQL application ID')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--probe-runtime', choices=('node', 'python'), default='node',
                        help='Sample app runtime for the ECS data-path drill (default: node)')
    args = parser.parse_args(argv)
    request = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
    request.validate()
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True,
                           service_security_group=args.service_security_group)
    database = AwsPostgresProvisioner(request).inspect_current()
    print(json.dumps({'database_id': database['database_id'], 'stack_id': database['stack_id'],
                      'status': database['status']}, ensure_ascii=False), flush=True)
    if not args.apply:
        print('읽기 전용 DB 검증 완료. --apply 없이는 ECS 서비스나 이미지를 만들지 않습니다.', flush=True)
        return

    source = (Path(__file__).resolve().parents[1] / 'examples'
              / f'postgres-probe-{args.probe_runtime}')
    expected_runtime = {'node': 'custom-dockerfile', 'python': 'python-wsgi'}[args.probe_runtime]
    key = secrets.token_urlsafe(32)
    record_id = uuid.uuid4().hex
    first_attempt = uuid.uuid4().hex[:16] + '-a1'
    second_attempt = uuid.uuid4().hex[:16] + '-a1'
    first = AwsExpressAdapter(lambda stage, message: print(f'[v1:{stage}] {message}', flush=True), settings)
    second = None
    first_result = None
    second_result = None
    with tempfile.TemporaryDirectory(prefix='onedeploy-postgres-smoke-') as directory:
        first_project = Path(directory) / 'app-v1'
        second_project = Path(directory) / 'app-v2'
        shutil.copytree(source, first_project)
        shutil.copytree(source, second_project)
        plan = replace(analyze(first_project), target='aws-ecs-express', health_path='/health',
                       required_env=['PROBE_KEY'])
        if plan.runtime != expected_runtime:
            raise AssertionError(f'Unexpected v1 probe runtime: {plan.runtime}')
        migrations = collect_sql_migrations(first_project)
        try:
            first_result = first.deploy(first_project, plan, first_attempt, {'PROBE_KEY': key},
                                        postgres=request, migrations=migrations)
            if (first_result.get('migration', {}).get('bundle_digest') != migrations.digest
                    or first_result['migration'].get('cleanup_complete') is not True):
                raise AssertionError('First ECS release did not complete the checked migration')
            probe(first_result['url'], key, record_id, 'POST')
            probe(first_result['url'], key, record_id, 'GET')
            # Keep each release input immutable and re-check its runtime/TLS profile.
            plan = replace(analyze(second_project), target='aws-ecs-express', health_path='/health',
                           required_env=['PROBE_KEY'])
            if plan.runtime != expected_runtime:
                raise AssertionError(f'Unexpected v2 probe runtime: {plan.runtime}')
            second_migrations = collect_sql_migrations(second_project)
            if second_migrations.digest != migrations.digest:
                raise AssertionError('Probe migration changed between ECS releases')
            second = AwsExpressAdapter(lambda stage, message: print(f'[v2:{stage}] {message}', flush=True),
                                       settings, existing=first_result)
            second_result = second.deploy(second_project, plan, second_attempt, {'PROBE_KEY': key},
                                          postgres=request, migrations=second_migrations)
            if (second_result.get('migration', {}).get('bundle_digest') != second_migrations.digest
                    or second_result['migration'].get('cleanup_complete') is not True):
                raise AssertionError('Updated ECS release did not confirm migration idempotency')
            if (second_result['url'] != first_result['url']
                    or second_result['service_arn'] != first_result['service_arn']
                    or second_result['image'] == first_result['image']):
                raise AssertionError('ECS service update did not preserve the URL and replace the image')
            probe(second_result['url'], key, record_id, 'GET')
            probe(second_result['url'], key, record_id, 'DELETE')
        finally:
            original_error = sys.exc_info()[1]
            try:
                if first_result:
                    results = [item for item in (first_result, second_result) if item]
                    for _ in range(180):
                        service = json.loads(first.aws(['ecs', 'describe-express-gateway-service',
                            '--service-arn', first_result['service_arn']], private=True, quiet=True))['service']
                        active = service.get('activeConfigurations', [])
                        if not service.get('currentDeployment') and len(active) == 1:
                            active_image = active[0].get('primaryContainer', {}).get('image')
                            current = next((item for item in results if item['image'] == active_image), None)
                            if current:
                                break
                        time.sleep(5)
                    else:
                        raise RuntimeError('ECS service did not settle; inspect and retire it manually')
                    images = [adapter.image for adapter in (first, second)
                              if adapter and adapter.image_pushed and adapter.image]
                    first.retire({**current, 'images': list(dict.fromkeys(images))}, first_attempt)
                    print('임시 ECS 서비스와 이미지 정리 완료. RDS와 관리형 비밀은 보존합니다.', flush=True)
                else:
                    first.cleanup_failure(first_attempt)
            except Exception as exc:
                print('ECS 정리 상태를 직접 확인해야 합니다:', exc, flush=True)
                if original_error is not None:
                    original_error.add_note(f'ECS cleanup also failed: {exc}')
                else:
                    raise
        print('PASS: PostgreSQL migration and write/read survived an ECS service revision update.', flush=True)


if __name__ == '__main__':
    main()
