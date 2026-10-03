"""Fake Responses transport for a real local deployment; never a product model."""
from __future__ import annotations

import io
import json
import threading
import urllib.request


class ResponsesWireFixture:
    def __init__(self, actions=None, *, expected_target='local-docker', planner_requests=1,
                 managed_postgres=False, expected_model='wire-fixture-model'):
        self.local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.lock = threading.Lock()
        self.planner_requests = 0
        self.agent_requests = 0
        self.expected_target = expected_target
        self.expected_planner_requests = planner_requests
        self.managed_postgres = managed_postgres
        self.expected_model = expected_model
        self.actions = actions if actions is not None else [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'package.json', 'old_text': '"scripts": {}',
                                     'new_text': '"scripts": {"start": "node server.js"}'}),
            ('apply_project_patch', {'path': 'server.js',
                                     'old_text': ").listen(4321, '127.0.0.1',",
                                     'new_text': ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ]

    def open(self, request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url.startswith('http://127.0.0.1:'):
            return self.local_opener.open(request, timeout=timeout)
        if url != 'https://api.openai.com/v1/responses':
            raise AssertionError('Unexpected network request: ' + url)
        assert request.get_header('Authorization') == 'Bearer wire-fixture-key'
        payload = json.loads(request.data)
        assert payload['model'] == self.expected_model and payload['store'] is False
        with self.lock:
            body = self._planner_response(payload) if 'text' in payload else self._agent_response(payload)
        return io.BytesIO(json.dumps(body).encode())

    def _planner_response(self, payload):
        assert self.planner_requests == 0 and self.expected_planner_requests == 1
        assert payload['text']['format']['type'] == 'json_schema'
        assert payload['text']['format']['strict'] is True
        context = json.loads(payload['input'])
        assert 'local-docker' in context['available_targets']
        assert '"scripts": {}' in context['files']['package.json']
        self.planner_requests += 1
        proposal = {'target': 'local-docker', 'workload': 'stateless-http',
                    'rationale': '로컬 Docker 배포 경로를 검증합니다.',
                    'evidence': [{'file': 'package.json', 'quote': '"scripts": {}'}]}
        return {'status': 'completed', 'output': [{'type': 'message', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': json.dumps(proposal)}]}]}

    def _agent_response(self, payload):
        index = self.agent_requests
        assert index < len(self.actions)
        assert payload['tool_choice'] == 'required' and payload['parallel_tool_calls'] is False
        assert payload['include'] == ['reasoning.encrypted_content']
        assert all(tool['strict'] is True for tool in payload['tools'])
        history = payload['input']
        if index == 0:
            assert len(history) == 1 and history[0]['role'] == 'user'
            initial = json.loads(history[0]['content'])
            assert initial['target'] == self.expected_target
            assert initial['managed_postgres_connection'] is self.managed_postgres
        else:
            reasoning, function_call, tool_result = history[-3:]
            assert reasoning['type'] == 'reasoning'
            assert reasoning['encrypted_content'] == f'opaque-{index}'
            assert function_call['type'] == 'function_call'
            assert function_call['call_id'] == f'call_{index}'
            assert tool_result['type'] == 'function_call_output'
            assert tool_result['call_id'] == function_call['call_id']
            result = json.loads(tool_result['output'])
            assert 'error' not in result, result
            previous_name, previous_args = self.actions[index - 1]
            if previous_name == 'read_project_files':
                assert set(previous_args['paths']) <= set(result['files'])
            if previous_name == 'apply_project_patch':
                assert result['changed'] == previous_args['path']
            if previous_name == 'configure_deployment':
                assert result['ready'] is True
        name, arguments = self.actions[index]
        self.agent_requests += 1
        return {'status': 'completed', 'output': [
            {'type': 'reasoning', 'id': f'rs_{index + 1}',
             'encrypted_content': f'opaque-{index + 1}', 'summary': []},
            {'type': 'function_call', 'id': f'fc_{index + 1}',
             'call_id': f'call_{index + 1}', 'name': name,
             'arguments': json.dumps(arguments), 'status': 'completed'},
        ]}

    def assert_complete(self):
        assert self.planner_requests == self.expected_planner_requests, self.planner_requests
        assert self.agent_requests == len(self.actions), self.agent_requests
