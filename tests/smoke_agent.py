"""Real agent-tool HTTP/Docker smoke. Default AI decisions are a TEST FIXTURE."""
import argparse
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_fixture import EnvironmentFixture, PythonDockerfileFixture, RepairFixture
from onedeploy.agent import OpenAIDeployAgent
from onedeploy.analysis import AISettings
from onedeploy.infrastructure import OpenAIInfrastructurePlanner
from onedeploy.server import App, handler_for


class InfrastructureFixture:
    """Deterministic target choice for a real Docker integration test."""
    def __init__(self, _settings):
        pass

    def propose(self, files, available_targets, public_access):
        assert 'local-docker' in available_targets
        name, content = next(iter(files.items()))
        return {'target': 'local-docker', 'workload': 'stateless-http',
                'rationale': '로컬 실행 경로 검증',
                'evidence': [{'file': name, 'quote': content[:20]}]}


def verify_node_repair(job):
    """A successful HTTP response alone does not prove the model repaired the upload."""
    original = Path(job['project'])
    work = original.parent / 'work'
    changed = {item.get('path') for item in job.get('changes', [])
               if isinstance(item.get('diff'), str) and item['diff'].strip()}
    if not {'package.json', 'server.js'} <= changed:
        raise AssertionError('시작 스크립트와 서버 바인딩의 AI 수정 기록이 모두 필요합니다.')
    if (json.loads((original / 'package.json').read_text())['scripts'] != {}
            or "'127.0.0.1'" not in (original / 'server.js').read_text()
            or (original / 'Dockerfile').exists()):
        raise AssertionError('업로드 원본이 변경됐습니다.')
    scripts = json.loads((work / 'package.json').read_text()).get('scripts', {})
    server = (work / 'server.js').read_text()
    if (not isinstance(scripts.get('start'), str) or not scripts['start'].strip()
            or 'process.env.PORT' not in server or re.search(r'''["']127\.0\.0\.1["']''', server)
            or not re.search(r'''["']0\.0\.0\.0["']''', server)):
        raise AssertionError('작업용 복사본에 시작·PORT·바인딩 수정이 없습니다.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Use actual AI API (requires configured key/model)')
    parser.add_argument('--environment', action='store_true', help='Exercise missing environment and resume')
    parser.add_argument('--python', action='store_true', help='Use a scripted fixture for a Python Dockerfile app')
    parser.add_argument('--folder', action='store_true', help='Upload a browser-style folder instead of a ZIP')
    parser.add_argument('--auto', action='store_true', help='Exercise infrastructure target planning')
    args = parser.parse_args()
    if args.python and (args.live or args.environment):
        parser.error('--python cannot be combined with --live or --environment')
    if args.live and args.auto:
        parser.error('--live --auto may select a billable cloud target; use a dedicated cloud drill')
    settings = AISettings.from_environment() if args.live else AISettings('test-fixture', 'fixture-model')
    if args.live and not settings.available:
        raise RuntimeError('Set OPENAI_API_KEY and ONEDEPLOY_AI_MODEL for live testing')
    with tempfile.TemporaryDirectory(prefix='onedeploy-agent-smoke-') as directory:
        app = App(Path(directory), settings, OpenAIDeployAgent if args.live else
                  (PythonDockerfileFixture if args.python else EnvironmentFixture if args.environment else RepairFixture),
                  infrastructure_planner_factory=InfrastructureFixture if args.auto and not args.live else OpenAIInfrastructurePlanner)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        job_id = None
        def request(path, data=None, content_type=None):
            headers = {'X-OneDeploy-Token': app.token}
            if content_type:
                headers['Content-Type'] = content_type
            if path == '/api/deployments' and args.auto:
                headers['X-Deploy-Target'] = 'auto'
            req = urllib.request.Request(base + path, data=data, headers=headers)
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        try:
            files = []
            if args.python:
                files.append(('Dockerfile', b'FROM python:3.13-alpine\nWORKDIR /app\nCOPY server.py ./\nCMD ["python", "server.py"]\n'))
                files.append(('server.py', b"from http.server import BaseHTTPRequestHandler, HTTPServer\nimport os\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header('Content-Type', 'application/json')\n        self.end_headers()\n        self.wfile.write(b'{\"message\":\"Python app is running\"}')\nHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()\n"))
            else:
                for file in Path('examples/unready-node').iterdir():
                    data = file.read_bytes()
                    if args.environment and file.name == 'server.js':
                        data = b"if (!process.env.DEMO_TOKEN) throw Error('DEMO_TOKEN is required');\n" + data
                    files.append((file.name, data))
            if args.folder:
                boundary = 'onedeploy-smoke-boundary'
                body = bytearray()
                for name, data in files:
                    for field, value in [('path', ('sample-app/' + name).encode()), ('file', data)]:
                        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"'.encode())
                        if field == 'file':
                            body.extend(f'; filename="{name}"'.encode())
                        body.extend(b'\r\n\r\n' + value + b'\r\n')
                body.extend(f'--{boundary}--\r\n'.encode())
                upload, content_type = bytes(body), 'multipart/form-data; boundary=' + boundary
            else:
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, 'w') as bundle:
                    for name, data in files:
                        bundle.writestr(name, data)
                upload, content_type = archive.getvalue(), None
            # One upload request starts editing and deployment; there is no analyze/approve step.
            job_id = request('/api/deployments', upload, content_type)['id']
            for _ in range(950):
                job = request('/api/jobs/' + job_id)
                if job['status'] == 'waiting_input' and args.environment:
                    assert job['missing_environment'] == ['DEMO_TOKEN']
                    request('/api/deployments/' + job_id + '/resume', json.dumps({
                        'environment': {'DEMO_TOKEN': 'synthetic-agent-runtime-value'}}).encode())
                    time.sleep(1)
                    continue
                if job['status'] != 'running':
                    break
                time.sleep(1)
            if job['status'] != 'succeeded':
                raise AssertionError(json.dumps(job, indent=2, ensure_ascii=False))
            if args.auto:
                assert job['target'] == 'local-docker'
                assert job['infrastructure_plan']['planner'] == 'openai'
            with opener.open(job['result']['url'], timeout=5) as response:
                assert json.load(response)['message'] == ('Python app is running' if args.python else 'Original application is running')
            original = Path(job['project'])
            if args.python:
                assert not (original / 'package.json').exists()
                assert "'127.0.0.1'" in (original / 'server.py').read_text()
                assert (original / 'Dockerfile').exists()
                assert job['plan']['runtime'] == 'custom-dockerfile'
            else:
                verify_node_repair(job)
            assert 'synthetic-agent-runtime-value' not in json.dumps(job)
            if not args.live and not args.python:
                assert job['attempts'] == 2
                assert any(e['stage'] == 'attempt_failed' for e in job['events'])
                containers = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True)
                assert f'onedeploy-{job_id}-a1' not in containers.splitlines()
            print('PASS: one request -> source edits -> real Docker -> HTTP URL', flush=True)
            print('AI mode: ' + ('LIVE' if args.live else 'SCRIPTED TEST FIXTURE (not live AI)'), flush=True)
            print('Attempts: ' + str(job['attempts']), flush=True)
            print(json.dumps(job['result']), flush=True)
            if args.python:
                app.monitor_once()
                observed = request('/api/jobs/' + job_id)
                assert observed['last_health']['healthy']
                assert observed['last_health']['source'] == 'automatic'
                assert (app.root / job_id / 'health.json').is_file()
                print('PASS: automatic health check recorded Docker ownership and HTTP 200', flush=True)
            request('/api/jobs/' + job_id + '/retire', b'')
            for _ in range(30):
                retired = request('/api/jobs/' + job_id)
                if retired['deployment_state'] != 'deleting':
                    break
                time.sleep(1)
            assert retired['deployment_state'] == 'deleted', json.dumps(retired, ensure_ascii=False)
            assert subprocess.run(['docker', 'container', 'inspect', job['result']['container']],
                                  capture_output=True).returncode != 0
            assert subprocess.run(['docker', 'image', 'inspect', job['result']['image']],
                                  capture_output=True).returncode != 0
            print('PASS: retirement API removed the owned container and image tag', flush=True)
        finally:
            server.shutdown()
            server.server_close()
            if job_id:
                for attempt in range(1, 4):
                    subprocess.run(['docker', 'rm', '-f', f'onedeploy-{job_id}-a{attempt}'], capture_output=True)
                    subprocess.run(['docker', 'image', 'rm', f'onedeploy/{job_id}-a{attempt}:latest'], capture_output=True)


if __name__ == '__main__':
    main()
