"""Parallel real protected-write producers; all state/transport is fixture owned."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import test_approval_bridge
import approval_bridge as bridge
import notifier


class ParallelApprovalTests(unittest.TestCase):
    def test_two_native_protected_writes_publish_independent_full_cards(self):
        fixture = test_approval_bridge.ApprovalBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture._assert_core_generation()
        from tools import approval as native, approval_context
        from tools.file_tools_write_guards import _request_protected_instruction_approval
        from tui_gateway import server, server_requests

        server._approval_request_payload({'command': 'synthetic redactor preparation'})
        sid = 'parallel-native-' + uuid.uuid4().hex[:12]
        key = 'parallel-stored-' + uuid.uuid4().hex[:12]
        route = fixture.route
        row = {'id': sid, 'session_key': key, 'status': 'waiting'}
        server._sessions[sid] = {'session_key': key}
        published = threading.Event()
        results, errors, threads = [], [], []

        def notify(data):
            server._emit_approval_request(sid, data)
            if len(server_requests.open_requests(sid)) == 2:
                published.set()

        def producer(path):
            try:
                results.append(_request_protected_instruction_approval([path]))
            except BaseException as error:
                errors.append(type(error).__name__)

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(server_requests, '_write', lambda _frame: None))
                stack.enter_context(patch.object(server_requests, '_emit', lambda *a, **k: None))
                stack.enter_context(patch.object(native, '_presence', return_value=(None, False, True, False)))
                stack.enter_context(patch.object(native, 'get_current_session_key', return_value=key))
                stack.enter_context(patch.object(approval_context, 'get_current_session_key', return_value=key))
                stack.enter_context(patch.object(native, '_unattended_contexts', return_value=[]))
                stack.enter_context(patch.object(approval_context, '_get_approval_mode', return_value='on'))
                native.register_gateway_notify(key, notify)
                for path in ['AGENTS.md', 'CLAUDE.md']:
                    thread = threading.Thread(target=producer, args=(path,), daemon=True)
                    threads.append(thread)
                    thread.start()
                self.assertTrue(published.wait(8), 'both real producer requests must be pending together')
                self.assertEqual(errors, [])
                requests = copy.deepcopy(server_requests.open_requests(sid))
                queue = copy.deepcopy(native.list_gateway_approvals(key))
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(queue), 2)
                self.assertEqual(len({request['id'] for request in requests}), 2)
                entries = []
                for request in requests:
                    pending = next(item for item in queue if item['request_id'] == request['params']['request_id'])
                    self.assertFalse(request['params']['allow_session'])
                    self.assertFalse(request['params']['allow_permanent'])
                    descriptor = fixture.descriptor_for(request, pending, session=row, epoch='parallel-epoch')
                    receipt = bridge.register({'action': 'approval_register', 'capability': bridge.CAPABILITY,
                        'owner': fixture.owner, 'descriptor': descriptor, 'request': request, 'pending': pending,
                        'route': route, 'session': row, 'epoch': 'parallel-epoch'},
                        notifier, root=fixture.root, state=fixture.state, now=time.time())
                    self.assertTrue(receipt.get('ok'), receipt)
                    entries.append({'request': request, 'pending': pending, 'descriptor': descriptor, 'receipt': receipt})
                sends = [payload for method, payload in fixture.calls if method == 'sendMessage']
                self.assertEqual(len(sends), 2)
                self.assertTrue(all('명령 (전체)' in payload['text'] for payload in sends))
                self.assertTrue(all(len(payload['reply_markup']['inline_keyboard'][0]) == 2 for payload in sends))
                self.assertNotEqual(entries[0]['receipt']['id'], entries[1]['receipt']['id'])
                artifact = Path(os.environ['HERMES_TEST_ISOLATION_META_ROOT']) / 'parallel-approval-native.json'
                artifact.write_text(json.dumps({'route': route, 'row': row, 'owner': fixture.owner,
                    'epoch': 'parallel-epoch', 'entries': entries}, ensure_ascii=False), encoding='utf-8')
                print('PARALLEL_NATIVE_SNAPSHOT=' + str(artifact))
                # Exact one-request denies settle actual native waiters, without executing writes.
                for index, entry in enumerate(entries):
                    receipt = server._methods['approval.respond']('parallel-deny-' + str(index),
                        {'session_id': sid, 'request_id': entry['pending']['request_id'], 'choice': 'deny', 'all': False})
                    self.assertEqual(receipt['result']['resolved'], 1)
                    if index == 0:
                        self.assertEqual(len(native.list_gateway_approvals(key)), 1)
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 2)
                self.assertTrue(all(isinstance(result, str) and result.startswith('BLOCKED:') and
                    'denied by the user' in result for result in results))
        finally:
            native.unregister_gateway_notify(key)
            server_requests.cancel(sid, 'fixture cleanup')
            for thread in threads:
                thread.join(5)
            server._sessions.pop(sid, None)
