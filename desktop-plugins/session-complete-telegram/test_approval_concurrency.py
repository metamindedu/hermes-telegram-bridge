"""Parallel real protected-write producers; all state/transport is fixture owned."""
import copy
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
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


class ApprovalPublicationConcurrencyTests(unittest.TestCase):
    """Drive the real Desktop monitor against the real fake-home SQLite consumer."""

    package = Path(__file__).resolve().parent
    node_test = package / 'approval_concurrency.test.mjs'
    current_plugin = package / 'desktop' / 'plugin.js'
    frozen_plugin = package.parent.parent / '.private/approval-scheduling-20261023-v1/candidate/desktop-plugins/session-complete-telegram/desktop/plugin.js'
    prefix = 'approval-publication-epoch-interleaving'
    scenarios = ('receipt-before-callback', 'publication-choice-before-receipt', 'lookup-before-publication')

    def test_current_publication_interleavings_defer_stale_owner_snapshot(self):
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario):
                fixture = self._fixture()
                try:
                    execution = self._run_node(fixture, self.current_plugin, scenario, expectation='green')
                    self.assertFalse(execution.get('launch_denied'), execution)
                    self.assertEqual(execution['exit_code'], 0, execution)
                    self._assert_green_consumer(fixture, execution)
                finally:
                    fixture.doCleanups()

    def test_frozen_v1_reproduces_stale_owner_wide_poll_rejection(self):
        if not self.frozen_plugin.is_file():
            self.skipTest('local immutable frozen-v1 candidate is unavailable; CI tests current source only')
        fixture = self._fixture()
        try:
            execution = self._run_node(fixture, self.frozen_plugin, 'receipt-before-callback', expectation='baseline-red')
            self.assertFalse(execution.get('launch_denied'), execution)
            self.assertEqual(execution['exit_code'], 1, execution)
            bid = execution['bridge_ids']['b']
            row = fixture.row(bid)
            self.assertEqual(row['status'], 'rejected', 'frozen v1 must reproduce the real consumer rejection')
            self.assertEqual(len(execution['poll_requests']), 1)
            self.assertNotIn(bid, [item['id'] for item in execution['poll_requests'][0]['observations']])
            self.assertEqual(len(execution['native_responses']), 0)
            self.assertEqual(len(execution['claim_valid']), 0)
            events = bridge.diagnostics(bid, state=fixture.state)['events']
            self.assertEqual(events[-1]['source'], 'poll')
            self.assertEqual(events[-1]['reason'], 'native_unverified')
            self.assertNotEqual(events[-1]['reason'], 'owner_lease_lapsed')
            self.assertIn('the stale A-only owner snapshot must not terminally reject queued B', execution['stderr'])
            print('APPROVAL_PUBLICATION_BASELINE_RED_RECEIPT=' + execution['receipt_path'])
        finally:
            fixture.doCleanups()

    def _fixture(self):
        fixture = test_approval_bridge.ApprovalBridgeTests()
        fixture.setUp()
        fixture._assert_core_generation()
        return fixture

    def _dto(self, fixture):
        approvals = {}
        for label, char in (('a', 'a'), ('b', 'b')):
            request, pending = fixture.pair(
                command='synthetic publication-race operation ' + label,
                description='synthetic protected review ' + label,
                key='recursive delete', queue_id=char * 32, srq='srq-' + char * 12)
            descriptor = fixture.descriptor_for(request=request, pending=pending, epoch='fixture-epoch')
            approvals[label] = {'request': request, 'pending': pending, 'descriptor': descriptor}
        return {'route': fixture.route, 'session': fixture.session, 'owner': fixture.owner, 'approvals': approvals}

    def _child_environment(self, fixture, plugin, scenario):
        allowed = (
            'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH', 'PATHEXT',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA', 'APPDATA',
            'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH',
            'HERMES_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
            'HERMES_TEST_TMPDIR', 'TMPDIR', 'TMP', 'TEMP', 'RUNNER_TEMP',
            'HERMES_TEST_ISOLATION_ROOT', 'HERMES_TEST_ISOLATION_ARTIFACT_ROOT',
            'HERMES_TEST_ISOLATION_META_ROOT', 'HERMES_TEST_ISOLATION_OUTSIDE_ROOT',
            'HERMES_TEST_ISOLATION_TOKEN', 'HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED',
            'HERMES_CORE_ROOT', 'HERMES_DISABLE_LAZY_INSTALLS',
        )
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        for key in ('NODE_TEST_CONTEXT', 'NODE_OPTIONS'):
            environment.pop(key, None)
        environment['APPROVAL_CONCURRENCY_PLUGIN_SOURCE'] = str(plugin.resolve())
        environment['APPROVAL_CONCURRENCY_SCENARIO'] = scenario
        forbidden_prefixes = ('TELEGRAM_', 'OPENAI_', 'ANTHROPIC_', 'GOOGLE_', 'SUPABASE_',
                              'CLOUDFLARE_', 'PORTONE_', 'AWS_', 'AZURE_', 'MODAL_')
        self.assertNotIn('NODE_TEST_CONTEXT', environment)
        self.assertNotIn('NODE_OPTIONS', environment)
        self.assertFalse(any(key.upper().startswith(forbidden_prefixes) or
                             key.upper().endswith(('_API_KEY', '_ACCESS_TOKEN', '_CLIENT_SECRET', '_PASSWORD'))
                             for key in environment))
        for key in ('HERMES_HOME', 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA',
                    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'HERMES_TEST_TMPDIR',
                    'TMPDIR', 'TMP', 'TEMP', 'RUNNER_TEMP'):
            self.assertTrue(Path(environment[key]).resolve().is_relative_to(
                Path(os.environ['HERMES_TEST_ISOLATION_ROOT']).resolve()), key)
        self.assertEqual(environment['HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED'], '1')
        self.assertEqual(environment['HERMES_TEST_ISOLATION_TOKEN'], os.environ['HERMES_TEST_ISOLATION_TOKEN'])
        return environment

    def _run_node(self, fixture, plugin, scenario, *, expectation):
        meta = Path(os.environ['HERMES_TEST_ISOLATION_META_ROOT']).resolve(strict=True)
        source = plugin.resolve(strict=True)
        allowed_sources = {path.resolve(strict=True) for path in (self.current_plugin, self.frozen_plugin) if path.is_file()}
        self.assertIn(source, allowed_sources, 'test-only plugin override escaped the fixed source allowlist')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        run_id = uuid.uuid4().hex
        stem = f'approval-publication-{expectation}-{scenario}-{run_id}'
        stdout_path = meta / (stem + '.stdout.jsonl')
        stderr_path = meta / (stem + '.stderr.tap')
        receipt_path = meta / (stem + '.receipt.json')
        command = [
            'node', '--test-isolation=none', '--test', '--test-name-pattern=' + self.prefix,
            '--test-reporter=tap', '--test-reporter-destination=stderr', self.node_test.name,
        ]
        fixture_dto = self._dto(fixture)
        env = self._child_environment(fixture, source, scenario)
        messages = queue.Queue()
        eof = object()
        process = None
        timed_out = False
        polls, poll_results, claim_valid, settlements = [], [], [], []
        registrations, lookups, native_requests, native_receipts, callback_results = [], [], [], [], []
        registration_hold = None
        pending_registration = None
        server_errors = []

        def write_child(message):
            assert process is not None and process.stdin is not None
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n')
            process.stdin.flush()

        def answer(message_id, result=None, error=None):
            response = {'id': message_id}
            if error is not None:
                response['error'] = error
            else:
                response['result'] = result
            write_child(response)

        def event(name, payload=None):
            write_child({'event': name, 'payload': payload})

        def helper(request):
            action = request.get('action')
            if action == 'approval_poll':
                polls.append(copy.deepcopy({'observations': request.get('observations', []),
                                            'unverified': request.get('unverified', []),
                                            'absent': request.get('absent', [])}))
            elif action == 'approval_register':
                registrations.append(copy.deepcopy(request))
            elif action == 'approval_registration_lookup':
                pass
            elif action == 'approval_claim_valid':
                pass
            elif action == 'approval_settle':
                pass
            result = bridge.helper(request, notifier, root=fixture.root, state=fixture.state,
                                   result_budget=24 * 1024)
            if action == 'approval_poll':
                poll_results.append(copy.deepcopy(result))
            elif action == 'approval_registration_lookup':
                lookups.append({'request': copy.deepcopy(request), 'result': copy.deepcopy(result)})
            elif action == 'approval_claim_valid':
                claim_valid.append({'request': copy.deepcopy(request), 'result': copy.deepcopy(result)})
            elif action == 'approval_settle':
                settlements.append({'request': copy.deepcopy(request), 'result': copy.deepcopy(result)})
            return result

        def choose_b():
            if registration_hold is None or not registration_hold.get('id'):
                raise AssertionError('B callback attempted before real publication')
            row = fixture.row(registration_hold['id'])
            if row is None:
                raise AssertionError('B registration did not create a fake-home SQLite row')
            result = bridge.choose(row['token'], 'once',
                fixture.identity(registration_hold['id'], event='synthetic-publication-race-' + scenario),
                state=fixture.state, now=time.time())
            callback_results.append(copy.deepcopy(result))
            return result

        def inspect():
            bid = registration_hold.get('id') if registration_hold else None
            row = fixture.row(bid) if bid else None
            return {
                'b': {'id': bid, 'status': row['status'] if row else None},
                'polls': copy.deepcopy(polls), 'pollResults': copy.deepcopy(poll_results),
                'claimValid': copy.deepcopy(claim_valid), 'settlements': copy.deepcopy(settlements),
                'registers': copy.deepcopy(registrations), 'lookups': copy.deepcopy(lookups),
                'nativeRequests': copy.deepcopy(native_requests),
                'syntheticNativeReceipts': copy.deepcopy(native_receipts),
                'callbackResults': copy.deepcopy(callback_results),
                'pythonConsumerFinalState': {'id': bid, 'status': row['status']} if row else None,
            }

        def handle(message):
            nonlocal registration_hold, pending_registration
            message_id, op = message.get('id'), message.get('op')
            payload = message.get('payload') or {}
            if op == 'begin':
                self.assertEqual(Path(payload['plugin_source']).resolve(strict=True), source)
                self.assertEqual(payload['plugin_source_sha256'], source_hash)
                answer(message_id, {**copy.deepcopy(fixture_dto), 'plugin_source_sha256': source_hash})
                return
            if op == 'helper':
                action = payload.get('action')
                is_b_register = action == 'approval_register' and payload.get('request', {}).get('id') == \
                    fixture_dto['approvals']['b']['request']['id']
                if is_b_register and scenario == 'publication-choice-before-receipt':
                    registration_hold = helper(payload)
                    if registration_hold.get('ok') is not True or registration_hold.get('status') != 'open':
                        raise AssertionError('real B registration did not publish an open card')
                    callback = choose_b()
                    if callback.get('ok') is not True or callback.get('status') != 'queued':
                        raise AssertionError('real B callback did not queue exactly once before receipt')
                    pending_registration = (message_id, copy.deepcopy(registration_hold))
                    event('publication_and_choice_complete')
                    return
                if is_b_register and scenario == 'lookup-before-publication':
                    pending_registration = (message_id, copy.deepcopy(payload))
                    event('registration_write_waiting')
                    return
                result = helper(payload)
                if is_b_register:
                    registration_hold = copy.deepcopy(result)
                    if result.get('ok') is not True or result.get('status') != 'open':
                        raise AssertionError('real B registration receipt did not complete')
                answer(message_id, result)
                return
            if op == 'choose_b':
                result = choose_b()
                answer(message_id, result)
                return
            if op == 'release_registration':
                if not pending_registration or scenario != 'publication-choice-before-receipt':
                    raise AssertionError('no held real publication receipt is awaiting release')
                original_id, result = pending_registration
                pending_registration = None
                answer(original_id, result)
                answer(message_id, {'released': True})
                return
            if op == 'publish_registration':
                if not pending_registration or scenario != 'lookup-before-publication':
                    raise AssertionError('no pre-publication registration is awaiting its normal write')
                original_id, request = pending_registration
                pending_registration = None
                result = helper(request)
                registration_hold = copy.deepcopy(result)
                if result.get('ok') is not True or result.get('status') != 'open':
                    raise AssertionError('normal B registration failed after read-only found:false')
                answer(original_id, result)
                answer(message_id, {'published': True})
                return
            if op == 'native_response':
                native_requests.append(copy.deepcopy(payload))
                result = {'resolved': 1}
                native_receipts.append(copy.deepcopy(result))
                answer(message_id, result)
                return
            if op == 'inspect':
                answer(message_id, inspect())
                return
            raise AssertionError('unexpected Node RPC operation: ' + str(op))

        def read_stream(stream, destination, *, parse=False):
            try:
                with destination.open('x', encoding='utf-8', newline='') as output:
                    for line in iter(stream.readline, ''):
                        output.write(line)
                        output.flush()
                        if parse:
                            try:
                                messages.put(json.loads(line))
                            except json.JSONDecodeError:
                                messages.put({'_protocol_error': line.rstrip()})
            finally:
                if parse:
                    messages.put(eof)

        receipt = {
            'schema': 'approval-publication-interleaving-v1', 'scenario': scenario,
            'expectation': expectation, 'plugin_source': str(source), 'plugin_source_sha256': source_hash,
            'python_bridge_head': subprocess.run(
                ['git', '-C', str(self.package.parent.parent), 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True, timeout=20).stdout.strip(),
            'command': command, 'environment_keys': sorted(env),
            'node_test_context_inherited': 'NODE_TEST_CONTEXT' in env,
            'provider_credentials_inherited': any(key.upper().startswith((
                'TELEGRAM_', 'OPENAI_', 'ANTHROPIC_', 'GOOGLE_', 'SUPABASE_', 'CLOUDFLARE_',
                'PORTONE_', 'AWS_', 'AZURE_', 'MODAL_')) or key.upper().endswith(
                ('_API_KEY', '_ACCESS_TOKEN', '_CLIENT_SECRET', '_PASSWORD')) for key in env),
            'raw_stdout': str(stdout_path), 'raw_stderr': str(stderr_path),
            'receipt_path': str(receipt_path),
        }
        try:
            process = subprocess.Popen(command, cwd=str(self.package), env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding='utf-8', bufsize=1)
        except RuntimeError as error:
            receipt.update(outcome='guard_denied', launch_error_type=type(error).__name__,
                           launch_error=str(error), exit_code=None)
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print('APPROVAL_PUBLICATION_NODE_RECEIPT=' + str(receipt_path))
            return {'launch_denied': True, 'receipt_path': str(receipt_path), 'error': str(error)}

        stdout_reader = threading.Thread(target=read_stream, args=(process.stdout, stdout_path),
                                         kwargs={'parse': True}, daemon=True)
        stderr_reader = threading.Thread(target=read_stream, args=(process.stderr, stderr_path), daemon=True)
        stdout_reader.start(); stderr_reader.start()
        deadline = time.monotonic() + 45
        saw_eof = False
        while not saw_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            try:
                message = messages.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if process.poll() is not None and not stdout_reader.is_alive():
                    break
                continue
            if message is eof:
                saw_eof = True
                break
            if '_protocol_error' in message:
                server_errors.append('non_json_stdout')
                continue
            try:
                handle(message)
            except BaseException as error:
                server_errors.append(type(error).__name__)
                try:
                    answer(message.get('id'), error=type(error).__name__)
                except Exception:
                    pass
        if process.stdin is not None:
            process.stdin.close()
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
            timed_out = True
        stdout_reader.join(timeout=3); stderr_reader.join(timeout=3)
        if process.stdout is not None: process.stdout.close()
        if process.stderr is not None: process.stderr.close()
        if stdout_reader.is_alive() or stderr_reader.is_alive():
            timed_out = True
        stderr = stderr_path.read_text(encoding='utf-8', errors='replace') if stderr_path.exists() else ''
        receipt.update(
            outcome='timeout' if timed_out else ('completed' if exit_code == 0 else 'node_test_failed'),
            exit_code=exit_code, duration_seconds=round(time.monotonic() - (deadline - 45), 3),
            server_error_types=server_errors, b_bridge_id=registration_hold.get('id') if registration_hold else None,
            poll_requests=polls, poll_results=poll_results, claim_valid=claim_valid,
            settlements=settlements, registers=registrations, lookups=lookups,
            native_requests=native_requests, native_receipts=native_receipts,
            callback_results=callback_results, python_consumer_final_state=inspect()['pythonConsumerFinalState'],
        )
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('APPROVAL_PUBLICATION_NODE_RECEIPT=' + str(receipt_path))
        return {**receipt, 'receipt_path': str(receipt_path), 'stderr': stderr,
                'bridge_ids': {'b': registration_hold.get('id') if registration_hold else None},
                'native_responses': native_requests, 'poll_requests': polls,
                'claim_valid': claim_valid, 'settlements': settlements,
                'lookups': lookups, 'server_error_types': server_errors,
                'native_receipts': native_receipts, 'callback_results': callback_results,
                'final_state': inspect()['pythonConsumerFinalState']}

    def _assert_green_consumer(self, fixture, execution):
        self.assertEqual(execution['server_error_types'], [], execution)
        self.assertEqual(execution['provider_credentials_inherited'], False)
        self.assertEqual(execution['node_test_context_inherited'], False)
        bid = execution['bridge_ids']['b']
        self.assertIsNotNone(bid)
        self.assertEqual(fixture.row(bid)['status'], 'applied')
        self.assertEqual(len(execution['poll_requests']), 1)
        self.assertEqual(len(execution['claim_valid']), 1)
        self.assertTrue(execution['claim_valid'][0]['result']['valid'])
        self.assertEqual(len(execution['settlements']), 1)
        self.assertEqual(execution['settlements'][0]['result']['status'], 'applied')
        self.assertEqual(len(execution['callback_results']), 1)
        self.assertEqual(execution['callback_results'][0]['status'], 'queued')
        self.assertEqual(len(execution['native_responses']), 1)
        self.assertEqual(execution['native_responses'][0], {
            'session_id': fixture.session['id'], 'request_id': 'b' * 32, 'choice': 'once', 'all': False})
        self.assertEqual(execution['native_receipts'], [{'resolved': 1}])
        self.assertEqual(execution['final_state'], {'id': bid, 'status': 'applied'})
        self.assertEqual(execution['registers'][-1]['request']['id'], 'srq-' + 'b' * 12)
        self.assertEqual(len([item for item in execution['registers']
                              if item['request']['id'] == 'srq-' + 'b' * 12]), 1)
        if execution['scenario'] == 'lookup-before-publication':
            self.assertEqual(len(execution['lookups']), 1)
            self.assertEqual(execution['lookups'][0]['result'], {'ok': True, 'found': False})
        print('APPROVAL_PUBLICATION_GREEN_RECEIPT=' + execution['receipt_path'])
