import ast
import copy
import io
import hmac
import json
from functools import wraps
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from test_server import server  # Loads the worker from this checkout.
import approvals


class Capture(io.StringIO):
    def close(self):
        pass


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.out = Capture()
        self.records = []
        self.events = []
        self.s = approvals.AppServerSession(
            self.out, lock=threading.RLock(), thread_params={}, turn_params={},
            on_event=self.events.append, on_approvals=lambda r: self.records.append(copy.deepcopy(r)),
            on_final=lambda x: None, is_active=lambda: True, redact=lambda s: s.replace('SENSITIVE', '[redacted]'))
        self.s.thread_id, self.s.turn_id = 'thread', 'turn'
        self.addCleanup(self.s.close)

    def form(self, schema=None, **extra):
        return {'id': 71, 'method': approvals.MCP_ELICITATION, 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'serverName': 'local-smoke-test',
            'mode': 'form', 'message': 'Allow the harmless local echo operation?',
            'requestedSchema': schema or {'type': 'object', 'properties': {
                'confirm': {'type': 'boolean', 'title': 'Confirm echo'}}, 'required': ['confirm']}, **extra}}

    def input(self, method='tool/requestUserInput', **extra):
        return {'id': 'input-72', 'method': method, 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'itemId': 'i', 'isBlocking': True,
            'questions': [{'id': 'choice', 'header': 'Echo', 'question': 'Allow local echo?',
                           'options': [{'label': 'Accept', 'description': 'Run the echo'},
                                       {'label': 'Decline', 'description': 'Do nothing'}]}], **extra}}

    def record(self):
        return self.records[-1][-1]

    def response(self):
        return json.loads(self.out.getvalue().splitlines()[-1])

    def test_harmless_gated_echo_requires_explicit_approval(self):
        executed = []
        self.s.receive(self.form())
        self.assertEqual(self.out.getvalue(), '')
        self.assertEqual(self.record()['status'], 'pending')
        self.s.decide(self.record()['approval_id'], 'allow', {'confirm': True})
        r = self.response()
        if r['result'] == {'action': 'accept', 'content': {'confirm': True}}:
            executed.append('echo only; no network or HA calls')
        self.assertEqual(r['id'], 71)
        self.assertEqual(len(executed), 1)

    def test_form_reject_uses_official_response(self):
        self.s.receive(self.form())
        self.s.decide(self.record()['approval_id'], 'deny')
        self.assertEqual(self.response(), {'id': 71, 'result': {'action': 'decline', 'content': None}})

    def test_missing_or_forged_content_does_not_consume_request(self):
        self.s.receive(self.form())
        key = self.record()['approval_id']
        for content in [None, {}, {'confirm': 1}, {'confirm': 'true'}, {'confirm': True, 'extra': True}]:
            with self.assertRaises(ValueError): self.s.decide(key, 'allow', content)
        self.assertIn(key, self.s._pending)
        self.assertEqual(self.out.getvalue(), '')

    def test_form_enum_preserves_wire_value(self):
        schema = {'type': 'object', 'properties': {'action': {'type': 'string', 'oneOf': [
            {'const': 'approve_once', 'title': 'Approve once'}, {'const': 'reject', 'title': 'Reject'}]}},
            'required': ['action']}
        self.s.receive(self.form(schema))
        self.s.decide(self.record()['approval_id'], 'allow', {'action': 'approve_once'})
        self.assertEqual(self.response()['result']['content'], {'action': 'approve_once'})

    def test_empty_form_returns_required_empty_content(self):
        self.s.receive(self.form({'type': 'object', 'properties': {}}))
        self.s.decide(self.record()['approval_id'], 'allow', {})
        self.assertEqual(self.response()['result'], {'action': 'accept', 'content': {}})

    def test_unsupported_forms_fail_closed(self):
        for request in [self.form(mode='url'), self.form(mode='future'),
                        self.form({'type': 'object', 'properties': {'password': {'type': 'string'}}}),
                        self.form({'type': 'array', 'properties': {}}),
                        self.form(message='SENSITIVE'), self.form(message='x' * 33000)]:
            self.s.receive(request)
            self.assertEqual(self.response()['result']['action'], 'decline')
            self.assertEqual(self.record()['status'], 'denied')
            self.assertFalse(self.record()['can_allow'])

    def test_null_turn_id_allowed_for_mcp_only(self):
        self.s.receive(self.form(turnId=None))
        self.assertEqual(self.out.getvalue(), '')

    def test_wrong_thread_rejected(self):
        self.s.receive(self.form(threadId='other'))
        self.assertEqual(self.response()['result']['action'], 'decline')
        self.assertEqual(self.records, [])

    def test_request_limit_fails_closed(self):
        with patch.object(approvals, 'REQUEST_LIMIT', 0):
            self.s.receive(self.form())
        self.assertEqual(self.response()['result']['action'], 'decline')
        self.assertFalse(self.s._pending)

    def test_redacted_option_cannot_be_approved(self):
        request = self.input()
        request['params']['questions'][0]['options'][0]['description'] = 'SENSITIVE'
        self.s.receive(request)
        self.assertFalse(self.record()['can_allow'])
        self.assertEqual(self.record()['fields'], [])

    def test_unknown_request_keeps_error_fallback(self):
        self.s.receive({'id': 99, 'method': 'unknown/request', 'params': {}})
        self.assertEqual(self.response()['error']['code'], -32601)

    def test_invalid_mode_is_rejected(self):
        self.s.receive(self.form(mode=['invalid']))
        self.assertEqual(self.response()['result']['action'], 'decline')

    def test_user_input_protocol_and_alias(self):
        for method in approvals.USER_INPUT:
            self.s.receive(self.input(method))
            self.s.decide(self.record()['approval_id'], 'allow', {'choice': 'Accept'})
            self.assertEqual(self.response(), {'id': 'input-72', 'result': {
                'answers': {'choice': {'answers': ['Accept']}}}})

    def test_user_input_reject_selects_explicit_decline(self):
        self.s.receive(self.input())
        self.s.decide(self.record()['approval_id'], 'deny')
        self.assertEqual(self.response()['result'], {'answers': {'choice': {'answers': ['Decline']}}})

    def test_secret_and_freeform_questions_are_not_published(self):
        for flag in ['isSecret', 'isOther']:
            request = self.input()
            request['params']['questions'][0][flag] = True
            self.s.receive(request)
            self.assertFalse(self.record()['can_allow'])
            self.assertEqual(self.response()['result'], {'answers': {}})

    def test_timer_is_bounded_and_rejects(self):
        with patch.object(approvals.threading, 'Timer') as timer:
            self.s.receive(self.input(autoResolutionMs=1000))
            self.assertEqual(timer.call_args.args[0], 1)
            timer.return_value.start.assert_called_once()
            self.s._expire_mcp(self.record()['approval_id'])
            self.assertEqual(self.response()['result']['answers']['choice']['answers'], ['Decline'])

    def test_duplicate_decision_is_not_replayed(self):
        self.s.receive(self.form())
        key = self.record()['approval_id']
        self.s.decide(key, 'deny')
        before = self.out.getvalue()
        with self.assertRaises(LookupError): self.s.decide(key, 'allow', {'confirm': True})
        self.assertEqual(self.out.getvalue(), before)

    def test_server_resolution_cancels_timer(self):
        self.s.receive(self.form())
        key = self.record()['approval_id']
        self.s.receive({'method': 'serverRequest/resolved', 'params': {'threadId': 'thread', 'requestId': 71}})
        self.assertNotIn(key, self.s._pending_mcp)
        self.assertNotIn(key, self.s._mcp_timers)
        self.assertEqual(self.record()['status'], 'expired')

    def test_close_rejects_without_approving(self):
        self.s.receive(self.form())
        self.s.close()
        replies = [json.loads(line) for line in self.out.getvalue().splitlines()]
        self.assertIn({'id': 71, 'result': {'action': 'decline', 'content': None}}, replies)
        self.assertFalse(self.s._mcp_timers)

    def test_command_approvals_unchanged(self):
        self.s.receive({'id': 80, 'method': approvals.COMMAND_APPROVAL, 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'command': 'echo harmless', 'cwd': '/tmp'}})
        self.s.decide(self.record()['approval_id'], 'allow')
        self.assertEqual(self.response(), {'id': 80, 'result': {'decision': 'accept'}})

    def test_file_approvals_unchanged(self):
        self.s._file_changes['file'] = [{'path': '/tmp/example', 'diff': '+example', 'kind': 'add'}]
        self.s.receive({'id': 81, 'method': approvals.FILE_APPROVAL, 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'itemId': 'file'}})
        self.s.decide(self.record()['approval_id'], 'deny')
        self.assertEqual(self.response(), {'id': 81, 'result': {'decision': 'decline'}})

    def test_broad_permissions_still_denied(self):
        self.s.receive({'id': 82, 'method': 'item/permissions/requestApproval', 'params': {}})
        self.assertEqual(self.response(), {'id': 82, 'result': {'permissions': {}, 'scope': 'turn'}})

    def test_endpoint_keeps_auth_and_turn_checks(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text())
        route = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'decide_approval')
        self.assertIn('require_auth', [n.id for n in route.decorator_list if isinstance(n, ast.Name)])
        text = ast.unparse(route)
        self.assertIn('current_turn_id', text)
        self.assertIn("payload.get('content')", text)

    def test_authenticated_endpoint_forwards_content_only_for_current_turn(self):
        from flask import Flask, Response, jsonify, request
        self.s.receive(self.form())
        key = self.record()['approval_id']
        app = Flask('approval-test')
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text())
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'require_auth', 'decide_approval'}]
        env = {'app': app, 'Response': Response, 'jsonify': jsonify, 'request': request, 'wraps': wraps,
               'hmac': hmac, 'time': time, 'is_ingress_request': lambda: False,
               'api_token': lambda: 'test-fixture-only', 'integration_state': {},
               'lock': threading.RLock(), 'tasks': {'task': {'current_turn_id': 'turn'}},
               'approval_sessions': {'task': self.s}, 'APPROVAL_GONE': approvals.APPROVAL_GONE}
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'approval-route-test', 'exec'), env)
        client = app.test_client()
        path = '/tasks/task/approvals/' + key
        data = {'turn_id': 'turn', 'decision': 'allow', 'content': {'confirm': True}}
        self.assertEqual(client.post(path, json=data).status_code, 401)
        self.assertEqual(self.out.getvalue(), '')
        headers = {'X-Codex-Worker-Token': 'test-fixture-only'}
        self.assertEqual(client.post(path, json={**data, 'turn_id': 'old'}, headers=headers).status_code, 409)
        self.assertEqual(client.post(path, json={**data, 'content': {'confirm': 'true'}}, headers=headers).status_code, 400)
        self.assertEqual(client.post(path, json=data, headers=headers).status_code, 200)
        self.assertEqual(self.response(), {'id': 71, 'result': {'action': 'accept', 'content': {'confirm': True}}})
        self.assertEqual(client.post(path, json=data, headers=headers).status_code, 409)


if __name__ == '__main__':
    unittest.main()
