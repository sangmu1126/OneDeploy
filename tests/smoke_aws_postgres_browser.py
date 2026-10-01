"""Opt-in real Chrome UI -> existing RDS -> ECS smoke with scripted AI tools."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.postgres import discover_existing_postgres
from onedeploy.server import App, handler_for
from tests.smoke_aws_postgres_api import PostgresFixture, archive
from tests.smoke_aws_postgres import probe

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_cdp.mjs')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify the browser PostgreSQL deployment path')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Deploy temporary billable ECS resources')
    mode.add_argument('--browser-read-only', action='store_true',
                      help='Verify browser network and existing RDS lookup without deploying')
    parser.add_argument('--application', default='demo-app')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service-security-group', required=True)
    args = parser.parse_args(argv)
    if args.application != 'demo-app':
        parser.error('The browser driver currently uses the demo-app fixture only')
    if not re.fullmatch(r'\d{12}', args.account):
        parser.error('--account must be a 12-digit AWS account ID')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True,
                           service_security_group=args.service_security_group)
    settings.validate()
    database = discover_existing_postgres(args.application, settings)
    print('Existing DB verified:', database['database_id'], database['status'], flush=True)
    if not args.apply and not args.browser_read_only:
        print('읽기 전용 확인 완료. --apply 없이는 브라우저 배포나 ECS 생성을 시작하지 않습니다.', flush=True)
        return
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    if args.apply:
        subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                       check=True, capture_output=True, text=True, timeout=20)
    state = Path(tempfile.mkdtemp(prefix='onedeploy-postgres-browser-smoke-'))
    app = App(state, AISettings('fixture-only', 'scripted'), PostgresFixture,
              aws_settings=settings, monitor_interval=0)
    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    archive_path = state / 'probe.zip'
    archive_path.write_bytes(archive())
    profile = state / 'chrome-profile'
    chrome_log = (state / 'chrome.log').open('w')
    chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
        '--no-default-browser-check', '--disable-gpu', '--disable-background-networking',
        '--no-proxy-server', '--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0', '--remote-allow-origins=*',
        '--user-data-dir=' + str(profile), 'about:blank'],
        stdout=chrome_log, stderr=subprocess.STDOUT)
    key = secrets.token_urlsafe(32)
    record_id = uuid.uuid4().hex
    job_id = None
    retired = False
    def api(path, data=None):
        request = urllib.request.Request(url.rstrip('/') + path, data=data,
                                         headers={'X-OneDeploy-Token': app.token})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 30
        while not port_file.is_file():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome debugging endpoint did not start; see ' + str(state / 'chrome.log'))
            time.sleep(.2)
        port = port_file.read_text().splitlines()[0]
        environment = {**os.environ, 'ONEDEPLOY_BROWSER_PROBE_KEY': key}
        subprocess.run(['node', str(DRIVER), url, port, str(archive_path),
                        'apply' if args.apply else 'read-only'],
                       check=True, timeout=40 * 60, env=environment)
        if args.browser_read_only:
            if app.jobs:
                raise AssertionError('Read-only browser check created a deployment job')
            retired = True
            print('PASS: real browser filled the default network and verified existing RDS without deployment', flush=True)
            return
        jobs = list(app.jobs.values())
        if len(jobs) != 1:
            raise AssertionError('Expected one browser deployment job')
        job = jobs[0]
        job_id = job['id']
        if (job['status'] != 'succeeded' or job.get('attempts') != 1
                or job.get('result', {}).get('database', {}).get('database_id') != database['database_id']
                or job.get('result', {}).get('migration', {}).get('cleanup_complete') is not True):
            raise AssertionError('Browser job did not complete an owned DB deployment')
        if not api('/api/jobs/' + job_id + '/health').get('healthy'):
            raise AssertionError('Browser deployment health check failed')
        endpoint = job['result']['url']
        probe(endpoint, key, record_id, 'POST')
        probe(endpoint, key, record_id, 'GET')
        probe(endpoint, key, record_id, 'DELETE')
        print('PASS: real browser UI -> ECS + RDS -> HTTP write/read/delete', flush=True)
    finally:
        try:
            jobs = list(app.jobs.values())
            if len(jobs) == 1 and jobs[0].get('status') == 'succeeded' \
                    and jobs[0].get('deployment_state', 'active') in {'active', 'delete_failed'}:
                job_id = jobs[0]['id']
                api('/api/jobs/' + job_id + '/retire', b'')
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    job = api('/api/jobs/' + job_id)
                    if job.get('deployment_state') in {'deleted', 'delete_failed'}:
                        break
                    time.sleep(3)
                retired = job.get('deployment_state') == 'deleted'
                print('Temporary ECS retirement:', 'confirmed' if retired else 'needs attention', flush=True)
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
            if retired:
                shutil.rmtree(state)
            else:
                print('Smoke state retained for diagnosis:', state, flush=True)
                if job_id:
                    print('Job:', job_id, flush=True)
    if not retired:
        raise RuntimeError('Temporary ECS resources were not confirmed retired')


if __name__ == '__main__':
    main()
