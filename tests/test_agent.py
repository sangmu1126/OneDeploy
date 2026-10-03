import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_fixture import RepairFixture, call
from onedeploy.agent import AgentError, DeploymentAgent, DeploymentCancelled, DeploymentTools, NeedsEnvironment, OpenAIDeployAgent
from onedeploy.analysis import AISettings
from onedeploy.core import LocalDockerAdapter
from onedeploy.server import App


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original'
        shutil.copytree('examples/unready-node', self.original)
        self.events, self.updates = [], []
        self.tools = DeploymentTools(self.original, self.root / 'work', 'a' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u))

    def test_patches_working_copy_only_and_invalidates_plan(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.read_project_files(['server.js'])
        self.tools.apply_project_patch('server.js', "'127.0.0.1'", "'0.0.0.0'")
        self.assertIn("'127.0.0.1'", (self.original / 'server.js').read_text())
        self.assertIsNone(self.tools.plan)
        with self.assertRaisesRegex(ValueError, 'Configure'):
            self.tools.deploy_application()

    def test_patch_requires_read_and_exact_match(self):
        with self.assertRaisesRegex(ValueError, 'Read'):
            self.tools.apply_project_patch('server.js', 'http', 'https')
        self.tools.read_project_files(['server.js'])
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.tools.apply_project_patch('server.js', 'not-found', 'new')

    def test_paths_cannot_escape_working_directory(self):
        for name in ('../secret.js', '/tmp/secret.js', '.env', '.git/config.json', 'Dockerfile', 'package-lock.json', 'node_modules/x.js'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tools.file_path(name)
        (self.tools.work / 'link.js').symlink_to(self.original / 'server.js')
        with self.assertRaises(ValueError):
            self.tools.read_project_files(['link.js'])

    def test_existing_dockerfile_can_be_repaired_in_working_copy(self):
        original_file = self.original / 'Dockerfile'
        original_file.write_text('FROM node:22-alpine\nCMD ["npm", "start"]\n')
        custom = DeploymentTools(self.original, self.root / 'custom-work', 'b' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u))
        self.assertIn('Dockerfile', custom.read_project_files(['Dockerfile'])['files'])
        custom.apply_project_patch('Dockerfile', 'FROM node:22-alpine', 'FROM node:22-bookworm-slim')
        custom.configure_deployment('dockerfile', None, 3000, '/', [])
        self.assertEqual(custom.plan.dockerfile_source, 'existing')
        self.assertIn('bookworm-slim', custom.plan.dockerfile)
        self.assertIn('node:22-alpine', original_file.read_text())

    def test_python_dockerfile_project_can_be_repaired(self):
        original = self.root / 'python-original'
        original.mkdir()
        (original / 'Dockerfile').write_text('FROM python:3.13-alpine\nCOPY . /app\nCMD ["python", "/app/server.py"]\n')
        (original / 'server.py').write_text('host = "127.0.0.1"\n')
        custom = DeploymentTools(original, self.root / 'python-work', 'c' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u))
        custom.read_project_files(['Dockerfile', 'server.py'])
        custom.apply_project_patch('server.py', '127.0.0.1', '0.0.0.0')
        custom.configure_deployment('dockerfile', None, 3000, '/', [])
        self.assertEqual(custom.plan.runtime, 'custom-dockerfile')
        self.assertIn('127.0.0.1', (original / 'server.py').read_text())
        self.assertIn('0.0.0.0', (custom.work / 'server.py').read_text())

    def test_environment_pause_never_returns_values(self):
        with self.assertRaises(NeedsEnvironment) as raised:
            self.tools.request_environment(['DATABASE_URL'], 'DB 접속값이 필요합니다.')
        self.assertEqual(raised.exception.names, ['DATABASE_URL'])
        self.tools.environment['DATABASE_URL'] = 'secret-database-url'
        self.assertEqual(self.tools.request_environment(['DATABASE_URL'], 'DB'), {'available': True})
        self.tools.event('output', 'value=secret-database-url')
        self.assertNotIn('secret-database-url', str(self.events))

    def test_cannot_finish_by_claiming_success(self):
        class Provider:
            def next(self, history):
                return [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Deployment successful'}]}]
        with self.assertRaises(AgentError):
            DeploymentAgent(Provider(), self.tools).run()
        self.assertIsNone(self.tools.result)

    def test_unknown_tools_never_execute_and_steps_are_bounded(self):
        class Provider:
            def next(self, history):
                return call('shell', {'command': 'bad'})
        with self.assertRaisesRegex(AgentError, '횟수 제한'):
            DeploymentAgent(Provider(), self.tools, max_steps=2).run()
        self.assertEqual(len([u for u in self.updates if 'steps' in u]), 2)

    def test_failed_deployment_is_returned_to_agent_then_repaired(self):
        attempts = []
        class Adapter:
            def __init__(self, event):
                self.event = event
            def deploy(self, project, plan, attempt_id, environment):
                attempts.append(plan)
                if len(attempts) == 1:
                    self.event('output', 'HTTP refused: bound to localhost')
                    raise RuntimeError('HTTP connection failed')
                self.assert_binding = "'0.0.0.0'" in (project / 'server.js').read_text()
                assert self.assert_binding
                return {'url': 'http://127.0.0.1:1234'}
            def command(self, args, timeout):
                return ''
            def cleanup_failure(self, attempt_id):
                pass
        self.tools.adapter_factory = Adapter
        result = DeploymentAgent(RepairFixture(), self.tools).run()
        self.assertEqual(result['url'], 'http://127.0.0.1:1234')
        self.assertEqual(self.tools.attempts, 2)
        self.assertEqual(attempts[-1].port, 3000)

    def test_accepted_aws_update_stops_automatic_retry(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        class Adapter:
            updated_existing = True
            previous_deployment_arn = 'old-deployment'
            image = 'example.amazonaws.com/onedeploy-managed:aaaaaaaaaaaaaaaa-a1'
            def __init__(self, event):
                self.event = event
            def deploy(self, *_args):
                raise RuntimeError('HTTP verification failed')
            def cleanup_failure(self, _attempt_id):
                pass
        self.tools.adapter_factory = Adapter
        with self.assertRaisesRegex(AgentError, '자동 재시도를 중단'):
            self.tools.deploy_application()
        self.assertEqual(self.tools.attempts, 1)
        self.assertTrue(any(update.get('aws_update_submitted') for update in self.updates))

    def test_three_attempt_limit_survives_new_agent_run(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.attempts = 3
        with self.assertRaisesRegex(AgentError, '2회'):
            self.tools.deploy_application()

    def test_cancelled_before_first_attempt_never_calls_adapter(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.cancel_check = lambda: True
        with patch.object(LocalDockerAdapter, 'deploy') as deploy:
            with self.assertRaises(DeploymentCancelled):
                self.tools.deploy_application()
            deploy.assert_not_called()
        self.assertEqual(self.tools.attempts, 0)

    def test_tool_protocol_preserves_reasoning_items(self):
        output = [{'type': 'reasoning', 'encrypted_content': 'opaque'}, *call('read_runtime_logs', {})]
        with patch('onedeploy.agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps({'status': 'completed', 'output': output}).encode())
            result = OpenAIDeployAgent(AISettings('fake', 'model')).next([{'role': 'user', 'content': 'deploy'}])
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result, output)
        self.assertFalse(payload['parallel_tool_calls'])
        self.assertFalse(payload['store'])
        self.assertIn('reasoning.encrypted_content', payload['include'])

    def test_waiting_job_and_unconfigured_agent_job_restore(self):
        root = self.root / 'state'
        job_id = 'b' * 16
        project = root / job_id / 'source'
        shutil.copytree(self.original, project)
        app = App(root, AISettings())
        app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'plan': None, 'status': 'waiting_input',
                            'project': str(project), 'events': [], 'missing_environment': ['DB_URL']}
        app.save(job_id)
        restored = App(root, AISettings())
        self.assertEqual(restored.jobs[job_id]['status'], 'waiting_input')
        self.assertEqual(restored.summaries()[0]['analyzer'], 'agent')


if __name__ == '__main__':
    unittest.main()
