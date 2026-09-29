import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onedeploy.core import LocalDockerAdapter
from onedeploy.server import App, handler_for


JOB_ID = 'd' * 16
ATTEMPT = JOB_ID + '-a1'
NAME = 'onedeploy-' + ATTEMPT
IMAGE = 'onedeploy/' + ATTEMPT + ':latest'


class LocalRetireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name))
        source = self.app.root / JOB_ID / 'source'
        source.mkdir(parents=True)
        self.app.jobs[JOB_ID] = {
            'id': JOB_ID, 'mode': 'agent', 'project': str(source), 'plan': None,
            'status': 'succeeded', 'target': 'local-docker', 'deployment_state': 'active',
            'events': [], 'result': {'container': NAME, 'image': IMAGE, 'url': 'http://127.0.0.1:12345'}}
        self.app.save(JOB_ID)
        handler_class = handler_for(self.app)
        self.handler = handler_class.__new__(handler_class)
        self.handler.path = f'/api/jobs/{JOB_ID}/retire'
        self.handler.headers = {'X-OneDeploy-Token': self.app.token, 'Content-Length': '0'}
        self.handler.json_response = Mock()

    def test_retire_requires_owned_container_and_image(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        container = {'Name': '/' + NAME,
                     'Config': {'Image': IMAGE, 'Labels': {'app': 'onedeploy',
                                                          'onedeploy-attempt': ATTEMPT}}}
        image = {'RepoTags': [IMAGE], 'Config': {'Labels': {'app': 'onedeploy'}}}
        with patch.object(adapter, 'inspect_resource', side_effect=[container, None, image, None]), \
                patch.object(adapter, 'command') as command:
            adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0], ['docker', 'rm', '-f', NAME])
        self.assertEqual(command.call_args_list[1].args[0], ['docker', 'image', 'rm', IMAGE])

    def test_retire_refuses_replaced_container(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        with patch.object(adapter, 'inspect_resource', return_value={
                'Name': '/' + NAME, 'Config': {'Image': IMAGE, 'Labels': {'app': 'other'}}}), \
                patch.object(adapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'ownership changed'):
                adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
            command.assert_not_called()

    def test_retire_refuses_retagged_image_when_container_missing(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        with patch.object(adapter, 'inspect_resource', side_effect=[None, {
                'RepoTags': [IMAGE], 'Config': {'Labels': {'app': 'other'}}}]), \
                patch.object(adapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'image ownership changed'):
                adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
            command.assert_not_called()

    def test_retire_api_and_restart_retry(self):
        with patch('onedeploy.server.threading.Thread') as thread:
            self.handler.do_POST()
            self.handler.json_response.assert_called_once_with(
                202, {'id': JOB_ID, 'deployment_state': 'deleting'})
            thread.assert_called_once()
        recovered = App(self.app.root)
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'delete_failed')
        with patch('onedeploy.server.LocalDockerAdapter.retire') as retire:
            recovered.retire_local(JOB_ID)
            retire.assert_called_once()
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'deleted')
        self.assertIn('retired_at', recovered.jobs[JOB_ID])


if __name__ == '__main__':
    unittest.main()
