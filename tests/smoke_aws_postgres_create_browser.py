"""Opt-in real Chrome network -> RDS creation drill with owned resource cleanup."""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from onedeploy.postgres import AwsPostgresProvisioner, PostgresRequest
from onedeploy.server import App, handler_for
from tests.smoke_aws_network import retire_probe
from tests.smoke_aws_postgres_cleanup import apply as cleanup_database

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify browser-created PostgreSQL and retire its resources')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--application', default='dbdrill-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'dbdrill-[a-f0-9]{8}', args.application):
        parser.error('--application must be a unique dbdrill-<8 hex> ID')
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
    state = Path('.onedeploy') / 'browser-db-drills' / args.application
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    app = App(state / 'app', AISettings('fixture-only', 'scripted'),
              aws_settings=settings, monitor_interval=0)

    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = state / 'chrome-profile'
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
            if db_operation and db_operation['status'] == 'succeeded' and network_operation \
                    and network_operation['status'] == 'succeeded':
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
