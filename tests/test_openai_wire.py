"""Exercise the production Responses planner and agent through mocked HTTP transport."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from openai_wire_fixture import ResponsesWireFixture
from onedeploy.analysis import AISettings, DEFAULT_AI_MODEL
from onedeploy.core import LocalDockerAdapter
from onedeploy.server import App, handler_for


class OpenAIWireTests(unittest.TestCase):
    def test_auto_plan_and_source_repair_use_responses_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                for source in sorted(Path('examples/unready-node').iterdir()):
                    bundle.write(source, source.name)
            fixture = ResponsesWireFixture(expected_model=DEFAULT_AI_MODEL)
            with patch('urllib.request.build_opener', return_value=fixture), \
                    patch.object(LocalDockerAdapter, 'deploy', return_value={'url': 'http://127.0.0.1:12345'}) as deploy, \
                    patch('onedeploy.server.threading.Thread') as worker, \
                    patch.dict('os.environ', {'OPENAI_API_KEY': 'wire-fixture-key',
                                              'ONEDEPLOY_AI_MODEL': ''}):
                app = App(state, monitor_interval=0)
                self.assertEqual(app.ai_settings.model, DEFAULT_AI_MODEL)
                handler_class = handler_for(app)
                handler = handler_class.__new__(handler_class)
                handler.path = '/api/deployments'
                handler.headers = {'X-OneDeploy-Token': app.token,
                                   'X-Deploy-Target': 'auto',
                                   'Content-Length': str(len(archive.getvalue()))}
                handler.rfile = io.BytesIO(archive.getvalue())
                handler.json_response = Mock()
                handler.do_POST()
                self.assertEqual(handler.json_response.call_args.args[0], 202)
                worker.assert_called_once()
                job_id = handler.json_response.call_args.args[1]['id']
                app.run_agent(job_id)
            fixture.assert_complete()
            job = app.jobs[job_id]
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(job['target'], 'local-docker')
            self.assertEqual(job['infrastructure_plan']['planner'], 'openai')
            self.assertEqual(job['attempts'], 1)
            self.assertEqual({change['path'] for change in job['changes']},
                             {'package.json', 'server.js'})
            self.assertEqual(json.loads((Path(job['project']) / 'package.json').read_text())['scripts'], {})
            work = state / job_id / 'work'
            self.assertIn('process.env.PORT', (work / 'server.js').read_text())
            self.assertIn('0.0.0.0', (work / 'server.js').read_text())
            self.assertEqual(deploy.call_count, 1)
            self.assertEqual(deploy.call_args.args[2], job_id + '-a1')


if __name__ == '__main__':
    unittest.main()
