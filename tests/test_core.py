import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from onedeploy.core import ImageBuilder, LocalDockerAdapter, analyze, extract_project, make_plan


class CoreTests(unittest.TestCase):
    def test_nested_archive_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "app.zip", "w") as z:
                z.writestr("app/package.json", '{"scripts":{"start":"node app.js"}}')
                z.writestr("app/.env", "SECRET=private")
                z.writestr("app/.env.production", "SECRET=private")
            project = extract_project(root / "app.zip", root / "out")
            self.assertEqual(project.name, "app")
            self.assertFalse((project / ".env").exists())
            self.assertFalse((project / ".env.production").exists())
            self.assertEqual(analyze(project).start_command, "npm start")

    def test_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "app.zip", "w") as z:
                z.writestr("../escape", "bad")
            with self.assertRaises(ValueError):
                extract_project(root / "app.zip", root / "out")
            self.assertFalse((root / "escape").exists())

    def test_start_required_and_lockfile_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{}')
            with self.assertRaises(ValueError):
                analyze(root)
            (root / "package.json").write_text(json.dumps({"scripts": {"start": "node x.js", "build": "tsc"}}))
            (root / "package-lock.json").write_text('{}')
            plan = analyze(root)
            self.assertIn("RUN npm ci", plan.dockerfile)
            self.assertIn("RUN npm run build", plan.dockerfile)

    def test_existing_dockerfile_is_built_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{}}')
            dockerfile = 'FROM node:22-alpine\nCOPY . /app\nCMD ["node", "/app/server.js"]\n'
            (root / "Dockerfile").write_text(dockerfile)
            (root / ".dockerignore").write_text("coverage\n")
            plan = analyze(root)
            self.assertEqual(plan.dockerfile_source, "existing")
            self.assertEqual(plan.dockerfile, dockerfile)
            with self.assertRaisesRegex(ValueError, 'start_script=dockerfile'):
                make_plan(root, 'start', None)
            commands = []
            ImageBuilder(lambda args: commands.append(args), lambda *_: None).build(root, plan, "test:latest")
            self.assertEqual((root / "Dockerfile").read_text(), dockerfile)
            self.assertIn('coverage\n', (root / '.dockerignore').read_text())
            self.assertIn('.env.*\n', (root / '.dockerignore').read_text())
            self.assertEqual(commands[0][-3:], ['-t', 'test:latest', str(root)])

    def test_dockerfile_only_archive_is_valid_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / 'app.zip', 'w') as bundle:
                bundle.writestr('app/Dockerfile', 'FROM python:3.13-alpine\nCMD ["python", "server.py"]\n')
                bundle.writestr('app/server.py', 'print("hello")')
                bundle.writestr('app/.venv/secret.txt', 'excluded')
            project = extract_project(root / 'app.zip', root / 'out')
            self.assertEqual(project, root / 'out' / 'app')
            self.assertFalse((project / '.venv').exists())
            plan = analyze(project)
            self.assertEqual(plan.runtime, 'custom-dockerfile')
            self.assertEqual(plan.framework, 'container')
            self.assertEqual(plan.dockerfile_source, 'existing')

    def test_failed_readiness_cleans_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"start":"node x.js"}}')
            adapter = LocalDockerAdapter(lambda *_: None)
            with patch.object(adapter, "command") as command, patch('onedeploy.core.time.sleep'), patch('onedeploy.core.urllib.request.build_opener') as opener:
                command.side_effect = ["", "container", "127.0.0.1:12345", "logs", ""]
                opener.return_value.open.side_effect = OSError("not ready")
                with self.assertRaisesRegex(RuntimeError, "HTTP 200"):
                    adapter.deploy(root, analyze(root), "test")
                self.assertEqual(command.call_args.args[0], ["docker", "rm", "-f", "onedeploy-test"])


if __name__ == '__main__':
    unittest.main()
