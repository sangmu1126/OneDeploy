import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from onedeploy.agent import DeploymentTools
from onedeploy.analysis import AISettings
from onedeploy.aws import AwsSettings
from onedeploy.infrastructure import (InfrastructureProfile, explicit_infrastructure_plan,
                                      inspect_infrastructure, validate_infrastructure,
                                      validate_infrastructure_proposal, OpenAIInfrastructurePlanner)
from onedeploy.server import App, handler_for


class InfrastructureTests(unittest.TestCase):
    def test_explicit_postgres_plan_requires_detected_engine_and_existing_binding(self):
        profile = InfrastructureProfile('database', ('package.json',), 1,
                                        ('database',), ('postgresql',))
        plan = explicit_infrastructure_plan('aws-ecs-express', profile,
                                            existing_postgres_id='onedeploy-demo-app')
        self.assertEqual(plan['workload'], 'postgresql-http')
        self.assertEqual(plan['database']['binding'], 'existing')
        self.assertIn('existing RDS PostgreSQL', plan['resources'])
        with self.assertRaisesRegex(ValueError, '다릅니다'):
            explicit_infrastructure_plan('cloud-run', profile,
                                         existing_postgres_id='onedeploy-demo-app')
        with self.assertRaisesRegex(ValueError, '다릅니다'):
            explicit_infrastructure_plan('aws-ecs-express',
                InfrastructureProfile('database', ('package.json',), 1,
                                      ('database',), ('mysql',)),
                existing_postgres_id='onedeploy-demo-app')

    def test_planner_uses_bounded_structured_api_request(self):
        proposal = {'target': 'local-docker', 'workload': 'stateless-http',
                    'rationale': '로컬 검증', 'evidence': [{'file': 'package.json', 'quote': 'start'}]}
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(proposal)}]}]}
        with patch('onedeploy.infrastructure.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = OpenAIInfrastructurePlanner(AISettings('fixture-key', 'fixture-model')).propose(
                {'package.json': 'start'}, ['local-docker'], False)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result, proposal)
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(json.loads(payload['input'])['available_targets'], ['local-docker'])

    def test_planner_keeps_response_size_limit_error(self):
        with patch('onedeploy.infrastructure.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '응답 크기 제한'):
                OpenAIInfrastructurePlanner(AISettings('fixture-key', 'fixture-model')).propose(
                    {'package.json': 'start'}, ['local-docker'], False)

    def test_ai_plan_requires_real_source_evidence_and_available_target(self):
        files = {'package.json': '{"scripts":{"start":"node server.js"}}'}
        proposal = {'target': 'aws-ecs-express', 'workload': 'stateless-http',
                    'rationale': 'HTTP 컨테이너를 공개 배포합니다.',
                    'evidence': [{'file': 'package.json', 'quote': '"start":"node server.js"'}]}
        plan = validate_infrastructure_proposal(proposal, files, ['aws-ecs-express'])
        self.assertEqual(plan['resources'][-1], 'ECS Express service')
        with self.assertRaisesRegex(ValueError, '사용할 수 없는'):
            validate_infrastructure_proposal(proposal, files, ['local-docker'])
        with self.assertRaisesRegex(ValueError, '근거'):
            validate_infrastructure_proposal({**proposal, 'evidence': [{'file': 'package.json', 'quote': 'invented'}]},
                                             files, ['aws-ecs-express'])
        with self.assertRaisesRegex(ValueError, '지원하지 않는'):
            validate_infrastructure_proposal({**proposal, 'workload': 'requires-unsupported-resources'},
                                             files, ['aws-ecs-express'])

    def test_detects_embedded_database_without_scanning_docs_or_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('const db = require("better-sqlite3")("app.db");')
            (project / 'docs').mkdir()
            (project / 'docs' / 'example.py').write_text('import sqlite3')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'sqlite')
            self.assertEqual(profile.evidence, ('server.js',))
            with self.assertRaisesRegex(ValueError, '데이터 손실'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_detects_prisma_sqlite_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'schema.prisma').write_text('datasource db { provider = "sqlite" url = env("DATABASE_URL") }')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'sqlite')
            self.assertEqual(profile.evidence, ('schema.prisma',))

    def test_detects_external_database_from_runtime_dependencies_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'pg': '^8.0.0'}, 'devDependencies': {'typescript': '^5.0.0'}}))
            (project / 'schema.prisma').write_text(
                'datasource db { provider = "postgresql" url = env("DATABASE_URL") }')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'database')
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.database_engines, ('postgresql',))
            self.assertEqual(profile.evidence, ('package.json', 'schema.prisma'))
            with self.assertRaisesRegex(ValueError, '마이그레이션'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_detects_database_from_python_manifest_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'requirements.txt').write_text('psycopg[binary]>=3.1\n')
            (project / 'app.py').write_text('import pymongo\n')
            (project / 'docs').mkdir()
            (project / 'docs' / 'example.py').write_text('import sqlite3\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.evidence, ('app.py', 'requirements.txt'))
            self.assertEqual(profile.database_engines, ('mongodb', 'postgresql'))

    def test_detects_database_from_pyproject_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'pyproject.toml').write_text(
                '[project]\ndependencies = ["psycopg[binary]>=3.1"]\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.database_engines, ('postgresql',))
            self.assertEqual(profile.evidence, ('pyproject.toml',))

    def test_mixed_or_unknown_database_engine_is_not_mistaken_for_postgres(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'pg': '8.23.0', 'mysql2': '3.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.database_engines, ('mysql', 'postgresql'))
            with self.assertRaisesRegex(ValueError, 'mysql'):
                validate_infrastructure(profile, 'aws-ecs-express', postgres=True)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'@prisma/client': '6.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.database_engines, ('unknown',))
            with self.assertRaisesRegex(ValueError, 'unknown'):
                validate_infrastructure(profile, 'aws-ecs-express', postgres=True)

    def test_database_dependency_only_in_dev_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'express': '^5.0.0'}, 'devDependencies': {'pg': '^8.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ())

    def test_detects_uploaded_sqlite_database_without_source_import(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'data').mkdir()
            (project / 'data' / 'users.sqlite3').write_bytes(b'SQLite format 3\x00')
            profile = inspect_infrastructure(project)
            self.assertIn('sqlite', profile.requirements)
            self.assertEqual(profile.evidence, ('data/users.sqlite3',))

    def test_scans_dependency_beyond_old_per_file_read_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'server.js').write_text(' ' * 25000 + '\nrequire("better-sqlite3")("app.db")')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('sqlite',))
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_incomplete_source_inspection_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for number in range(1001):
                (project / f'module{number:04d}.js').write_text('const ready = true;')
            (project / 'z_database.js').write_text('require("better-sqlite3")("app.db")')
            with self.assertRaisesRegex(ValueError, '끝까지 검사'):
                inspect_infrastructure(project)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'server.js').write_text(' ' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '끝까지 검사'):
                inspect_infrastructure(project)

    def test_detects_required_worker_and_local_file_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'scripts': {'start': 'node server.js', 'worker': 'node worker.js'},
                'dependencies': {'bullmq': '^5.0.0'}}))
            (project / 'server.js').write_text('fs.writeFileSync("uploads/photo.jpg", image);')
            profile = inspect_infrastructure(project)
            self.assertEqual(set(profile.requirements), {'background-worker', 'local-files'})
            with self.assertRaisesRegex(ValueError, '워커') as raised:
                validate_infrastructure(profile, 'aws-ecs-express')
            self.assertIn('영속 저장소', str(raised.exception))

    def test_detects_procfile_worker_without_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'Procfile').write_text('web: node server.js\nworker: node worker.js\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('background-worker',))

    def test_upload_blocks_sqlite_before_any_deployment_resource(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', json.dumps({
                'scripts': {'start': 'node server.js'},
                'dependencies': {'better-sqlite3': '^11.0.0'}}))
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-OneDeploy-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertIn('SQLite', handler.json_response.call_args.args[1]['error'])
            self.assertFalse(app.jobs)

    def test_ai_planner_failure_removes_uncommitted_upload(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-OneDeploy-Token': app.token, 'X-Deploy-Target': 'auto',
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('onedeploy.server.uuid.uuid4') as uuid4, \
                    patch('onedeploy.server.plan_infrastructure', side_effect=ValueError('AI 계획 실패')):
                uuid4.return_value.hex = 'a' * 32
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            self.assertFalse((root / ('a' * 16)).exists())

    def test_initial_job_save_failure_removes_upload_and_memory_entry(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-OneDeploy-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('onedeploy.server.uuid.uuid4') as uuid4, \
                    patch.object(app, 'save', side_effect=RuntimeError('기록 저장 실패')), \
                    patch('onedeploy.server.threading.Thread.start') as worker:
                uuid4.return_value.hex = 'b' * 32
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            self.assertFalse((root / ('b' * 16)).exists())
            worker.assert_not_called()

    def test_upload_blocks_postgres_before_any_deployment_resource(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', json.dumps({
                'scripts': {'start': 'node server.js'}, 'dependencies': {'pg': '^8.0.0'}}))
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-OneDeploy-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertIn('데이터베이스', handler.json_response.call_args.args[1]['error'])
            self.assertFalse(app.jobs)

    def test_agent_cannot_add_sqlite_after_upload_and_provision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('console.log("ready")')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None, lambda **_: None)
            tools.configure_deployment('start', None, 3000, '/', [])
            (tools.work / 'server.js').write_text('const sqlite = require("node:sqlite");')
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                tools.deploy_application()
            self.assertEqual(tools.attempts, 0)

    def test_agent_cannot_add_database_after_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('console.log("ready")')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None, lambda **_: None)
            tools.configure_deployment('start', None, 3000, '/', [])
            (tools.work / 'server.js').write_text('const db = require("pg");')
            with self.assertRaisesRegex(ValueError, '데이터베이스'):
                tools.deploy_application()
            self.assertEqual(tools.attempts, 0)

    def test_auto_target_resolves_before_job_and_keeps_infrastructure_plan(self):
        class Planner:
            def __init__(self, _settings):
                pass
            def propose(self, files, available_targets, public_access):
                return {'target': 'aws-ecs-express', 'workload': 'stateless-http',
                        'rationale': '공개 HTTP 서비스',
                        'evidence': [{'file': 'package.json', 'quote': '"start": "node server.js"'}]}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts": {"start": "node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                      aws_settings=AwsSettings('ap-northeast-2'), monitor_interval=0,
                      infrastructure_planner_factory=Planner)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-OneDeploy-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'auto', 'X-Public-Access': 'true'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('onedeploy.server.AwsSettings.unavailable_reason', return_value=None), \
                    patch('onedeploy.server.threading.Thread'):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            job = app.jobs[handler.json_response.call_args.args[1]['id']]
            self.assertEqual(job['target'], 'aws-ecs-express')
            self.assertEqual(job['requested_target'], 'auto')
            self.assertEqual(job['infrastructure_plan']['planner'], 'openai')
            self.assertEqual(job['infrastructure_profile']['storage'], 'unconfirmed')
            private_handler = handler_class.__new__(handler_class)
            private_handler.path = '/api/deployments'
            private_handler.headers = {**handler.headers, 'X-Public-Access': 'false'}
            private_handler.rfile = io.BytesIO(archive.getvalue())
            private_handler.json_response = Mock()
            with patch('onedeploy.server.AwsSettings.unavailable_reason', return_value=None):
                private_handler.do_POST()
            self.assertEqual(private_handler.json_response.call_args.args[0], 400)
            self.assertIn('사용할 수 없는', private_handler.json_response.call_args.args[1]['error'])
            self.assertEqual(len(app.jobs), 1)


if __name__ == '__main__':
    unittest.main()
