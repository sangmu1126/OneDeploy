"""Opt-in real Chrome network -> RDS creation drill with owned resource cleanup."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from onedeploy.postgres import AwsPostgresProvisioner, PostgresRequest
from onedeploy.postgres_snapshot import inspect_snapshot
from onedeploy.server import App, handler_for
from tests.smoke_aws_network import retire_probe
from tests.smoke_aws_postgres import probe
from tests.smoke_aws_postgres_api import PostgresFixture, archive
from tests.smoke_aws_postgres_cleanup import apply as cleanup_database

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')


def retire_final_snapshot(application: str, operation: dict, settings: AwsSettings,
                          request: PostgresRequest) -> None:
    """Remove only the final snapshot of this completed disposable UI drill."""
    if (not re.fullmatch(r'dbdrill-[a-f0-9]{8}', application)
            or operation['application_id'] != application
            or operation['database_id'] != 'onedeploy-' + application
            or request.application_id != application
            or operation['status'] != 'succeeded'
            or operation['stage'] != 'stack_deleted'
            or not re.fullmatch(r'onedeploy-' + application + r'-final-[a-f0-9]{12}',
                                operation['snapshot_id'])):
        raise AssertionError('임시 폐기 기록의 앱·DB·최종 스냅샷을 확인하지 못했습니다.')
    snapshot = inspect_snapshot(application, operation['snapshot_id'], settings)
    if snapshot['status'] != 'available' or snapshot['encrypted'] is not True:
        raise AssertionError('앱 소유 암호화 최종 스냅샷이 사용 가능 상태가 아닙니다.')
    adapter = AwsPostgresProvisioner(request).adapter
    stack_id = operation['stack_id']
    stacks = json.loads(adapter.aws(['cloudformation', 'describe-stacks',
        '--stack-name', stack_id], private=True, quiet=True)).get('Stacks', [])
    if (len(stacks) != 1 or stacks[0].get('StackId') != stack_id
            or stacks[0].get('StackStatus') != 'DELETE_COMPLETE'):
        raise AssertionError('폐기한 RDS 스택이 DELETE_COMPLETE 상태가 아닙니다.')
    databases = json.loads(adapter.aws(['rds', 'describe-db-instances'], private=True, quiet=True))
    if (databases.get('Marker') or not isinstance(databases.get('DBInstances'), list)
            or any(db.get('DBInstanceIdentifier') == operation['database_id']
                   for db in databases['DBInstances'])):
        raise AssertionError('폐기한 DB가 AWS 목록에 남아 있습니다.')
    deleted = json.loads(adapter.aws(['rds', 'delete-db-snapshot',
        '--db-snapshot-identifier', snapshot['snapshot_id']], private=True, quiet=True))
    if deleted.get('DBSnapshot', {}).get('DBSnapshotArn') != snapshot['snapshot_arn']:
        raise AssertionError('시험용 최종 스냅샷 삭제 응답의 ARN이 예상과 다릅니다.')
    adapter.aws(['rds', 'wait', 'db-snapshot-deleted',
                 '--db-snapshot-identifier', snapshot['snapshot_id']],
                timeout=3600, private=True, quiet=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify browser-created PostgreSQL and retire its resources')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--deploy-app', action='store_true',
                        help='Also upload and deploy a PostgreSQL app through Chrome')
    parser.add_argument('--retire-through-ui', action='store_true',
                        help='Retire the disposable RDS using the product browser UI')
    parser.add_argument('--application', default='dbdrill-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'dbdrill-[a-f0-9]{8}', args.application):
        parser.error('--application must be a unique dbdrill-<8 hex> ID')
    if args.deploy_app and args.retire_through_ui:
        parser.error('Run --deploy-app and --retire-through-ui in separate disposable drills')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    network = discover_default_network(settings)
    network_request = ServiceNetworkRequest(args.application, args.account, args.region,
                                            network['vpc_id'])
    network_provisioner = AwsServiceNetworkProvisioner(network_request)
    preview = network_provisioner.preflight()
    print('Read-only network preflight:', preview['stack_name'], flush=True)
    if not args.apply:
        print('읽기 전용 확인 완료. --apply 없이는 브라우저나 AWS 리소스를 생성하지 않습니다.', flush=True)
        return
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    if args.deploy_app:
        subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                       check=True, capture_output=True, text=True, timeout=20)
    state = Path('.onedeploy') / 'browser-db-drills' / args.application
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    app = App(state / 'app', AISettings('fixture-only', 'scripted'),
              PostgresFixture if args.deploy_app else None,
              aws_settings=settings, monitor_interval=0)

    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = state / 'chrome-profile'
    archive_path = state / 'probe.zip'
    if args.deploy_app:
        archive_path.write_bytes(archive())
    probe_key = secrets.token_urlsafe(32)
    record_id = uuid.uuid4().hex
    base = f'http://127.0.0.1:{server.server_port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path: str, data: bytes | None = None) -> dict:
        request = urllib.request.Request(base + path, data=data,
                                         headers={'X-OneDeploy-Token': app.token})
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    chrome_log = (state / 'chrome.log').open('w')
    chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
        '--no-default-browser-check', '--disable-gpu', '--disable-background-networking',
        '--no-proxy-server', '--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0', '--remote-allow-origins=*',
        '--user-data-dir=' + str(profile), 'about:blank'],
        stdout=chrome_log, stderr=subprocess.STDOUT)
    cleanup_complete = False
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 30
        while not port_file.is_file():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome debugging endpoint did not start: ' + str(state / 'chrome.log'))
            time.sleep(.2)
        try:
            subprocess.run(['node', str(DRIVER), f'http://127.0.0.1:{server.server_port}/',
                            port_file.read_text().splitlines()[0], args.application, 'create'],
                           check=True, timeout=20 * 60)
        except subprocess.CalledProcessError:
            if args.application not in app.postgres_operations.operations:
                raise
            print('Chrome 연결이 끊겼지만 DB 생성 요청이 기록돼 있어 완료 후 다시 조회합니다.',
                  flush=True)
        deadline = time.monotonic() + 3900
        while time.monotonic() < deadline:
            operation = app.postgres_operations.operations.get(args.application)
            if operation and operation['status'] != 'running':
                break
            time.sleep(5)
        operation = app.postgres_operations.get(args.application)
        if operation['status'] != 'succeeded':
            raise RuntimeError('RDS 생성 상태 확인 필요: ' + operation['message'])
        subprocess.run(['node', str(DRIVER), f'http://127.0.0.1:{server.server_port}/',
                        port_file.read_text().splitlines()[0], args.application, 'verify'],
                       check=True, timeout=120)
        network_operation = app.network_operations.get(args.application)
        db_operation = app.postgres_operations.get(args.application)
        created_network = network_provisioner.inspect_current()
        db_request = PostgresRequest(args.application, args.account, args.region,
                                     network['vpc_id'], tuple(network['subnet_ids']),
                                     created_network['service_security_group'])
        created_db = AwsPostgresProvisioner(db_request).inspect_current()
        if (network_operation['status'] != 'succeeded'
                or network_operation['stack_id'] != created_network['stack_id']
                or db_operation['status'] != 'succeeded'
                or db_operation['database_id'] != created_db['database_id']
                or app.jobs):
            raise AssertionError('브라우저 작업 기록과 AWS 결과가 일치하지 않습니다.')
        print('PASS: browser operations match the owned AWS network and RDS', flush=True)
        if args.deploy_app:
            environment = {**os.environ, 'ONEDEPLOY_BROWSER_PROBE_KEY': probe_key}
            subprocess.run(['node', str(DRIVER), base + '/',
                            port_file.read_text().splitlines()[0], args.application,
                            'deploy', str(archive_path.resolve())],
                           check=True, timeout=10 * 60, env=environment)
            deadline = time.monotonic() + 2400
            while time.monotonic() < deadline:
                jobs = list(app.jobs.values())
                if len(jobs) == 1 and jobs[0]['status'] in {
                        'succeeded', 'failed', 'interrupted', 'cancelled'}:
                    break
                time.sleep(3)
            jobs = list(app.jobs.values())
            if len(jobs) != 1:
                raise AssertionError('브라우저 배포 작업이 하나로 기록되지 않았습니다.')
            job = jobs[0]
            if (job['status'] != 'succeeded' or job.get('attempts') != 1
                    or job.get('result', {}).get('database', {}).get('database_id')
                    != created_db['database_id']
                    or job.get('result', {}).get('migration', {}).get('cleanup_complete') is not True):
                raise AssertionError('새로 만든 DB의 브라우저 배포·마이그레이션이 완료되지 않았습니다.')
            if not api('/api/jobs/' + job['id'] + '/health').get('healthy'):
                raise AssertionError('새 DB 배포의 상태 확인이 실패했습니다.')
            endpoint = job['result']['url']
            probe(endpoint, probe_key, record_id, 'POST')
            probe(endpoint, probe_key, record_id, 'GET')
            probe(endpoint, probe_key, record_id, 'DELETE')
            subprocess.run(['node', str(DRIVER), base + '/',
                            port_file.read_text().splitlines()[0], args.application,
                            'verify-deploy'], check=True, timeout=120)
            print('PASS: browser upload -> new RDS migration -> HTTP write/read/delete', flush=True)
        if args.retire_through_ui:
            subprocess.run(['node', str(DRIVER), base + '/',
                            port_file.read_text().splitlines()[0], args.application, 'retire'],
                           check=True, timeout=10 * 60)
            deadline = time.monotonic() + 7200
            while time.monotonic() < deadline:
                retirement = app.postgres_retirement_operations.operations.get(args.application)
                if retirement and retirement['status'] != 'running':
                    break
                time.sleep(5)
            retirement = app.postgres_retirement_operations.get(args.application)
            if retirement['status'] != 'succeeded' or retirement['stage'] != 'stack_deleted':
                raise RuntimeError('브라우저 DB 폐기 상태 확인 필요: ' + retirement['message'])
            subprocess.run(['node', str(DRIVER), base + '/',
                            port_file.read_text().splitlines()[0], args.application,
                            'verify-retire'], check=True, timeout=120)
            print('PASS: browser retirement completed with an available final snapshot', flush=True)
    finally:
        try:
            deadline = time.monotonic() + 3900
            while time.monotonic() < deadline:
                operation = app.postgres_operations.operations.get(args.application)
                if not operation or operation['status'] != 'running':
                    break
                time.sleep(5)
            db_operation = app.postgres_operations.operations.get(args.application)
            network_operation = app.network_operations.operations.get(args.application)
            retirement_operation = app.postgres_retirement_operations.operations.get(args.application)
            if retirement_operation:
                deadline = time.monotonic() + 7200
                while retirement_operation['status'] == 'running' and time.monotonic() < deadline:
                    time.sleep(5)
                retirement_operation = app.postgres_retirement_operations.operations[args.application]
            jobs = list(app.jobs.values())
            for job in jobs:
                if (job.get('status') == 'succeeded'
                        and job.get('deployment_state', 'active') in {'active', 'delete_failed'}):
                    api('/api/jobs/' + job['id'] + '/retire', b'')
                    deadline = time.monotonic() + 1200
                    while time.monotonic() < deadline:
                        retired = api('/api/jobs/' + job['id'])
                        if retired.get('deployment_state') in {'deleted', 'delete_failed'}:
                            break
                        time.sleep(3)
                    print('Temporary ECS retirement:', retired.get('deployment_state'), flush=True)
            ecs_retired = all(job.get('status') != 'succeeded'
                              or api('/api/jobs/' + job['id']).get('deployment_state') == 'deleted'
                              for job in jobs)
            if retirement_operation and retirement_operation['status'] == 'succeeded' \
                    and network_operation and network_operation['status'] == 'succeeded' \
                    and db_operation and db_operation['status'] == 'succeeded' and ecs_retired:
                db_request = PostgresRequest(args.application, args.account, args.region,
                    network['vpc_id'], tuple(network['subnet_ids']),
                    network_operation['service_security_group'])
                retire_final_snapshot(args.application, retirement_operation, settings,
                                      db_request)
                print('PASS: temporary final snapshot verified and deleted', flush=True)
                retire_probe(network_provisioner, network_operation['stack_id'],
                             network_operation['service_security_group'])
                print('PASS: temporary network stack deleted', flush=True)
                cleanup_complete = True
            elif retirement_operation:
                print('폐기 기록이 완료되지 않아 자동 정리를 중단했습니다. 확인할 앱:',
                      args.application, retirement_operation['status'], flush=True)
            elif db_operation and db_operation['status'] == 'succeeded' and network_operation \
                    and network_operation['status'] == 'succeeded' and ecs_retired:
                db_request = PostgresRequest(args.application, args.account, args.region,
                    network['vpc_id'], tuple(network['subnet_ids']),
                    network_operation['service_security_group'])
                result = cleanup_database(db_request, state / 'database-cleanup.json')
                print('PASS: temporary RDS deleted:', result['database_id'], flush=True)
                retire_probe(network_provisioner, network_operation['stack_id'],
                             network_operation['service_security_group'])
                print('PASS: temporary network stack deleted', flush=True)
                cleanup_complete = True
            elif db_operation is None and network_operation \
                    and network_operation['status'] == 'succeeded':
                retire_probe(network_provisioner, network_operation['stack_id'],
                             network_operation['service_security_group'])
                print('PASS: RDS 요청 전 생성한 임시 네트워크를 정리했습니다.', flush=True)
                cleanup_complete = True
            else:
                print('생성 결과가 불확실해 자동 정리를 중단했습니다. 확인할 앱:',
                      args.application, flush=True)
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
            chrome_log.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if cleanup_complete:
                shutil.rmtree(profile, ignore_errors=True)
            print('Local drill state:', state.resolve(), flush=True)
    if not cleanup_complete:
        raise RuntimeError('Temporary browser-created resources were not confirmed retired')


if __name__ == '__main__':
    main()
