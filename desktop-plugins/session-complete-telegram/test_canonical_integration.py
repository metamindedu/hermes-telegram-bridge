"""Current-core canonical transport integration through the registered fixed CLI.

Telegram Bot API calls are mocked. The WebSocket carries real GatewayClient.rpc
frames through the actual current AuthorityConnection/SessionAuthority and live
PendingControls waiter; the loopback identity gate and endpoint discovery are
fixture stubs. This is not the separate native-ticket/live-owner qualification.
"""
from __future__ import annotations

import sys

_DB_ISOLATION = sys.modules.get("_hermes_telegram_bridge_db_isolation")
if _DB_ISOLATION is None:
    raise RuntimeError(
        "test_canonical_integration requires the pre-import DB fence; run "
        "python -I -S guarded_unittest.py --phase canonical"
    )
_DB_ISOLATION.require_ready()

import argparse
import asyncio
import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_support
import newcanonical_backend as source_backend

CURRENT_REF = "297552a9d646119871391d202509822cd86330df"
LEGACY_REF = "74306c1d1b080837744faca3e371267dd09dda4b"
PLUGIN_ID = "session-complete-telegram"
TOKEN = "77:synthetic-token"
CHAT = 99
BOT = 77

NODE_JOINED_ADAPTER = r'''import {readFileSync} from 'node:fs';
import {registerHooks, stripTypeScriptTypes} from 'node:module';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host={}', shortCircuit:true};
  if (specifier === 'react/jsx-runtime') return {url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true};
  return nextResolve(specifier, context);
}});
const input=createInterface({input:process.stdin,crlfDelay:Infinity});
const iterator=input[Symbol.asyncIterator]();
const nextLine=async()=>{
  const next=await iterator.next();
  if(next.done)throw new Error('stdio adapter input closed before a fixed-CLI reply');
  return next.value;
};
const newline=String.fromCharCode(10);
const emit=value=>process.stdout.write(JSON.stringify(value)+newline);
const initial=JSON.parse(await nextLine());
const event=initial.event;
const source=readFileSync(resolve(process.env.HERMES_CORE_ROOT,'apps/desktop/src/api/canonical-protocol.ts'),'utf8');
const code=stripTypeScriptTypes(source,{mode:'transform'});
const sdk=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
(new sdk.CanonicalDesktopProtocol()).event(event);
const {CompletionMonitor,APPROVAL_CAPABILITY}=await import(pathToFileURL(resolve('desktop/plugin.js')).href);
const route=initial.route;
const routes=[route,{mode:'local',connectionId:route.connectionId,profile:'default',targetProfile:'default'}];
const atom=value=>({get:()=>value});
const api={profileRoutes:async()=>routes,notify:()=>{},
  state:{gateway:atom('open'),connectionId:atom(route.connectionId),profile:atom(route.profile)}};
const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});
monitor.approvalCapable=true;
monitor.helper=async request=>{
  emit({type:'helper',request});
  const response=JSON.parse(await nextLine());
  if(response.type!=='reply')throw new Error('stdio adapter received a non-reply frame');
  return response.value;
};
await monitor.onEvent(event);
const record=[...monitor.records.values()][0];
emit({type:'done',owner:monitor.replyOwner,approval_id:record?.approval?.id??null,
  canonical_observed_key:record?.canonicalObservedKey??null,
  prompt_id:record?.canonicalAsk?.prompt?.prompt_id??null,
  generation:record?.canonicalAsk?.prompt?.execution_generation??null});
input.close();
'''


def _core_root():
    root = Path(os.environ["HERMES_CORE_ROOT"]).resolve(strict=True)
    if root != _DB_ISOLATION.core_root:
        raise AssertionError("test core root differs from the pre-import pinned checkout")
    return root


def _core_generation(root: Path) -> str:
    return "current" if (root / "gateway" / "session_authority.py").is_file() and \
        (root / "hermes_cli" / "gateway_client.py").is_file() else "legacy"


def _module_in_core(module, root: Path):
    return _DB_ISOLATION.assert_module_origin(module, root)


def _frame_result(output: str) -> dict:
    lines = output.splitlines()
    if len(lines) != 3 or lines[0] != "HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN" or \
            lines[2] != "HERMES_TELEGRAM_BRIDGE_RESULT_V1_END":
        raise AssertionError(f"fixed CLI did not return exactly one result frame: {output!r}")
    data = base64.urlsafe_b64decode(lines[1] + "=" * (-len(lines[1]) % 4))
    return json.loads(data.decode("utf-8"))


class NativeGatewayFixture:
    """Loopback-only canonical owner; actor authentication is the explicit fixture stub."""
    def __init__(self, target_home: Path, helper_home: Path):
        self.target_home = target_home
        self.helper_home = helper_home
        self.started = threading.Event()
        self.stop = threading.Event()
        self.waiter_ready = threading.Event()
        self.thread = None
        self.loop = None
        self.owner = None
        self.desktop = None
        self.live = None
        self.ref = None
        self.generation = None
        self.desktop_snapshot = None
        self.ws_url = None
        self.rpc_frames = []
        self.waiter_result = None
        self.waiter_error = None
        self.notify_error = None
        self.waiter_thread = None
        self.thread_error = None
        self.desktop_subscribers = None
        self.store = None
        self.db_receipt = None
        self.prompt_id = "opaque.native.ID/reused"

    def start(self):
        self.thread = threading.Thread(target=self._thread_main, name="fixture-canonical-owner", daemon=True)
        self.thread.start()
        if not self.started.wait(45):
            self.stop.set()
            if self.thread:
                self.thread.join(timeout=10)
            raise RuntimeError(f"fixture owner startup timeout: {self.thread_error!r}")
        if self.thread_error:
            raise RuntimeError("fixture owner failed") from self.thread_error
        return self

    def _thread_main(self):
        scope_token = None
        reset_home = None
        try:
            _DB_ISOLATION.require_native_fences()
            _DB_ISOLATION.validate_path(self.target_home)
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
            reset_home = reset_hermes_home_override
            scope_token = set_hermes_home_override(self.target_home)
            # The public context-local home override is installed in this owner
            # thread before any SessionStore/SessionDB import or construction.
            asyncio.run(self._owner_main())
        except BaseException as exc:
            self.thread_error = exc
            self.started.set()
        finally:
            try:
                if self.store is not None:
                    self.store.close_all_db_handles()
            except BaseException as exc:
                if self.thread_error is None:
                    self.thread_error = exc
            finally:
                if scope_token is not None and reset_home is not None:
                    reset_home(scope_token)

    async def _owner_main(self):
        _DB_ISOLATION.require_native_fences()
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from gateway.session_authority import initialize_session_authority
        from gateway.session_controls import AuthorityConnection
        from gateway.session_local import create_local_session
        from gateway.session_policy import build_policy
        from hermes_state_runtime import admit_session_input, claim_session_input
        from websockets.asyncio.server import serve

        self.loop = asyncio.get_running_loop()
        store = SessionStore(self.target_home / "sessions", GatewayConfig())
        self.store = store
        db = store._db
        # SessionStore's sessions_dir is not its state.db authority. Prove the
        # actual SessionDB path and backing SQLite file before any epoch request.
        self.db_receipt = _DB_ISOLATION.verify_session_db(db, self.target_home / "state.db")
        runner = SimpleNamespace(session_store=store, _session_db=db, adapters={},
            _draining=False, _cached_agent_for=lambda _route: None,
            _adapter_for_source=lambda _source: None)
        self.owner = await initialize_session_authority(
            runner, profile_id=str(self.target_home), instance_id="fixture-native-instance", db=db)
        identity = {
            "provider":"local", "user_id":"fixture-human",
            "profile_id":str(self.target_home), "instance_id":self.owner.instance_id,
            "capabilities":["session:read", "session:approve", "session:respond", "session:control",
                "session:create", "session:submit"],
        }

        class Sink:
            def write(self, _frame):
                return True
            def close(self):
                return None

        self.desktop = AuthorityConnection(self.owner, Sink(), dict(identity))
        params = {"request_id":"canonical-fixture-session-create", "source":"gui",
                  "cwd":str(self.target_home), "model":"fixture-model", "toolsets":[]}
        policy = build_policy(params, {"platform_toolsets":{"gui":[]}}, private_secrets={})
        self.ref = create_local_session(self.owner, self.desktop.actor, params, trusted_policy=policy)
        await self.desktop.dispatch({"jsonrpc":"2.0", "id":1, "method":"session.resume",
                                     "params":{"session_id":self.ref.session_id}})
        admission = admit_session_input(self.owner.db, epoch=self.owner.epoch,
            principal_id=self.desktop.actor.subject, session_id=self.ref.session_id,
            request_id="canonical-fixture-running-turn", payload={"text":"synthetic already-running turn"})
        turn = claim_session_input(self.owner.db, epoch=self.owner.epoch,
                                   session_id=self.ref.session_id)
        self.generation = turn["generation"]
        self.live = self.owner.sessions[self.ref.session_id]
        self.desktop_subscribers = dict(self.live.subscribers)

        from tools.approval_gateway_wait import _await_gateway_decision
        approval_data = {
            "request_id":self.prompt_id,
            "command":"synthetic operation (not executed)",
            "description":"synthetic full native review",
            "allow_session":False,
            "allow_permanent":False,
            "edit":{"path":"fixture.txt", "before":"old", "after":"new",
                    "nested":{"preserve":True}},
        }
        def notify(data):
            try:
                self.owner.register_approval(self.ref.session_id, self.generation,
                    self.live.route, data, timeout_s=90)
                self.waiter_ready.set()
            except BaseException as exc:
                self.notify_error = exc
                self.waiter_ready.set()
                raise
        def wait_for_answer():
            try:
                self.waiter_result = _await_gateway_decision(self.live.route, notify, approval_data)
            except BaseException as exc:
                self.waiter_error = exc
        self.waiter_thread = threading.Thread(target=wait_for_answer, name="fixture-native-approval-waiter", daemon=True)
        self.waiter_thread.start()
        if not await asyncio.to_thread(self.waiter_ready.wait, 12):
            raise RuntimeError(f"native approval waiter did not publish: {self.waiter_error!r}")
        if self.notify_error:
            raise RuntimeError("native notify callback failed") from self.notify_error
        self.desktop_snapshot = await self.desktop.dispatch({"jsonrpc":"2.0", "id":2,
            "method":"session.resume", "params":{"session_id":self.ref.session_id}})
        if "result" not in self.desktop_snapshot:
            raise RuntimeError("Desktop fixture could not capture native prompt")

        def make_connection():
            return AuthorityConnection(self.owner, Sink(), dict(identity))

        async def peer(websocket):
            connection = make_connection()
            try:
                async for raw in websocket:
                    frame = json.loads(raw)
                    if frame.get("method") == "approval.respond":
                        self.rpc_frames.append(frame)
                    answer = await connection.dispatch(frame)
                    if answer is not None:
                        await websocket.send(json.dumps(answer, ensure_ascii=False))
            finally:
                await connection.close()

        async with serve(peer, "127.0.0.1", 0, subprotocols=["hermes-gateway-v1"], max_size=8 * 1024 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            self.ws_url = f"ws://127.0.0.1:{port}"
            self.started.set()
            await asyncio.to_thread(self.stop.wait)
        await self.desktop.close()

    def prompt(self):
        if not self.desktop_snapshot or "result" not in self.desktop_snapshot:
            raise AssertionError("missing real Desktop snapshot")
        prompts = self.desktop_snapshot["result"]["prompts"]
        matches = [prompt for prompt in prompts if prompt.get("kind") == "approval"]
        if len(matches) != 1:
            raise AssertionError(f"expected one actual canonical prompt, got {matches!r}")
        return matches[0]

    def stop_and_join(self):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.waiter_thread and self.waiter_thread.is_alive():
            self.waiter_thread.join(timeout=3)


class CanonicalCoreQualificationTests(unittest.TestCase):
    def test_documented_core_generation_has_no_current_client_namespace_fallback(self):
        root = _core_root()
        generation = _core_generation(root)
        declared = os.environ.get("HERMES_CORE_GENERATION")
        if declared:
            self.assertEqual(generation, "legacy" if declared == "installed" else declared)
        if generation == "legacy":
            self.assertIsNone(source_backend.load_compatibility())
            self.assertFalse((root / "hermes_cli" / "gateway_client.py").exists())
            return
        self.assertEqual(generation, "current")
        api = source_backend.load_compatibility()
        self.assertIsNotNone(api)
        self.assertEqual(api.protocol, "hermes-gateway-v1")
        for name in ("hermes_cli.gateway_client", "hermes_cli.gateway_runtime",
                     "gateway.session_controls", "gateway.session_authority",
                     "gateway.session_pending_controls", "tools.approval_gateway_wait"):
            _module_in_core(importlib.import_module(name), root)
        self.assertTrue((root / "gateway" / "session_authority.py").is_file())

    def test_current_core_real_authority_ptb_callback_and_registered_fixed_cli_apply_once(self):
        root = _core_root()
        if _core_generation(root) != "current":
            self.skipTest("the pinned noncanonical host has no canonical GatewayClient/runtime API; absence is asserted above")
        api = source_backend.load_compatibility()
        self.assertIsNotNone(api)
        self.assertEqual(os.environ.get("HERMES_CORE_REF", CURRENT_REF), CURRENT_REF)
        self.assertEqual(os.environ.get("HERMES_CORE_GENERATION", "current"), "current")

        temp = test_support.temporary_directory(prefix="canon-cli-")
        self.addCleanup(temp.cleanup)
        scratch = Path(temp.name)
        home = scratch / "h"
        target_home = home / "profiles" / "claire"
        package_home = home / "plugins" / PLUGIN_ID
        target_home.mkdir(parents=True)
        package_home.parent.mkdir(parents=True)
        source_package = Path(__file__).resolve().parent
        shutil.copytree(source_package, package_home,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test_*.py", "*.test.mjs", "*.md"))
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        service_enabled: true\n        attention_notifications_enabled: true\n        approvals_enabled: true\n        completion_notifications_enabled: true\n", encoding="utf-8")
        (target_home / "config.yaml").write_text("approvals:\n  timeout: 90\n", encoding="utf-8")
        env = patch.dict(os.environ, {"HERMES_HOME":str(home), "LOCALAPPDATA":str(scratch / "local"),
            "HERMES_DISABLE_LAZY_INSTALLS":"1"})
        env.start(); self.addCleanup(env.stop)
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        scope = set_secret_scope({"TELEGRAM_BOT_TOKEN":TOKEN, "TELEGRAM_HOME_CHANNEL":str(CHAT)},
                                 profile_home=str(home))
        self.addCleanup(reset_secret_scope, scope)

        module_name = "_canonical_registered_cli_fixture"
        spec = importlib.util.spec_from_file_location(module_name, package_home / "__init__.py",
            submodule_search_locations=[str(package_home)])
        self.assertIsNotNone(spec); self.assertIsNotNone(spec.loader)
        package = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = package
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in tuple(sys.modules)
            if name == module_name or name.startswith(module_name + ".")])
        spec.loader.exec_module(package)

        class Context:
            plugin_id = PLUGIN_ID
            def __init__(self):
                self.commands = []
                self.factories = []
            def register_cli_command(self, **kwargs):
                self.commands.append(kwargs)
            def register_telegram_handler(self, factory):
                self.factories.append(factory)
            def get_config(self, name, default=None):
                return {"service_enabled":True, "attention_notifications_enabled":True,
                        "approvals_enabled":True, "completion_notifications_enabled":True}.get(name, default)
        ctx = Context()
        package.register(ctx)
        self.assertEqual([entry["name"] for entry in ctx.commands], ["desktop-telegram-bridge"])
        self.assertEqual(len(ctx.factories), 1)
        registration = ctx.commands[0]
        parser = argparse.ArgumentParser()
        registration["setup_fn"](parser)

        package_notifier = importlib.import_module(f"{module_name}.notifier")
        package_approval = importlib.import_module(f"{module_name}.approval_bridge")
        package_backend = importlib.import_module(f"{module_name}.newcanonical_backend")
        state = home / "plugin-data" / PLUGIN_ID
        self.assertTrue(package_approval.bind_receiver(BOT, CHAT, None, "boot-canonical-fixture",
            private_verified=True, state=state))

        telegram_calls = []
        next_message_id = [3100]
        def telegram_api(_token, method, payload):
            telegram_calls.append((method, json.loads(json.dumps(payload, ensure_ascii=False))))
            if method == "sendMessage":
                next_message_id[0] += 1
                return {"ok":True,"result":{"message_id":next_message_id[0],
                    "chat":{"id":payload["chat_id"],"type":"private"},
                    "text":payload["text"],"message_thread_id":payload.get("message_thread_id"),
                    "from":{"id":BOT,"is_bot":True},"reply_markup":payload.get("reply_markup")}}
            if method == "editMessageText":
                return {"ok":True,"result":{"message_id":payload["message_id"],"chat":{"id":CHAT}}}
            return {"ok":False,"error_code":400,"description":"not modified"}

        owner = NativeGatewayFixture(target_home, home).start()
        self.addCleanup(owner.stop_and_join)
        self.assertFalse(owner.thread_error, repr(owner.thread_error))
        prompt = owner.prompt()
        self.assertEqual(prompt["prompt_id"], owner.prompt_id)
        self.assertEqual(prompt["execution_generation"], owner.generation)
        replay_epoch = owner.desktop_snapshot["result"]["replay_epoch"]
        replay = asyncio.run(owner.desktop.dispatch({"jsonrpc":"2.0", "id":900,
            "method":"session.events.since", "params":{"session_id":owner.ref.session_id,
                "last_seen":0, "replay_epoch":replay_epoch}}))
        self.assertIn("result", replay)
        event_frames = replay["result"].get("events")
        self.assertIsInstance(event_frames, list)
        actual_event = next((frame for frame in event_frames
            if isinstance(frame, dict) and frame.get("type") == "approval.request" and
            frame.get("payload", {}).get("prompt_id") == prompt["prompt_id"]), None)
        self.assertIsNotNone(actual_event, f"must source the event from actual AuthorityConnection replay: {replay.get('result')!r}")
        self.assertEqual(actual_event["payload"], prompt)
        route = {"mode":"local", "connectionId":"desktop-connection-fixture",
                 "profile":"claire", "targetProfile":"claire"}
        endpoint = SimpleNamespace(profile_id=str(target_home), instance_id="fixture-native-instance",
            authority_epoch=owner.owner.epoch, runtime_protocol=1,
            capabilities=frozenset({"session-authority-v1"}), control_home=str(home))
        discovery = SimpleNamespace(state="ready", endpoint=endpoint, reason_code=None)
        def discover(_home, *, timeout=2.0):
            self.assertGreater(timeout, 0)
            return discovery
        @contextlib.asynccontextmanager
        async def connect_fixture(captured_endpoint):
            from websockets.asyncio.client import connect
            self.assertIs(captured_endpoint, endpoint, "only the explicitly captured ready endpoint may connect")
            async with connect(owner.ws_url, subprotocols=[api.protocol], open_timeout=5,
                               max_size=8 * 1024 * 1024, proxy=None) as websocket:
                async with api.client_type(websocket) as client:
                    yield client
        fixture_api = type(api)(api.protocol, api.client_type, api.rpc_error_type,
                                connect_fixture, discover)

        def invoke(request):
            encoded = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")
            args = parser.parse_args(["--request", encoded])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                registration["handler_fn"](args)
            self.assertLessEqual(len(output.getvalue()), 48_000)
            return _frame_result(output.getvalue())

        node_env_keys=("PATH","SystemRoot","WINDIR","PATHEXT","COMSPEC","HOME","USERPROFILE",
            "HERMES_HOME","LOCALAPPDATA","APPDATA","XDG_CONFIG_HOME","XDG_DATA_HOME","XDG_CACHE_HOME",
            "HERMES_TEST_TMPDIR","TMPDIR","TMP","TEMP","RUNNER_TEMP",
            "HERMES_TEST_ISOLATION_ROOT","HERMES_TEST_ISOLATION_ARTIFACT_ROOT",
            "HERMES_TEST_ISOLATION_META_ROOT","HERMES_TEST_ISOLATION_OUTSIDE_ROOT",
            "HERMES_TEST_ISOLATION_TOKEN","HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED",
            "HERMES_INSTALL_ROOT")
        node_env={key:os.environ[key] for key in node_env_keys if key in os.environ}
        node_env.update({"HERMES_HOME":str(home),"LOCALAPPDATA":str(scratch/"local"),
            "HERMES_CORE_ROOT":str(root),"HERMES_CORE_REF":CURRENT_REF,
            "HERMES_CORE_GENERATION":"current","HERMES_DISABLE_LAZY_INSTALLS":"1"})

        def joined_monitor_cli():
            sdk_event={**actual_event,"profile":route["profile"],"connectionId":route["connectionId"]}
            process=subprocess.Popen(["node","--input-type=module","-e",NODE_JOINED_ADAPTER],
                cwd=source_package,env=node_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,text=True,encoding="utf-8",bufsize=1)
            self.assertIsNotNone(process.stdin);self.assertIsNotNone(process.stdout);self.assertIsNotNone(process.stderr)
            helper_calls=[]
            response_summaries=[]
            registration_result=None
            owner_value=None
            done=None
            try:
                process.stdin.write(json.dumps({"event":sdk_event,"route":route},ensure_ascii=False)+chr(10))
                process.stdin.flush()
                for line in process.stdout:
                    frame=json.loads(line)
                    if frame.get("type")=="helper":
                        request=frame.get("request")
                        self.assertIsInstance(request,dict)
                        action=request.get("action")
                        helper_calls.append(action)
                        if action=="canonical_approval_observe":
                            self.assertEqual(inbox_counts(),(0,0),'observation must not pregrant the owner')
                        elif action=="approval_owner_heartbeat":
                            self.assertEqual(inbox_counts(),(0,0),'the actual heartbeat creates the first owner lease')
                            owner_value=request.get("owner")
                            self.assertEqual(request.get("capability"),package_approval.CAPABILITY)
                        elif action=="approval_register":
                            self.assertEqual(inbox_counts(),(1,0),'registration must follow a successful fresh owner heartbeat')
                            self.assertEqual(request.get("owner"),owner_value)
                        response=invoke(request)
                        response_summaries.append({"action":action,**{key:response.get(key) for key in
                            ("ok","reason","status","feature_ready","version","capability")}})
                        if action=="approval_register":registration_result=response
                        process.stdin.write(json.dumps({"type":"reply","value":response},ensure_ascii=False)+chr(10))
                        process.stdin.flush()
                    elif frame.get("type")=="done":
                        done=frame
                    else:
                        self.fail('joined Node monitor emitted an unexpected stdio frame')
            finally:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None and done is None:
                    process.terminate()
                    process.wait(timeout=10)
            stderr=process.stderr.read()
            returncode=process.wait(timeout=45)
            self.assertEqual(returncode,0,stderr)
            self.assertIsNotNone(done,'joined Node monitor did not return completion state')
            self.assertEqual(helper_calls,["canonical_approval_observe","approval_owner_heartbeat","approval_register"],response_summaries)
            self.assertTrue(owner_value)
            self.assertEqual(done["owner"],owner_value)
            self.assertEqual(done["prompt_id"],owner.prompt_id)
            self.assertEqual(done["generation"],owner.generation)
            self.assertTrue(done["canonical_observed_key"])
            self.assertIsInstance(registration_result,dict)
            self.assertTrue(registration_result.get("ok"),registration_result)
            self.assertEqual(done["approval_id"],registration_result["id"])
            return owner_value,registration_result,done

        def read_approval(approval_id):
            connection = package_approval.connect_readonly(state)
            if connection is None:
                return None
            try:
                found = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
                return dict(found) if found is not None else None
            finally:
                connection.close()

        def inbox_counts():
            connection = package_approval.connect_readonly(state)
            if connection is None:
                return (0,0)
            try:
                return (connection.execute("SELECT count(*) FROM owners").fetchone()[0],
                        connection.execute("SELECT count(*) FROM approvals").fetchone()[0])
            finally:
                connection.close()

        self.assertEqual(inbox_counts(),(0,0),'canonical-only startup must not preseed owner or approval rows')

        with patch.object(package_backend, "load_compatibility", return_value=fixture_api), \
             patch.object(package_notifier, "telegram", side_effect=telegram_api):
            owner_name,registered,_done=joined_monitor_cli()
            self.assertEqual(registered["status"],"open")
            self.assertEqual(inbox_counts(),(1,1))
            self.assertEqual(sum(method=="sendMessage" for method,_payload in telegram_calls),1)
            row = read_approval(registered["id"])
            self.assertEqual(row["status"], "open")
            self.assertEqual(row["owner"],owner_name)
            self.assertIn(prompt["command"], row["review_text"])
            self.assertIn('"path": "fixture.txt"', row["review_text"])
            self.assertIn('"preserve": true', row["review_text"])
            self.assertNotIn("request_id", json.loads(row["descriptor"]))

            from telegram import Update
            from telegram.ext import Application, TypeHandler
            async def click_real_ptb_callback():
                app = Application.builder().token(TOKEN).build()
                with patch.object(type(app.bot), "initialize", new=AsyncMock()), \
                     patch.object(type(app.bot), "shutdown", new=AsyncMock()):
                    await app.initialize()
                    self.assertTrue(package_approval.wire(app, package_notifier, BOT, CHAT, None,
                        "boot-canonical-fixture", approvals_enabled=lambda:True, state=state))
                    downstream = []
                    async def tail(update, context): downstream.append(update)
                    app.add_handler(TypeHandler(Update, tail), group=1)
                    callback_data = telegram_calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                    update = Update.de_json({"update_id":901,"callback_query":{"id":"canonical-click",
                        "from":{"id":CHAT,"is_bot":False,"first_name":"fixture-owner"},
                        "chat_instance":"canonical-fixture","data":callback_data,
                        "message":{"message_id":row["message_id"],"date":1,
                            "chat":{"id":CHAT,"type":"private"},
                            "from":{"id":BOT,"is_bot":True,"first_name":"fixture-bot"},
                            "text":row["review_text"]}}}, app.bot)
                    ack = AsyncMock()
                    with patch.object(type(update.callback_query), "answer", new=ack):
                        await app.process_update(update)
                    self.assertGreater(ack.await_count, 0)
                    self.assertEqual(downstream, [], "real PTB handler must consume the callback")
                    await app.shutdown()
            asyncio.run(click_real_ptb_callback())
            queued = read_approval(registered["id"])
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(queued["choice"], "once")

            fresh_observation = invoke({"action":"approval_native_observe",
                "capability":package_approval.CAPABILITY, "owner":owner_name, "id":registered["id"]})
            self.assertTrue(fresh_observation.get("ok"), fresh_observation)
            polled = invoke({"action":"approval_poll", "capability":package_approval.CAPABILITY,
                "owner":owner_name, "observations":[fresh_observation], "unverified":[], "absent":[]})
            self.assertTrue(polled.get("ok"), polled)
            self.assertEqual(len(polled["items"]), 1)
            claim = polled["items"][0]
            self.assertEqual(claim["descriptor"]["dispatch_mode"], "canonical_native")
            applied = invoke({"action":"approval_native_apply", "capability":package_approval.CAPABILITY,
                "owner":owner_name, "id":registered["id"], "claim":claim["claim"]})
            self.assertEqual(applied, {"ok":True,"status":"applied","reason":None,
                                       "receipt":"decision_resolved"})

        self.assertEqual(len(owner.rpc_frames), 1)
        wire_params = owner.rpc_frames[0]["params"]
        self.assertEqual(wire_params, {"session_id":owner.ref.session_id,
            "execution_generation":owner.generation, "prompt_id":owner.prompt_id, "choice":"once"})
        self.assertEqual(set(wire_params), {"session_id","execution_generation","prompt_id","choice"})
        self.assertTrue(owner.waiter_result["resolved"])
        self.assertEqual(owner.waiter_result["choice"], "once")
        self.assertIsNone(owner.waiter_error)
        self.assertTrue(all(owner.live.subscribers.get(key) == value
                            for key, value in owner.desktop_subscribers.items()),
                        "helper detach must preserve the original Desktop subscription")
        self.assertNotIn("messages", applied)
        self.assertNotIn("history", applied)
        self.assertEqual(read_approval(registered["id"])["status"], "applied")

    def test_current_real_producer_families_project_canonical_prompts_and_once_deny_settle_exact_waiters(self):
        root = _core_root()
        if _core_generation(root) != "current":
            self.skipTest("the pinned noncanonical host has no canonical session authority; native legacy producer tests remain separate")
        from contextlib import ExitStack
        from tools import approval as native_approval, approval_context
        from tools.file_tools_write_guards import _request_protected_instruction_approval, _check_approval_required_write
        from tools.computer_use import tool as computer_use
        from tools.approval_prompt import request_elicitation_consent

        temp = test_support.temporary_directory(prefix="canon-producers-")
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "h"
        target_home = home / "profiles" / "claire"
        target_home.mkdir(parents=True)
        # The real guard protects the OS user's SSH home, not every arbitrary
        # profile directory named .ssh. HOME/USERPROFILE below bind this fake home.
        (home / ".ssh").mkdir()
        (home / ".ssh" / "config").write_text("# synthetic SSH config", encoding="utf-8")
        owner = NativeGatewayFixture(target_home, home).start()
        self.addCleanup(owner.stop_and_join)
        session_key = owner.live.route
        def ssh_config_producer():
            return _check_approval_required_write([str(home / ".ssh" / "config")], task_id="default")
        producers = [
            ("shell-danger", lambda:native_approval.check_dangerous_command("rm -rf /tmp/approval-fixture", "local"), False, False),
            ("python-cli-shell", lambda:native_approval.check_dangerous_command('python -c "print(1)"', "local"), False, False),
            ("execute-code", lambda:native_approval.check_execute_code_guard('print("complete Python body")', "local"), False, False),
            ("protected-instruction", lambda:_request_protected_instruction_approval(["AGENTS.md"]), False, False),
            ("ssh-config", ssh_config_producer, False, False),
            ("plugin-rule", lambda:native_approval.request_tool_approval("fixture_tool", "Synthetic plugin approval", rule_key="fixture-rule"), False, False),
            ("computer-use", lambda:computer_use._request_approval("click", {"delivery_mode":"foreground", "x":41, "y":23}), False, False),
            ("smart-denied", lambda:native_approval.check_all_command_guards("rm -rf /tmp/approval-fixture", "local"), True, False),
            ("mcp-elicitation", lambda:request_elicitation_consent("Synthetic MCP action", "Synthetic consent summary"), False, True),
        ]
        editor_module = importlib.import_module("gateway.session_local_editor")
        from acp_adapter.edit_approval import EditProposal
        producers.append(("editor-edit", lambda:editor_module.request_editor_edit(
            EditProposal("edit_file", "src/example.py", "old line\n", "new line\n",
                         {"diff":"-old line\n+new line"})), False, False))
        settled = 0
        try:
            for label, producer, smart_denied, mcp in producers:
                for choice in ("once", "deny"):
                    notified = threading.Event()
                    captured = {}
                    worker_error = []
                    outcome = []
                    def notify(data):
                        try:
                            captured["data"] = json.loads(json.dumps(data, ensure_ascii=False))
                            entries = native_approval._gateway_queues.get(session_key, [])
                            captured["entry"] = next((entry for entry in entries
                                if entry.data.get("request_id") == data.get("request_id")), None)
                            owner.owner.register_approval(owner.ref.session_id, owner.generation,
                                session_key, data, timeout_s=90)
                        except BaseException as exc:
                            worker_error.append(exc)
                        finally:
                            notified.set()
                    def invoke():
                        try:
                            outcome.append(producer())
                        except BaseException as exc:
                            worker_error.append(exc)
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(os.environ, {"USERPROFILE":str(home), "HOME":str(home)}))
                        stack.enter_context(patch.object(native_approval, "_presence", return_value=(None,False,True,False)))
                        stack.enter_context(patch.object(native_approval, "get_current_session_key", return_value=session_key))
                        stack.enter_context(patch.object(approval_context, "get_current_session_key", return_value=session_key))
                        stack.enter_context(patch.object(native_approval, "_unattended_contexts", return_value=[]))
                        stack.enter_context(patch.object(approval_context, "_get_approval_mode",
                            return_value="smart" if smart_denied else "on"))
                        if smart_denied:
                            stack.enter_context(patch.object(native_approval, "_smart_gate", return_value=(None,True)))
                            if hasattr(native_approval, "_tirith_scan"):
                                stack.enter_context(patch.object(native_approval, "_tirith_scan", return_value={
                                    "action":"allow", "findings":[], "summary":""}))
                        if mcp:
                            ctx = importlib.import_module("tools.approval_prompt")._ctx
                            stack.enter_context(patch.object(ctx, "_is_gateway_approval_context", return_value=True))
                            stack.enter_context(patch.object(ctx, "_is_single_query_approval_context", return_value=False))
                            stack.enter_context(patch.object(ctx, "_is_cron_approval_context", return_value=False))
                        stack.enter_context(patch.dict(native_approval._gateway_notify_cbs, {session_key:notify}))
                        worker = threading.Thread(target=invoke, name=f"canonical-producer-{label}-{choice}", daemon=True)
                        worker.start()
                        self.assertTrue(notified.wait(10), f"{label} did not publish canonical prompt: {worker_error!r}")
                        self.assertFalse(worker_error, repr(worker_error))
                        data = captured.get("data")
                        self.assertIsInstance(data, dict)
                        self.assertIsNotNone(captured.get("entry"), f"{label} native waiter entry missing")
                        async def capture_snapshot():
                            return await owner.desktop.dispatch({"jsonrpc":"2.0", "id":1200 + settled,
                                "method":"session.resume", "params":{"session_id":owner.ref.session_id}})
                        snapshot = asyncio.run(capture_snapshot())
                        self.assertIn("result", snapshot, f"{label}: {snapshot}")
                        prompts = snapshot["result"].get("prompts", [])
                        prompt_id = data.get("request_id")
                        matches = [item for item in prompts if item.get("kind") == "approval" and
                                   item.get("prompt_id") == prompt_id]
                        self.assertEqual(len(matches), 1, f"{label}: canonical prompt was not projected: {prompts!r}")
                        prompt = matches[0]
                        self.assertIn("once", prompt["choices"], label)
                        self.assertIn("deny", prompt["choices"], label)
                        if label == "execute-code":
                            self.assertIn("complete Python body", prompt["command"])
                        if label == "editor-edit":
                            self.assertEqual(prompt["edit"]["arguments"]["diff"], "-old line\n+new line")
                        response = asyncio.run(owner.desktop.dispatch({"jsonrpc":"2.0", "id":1300 + settled,
                            "method":"approval.respond", "params":{"session_id":owner.ref.session_id,
                                "execution_generation":owner.generation, "prompt_id":prompt_id, "choice":choice}}))
                        self.assertEqual(response.get("result"), {"status":"resolved", "prompt_id":prompt_id},
                                         f"{label}/{choice}: {response}")
                        worker.join(10)
                        self.assertFalse(worker.is_alive(), f"{label}/{choice} waiter did not settle")
                        self.assertFalse(worker_error, f"{label}/{choice}: {worker_error!r}")
                        self.assertTrue(captured["entry"].event.is_set(), f"{label}/{choice} exact entry unresolved")
                        self.assertEqual(captured["entry"].result, choice)
                        with native_approval._lock:
                            remaining = list(native_approval._gateway_queues.get(session_key, []))
                        self.assertTrue(any(entry.data.get("request_id") == owner.prompt_id for entry in remaining),
                            f"{label}/{choice} consumed the unrelated waiter")
                        settled += 1
        finally:
            native_approval.unregister_gateway_notify(session_key)
        self.assertEqual(settled, 20)


if __name__ == "__main__":
    unittest.main()
