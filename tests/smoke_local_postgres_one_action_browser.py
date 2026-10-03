"""Real Chrome and HTTP rehearsal of the combined RDS-create/deploy UI without AWS."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.postgres import PostgresRequest
from onedeploy.server import App, handler_for
from tests.smoke_aws_postgres_api import archive


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')
APPLICATION = 'dbdrill-1234abcd'


def run() -> dict:
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                           service_security_group='sg-33333333')
    request = PostgresRequest(APPLICATION, settings.expected_account, settings.region,
                              'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                              settings.service_security_group)
    quote = {'account': request.account, 'region': request.region,
             'vpc_id': request.vpc_id, 'subnet_ids': list(request.subnet_ids),
             'stack_name': request.stack_name, 'database_id': request.database_id,
             'engine_version': '18.3', 'instance_class': 'db.t4g.micro',
             'storage_type': 'gp3', 'storage_gib': 20,
             'pricing': {'baseline_730h_usd': '20.87'}}
    database = {'database_id': request.database_id}

    def reject_aws(_adapter, args, **_kwargs):
        raise AssertionError('Unexpected AWS command in local drill: ' + repr(args))

    with tempfile.TemporaryDirectory(prefix='onedeploy-one-action-browser-') as directory, \
            patch('onedeploy.postgres.AwsExpressAdapter.aws', autospec=True,
                  side_effect=reject_aws), \
            patch('onedeploy.postgres_operations.AwsPostgresProvisioner.preflight',
                  return_value=quote) as preflight, \
            patch('onedeploy.postgres_operations.AwsPostgresProvisioner.assert_stack_available'), \
            patch('onedeploy.postgres_operations.AwsPostgresProvisioner.create',
                  return_value=database) as create, \
            patch('onedeploy.server.AwsPostgresProvisioner.inspect_current',
                  return_value=database) as inspect, \
            patch.object(AwsSettings, 'unavailable_reason', return_value=None):
        root = Path(directory)
        zip_path = root / 'postgres-app.zip'
        zip_path.write_bytes(archive())
        app = App(root / 'app', AISettings('fixture-only', 'scripted'),
                  aws_settings=settings, monitor_interval=0,
                  agent_factory=lambda _: object())
        deployments = []

        def finish_deployment(job_id):
            deployments.append(job_id)
            with app.lock:
                app.jobs[job_id]['status'] = 'succeeded'
                app.jobs[job_id]['result'] = {
                    'url': 'https://example.test', 'target': 'aws-ecs-express'}
                app.save(job_id)

        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / 'chrome-profile'
        try:
            with patch.object(app, 'run_agent', side_effect=finish_deployment), \
                    (root / 'chrome.log').open('w') as log:
                chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
                    '--no-default-browser-check', '--disable-gpu',
                    '--disable-background-networking', '--no-proxy-server',
                    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                    '--remote-allow-origins=*', '--user-data-dir=' + str(profile),
                    'about:blank'], stdout=log, stderr=subprocess.STDOUT)
                try:
                    port_file = profile / 'DevToolsActivePort'
                    deadline = time.monotonic() + 30
                    while not port_file.is_file():
                        if chrome.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('Local Chrome debugging endpoint did not start')
                        time.sleep(.2)
                    subprocess.run(['node', str(DRIVER),
                        f'http://127.0.0.1:{server.server_port}/',
                        port_file.read_text().splitlines()[0], APPLICATION,
                        'one-action-local', str(zip_path)], check=True, timeout=60)
                finally:
                    chrome.terminate()
                    try:
                        chrome.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        chrome.kill()
                        chrome.wait(timeout=10)
            jobs = list(app.jobs.values())
            if (len(jobs) != 1 or deployments != [jobs[0]['id']]
                    or jobs[0]['status'] != 'succeeded'
                    or jobs[0]['infrastructure_plan']['database'] != {
                        'binding': 'create', 'database_id': request.database_id}
                    or app.postgres_operations.get(APPLICATION)['status'] != 'succeeded'
                    or jobs[0]['postgres_creation_id'] !=
                    app.postgres_operations.get(APPLICATION)['creation_id']):
                raise AssertionError('Browser one-action job did not preserve the RDS binding')
            if preflight.call_count != 2 or create.call_count != 1 or inspect.call_count != 1:
                raise AssertionError('Browser one-action plan, creation, or ownership check was skipped')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
    return {'application_id': APPLICATION, 'status': 'passed',
            'aws_mode': 'mocked', 'deployment_executed': False}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
