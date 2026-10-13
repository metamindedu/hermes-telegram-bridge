"""M2/M3 regression boundaries through the registered native CLI and renderer monitor.

Only Telegram transport is mocked. The renderer fixture imports the real Desktop
monitor with stubbed SDK/JSX modules; it does not claim to render React.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

import test_support
from hermes_state import SessionDB

SOURCE = Path(__file__).resolve().parent
PLUGIN_ID = "session-complete-telegram"
COMMAND_ID = "desktop-telegram-bridge"
FRAME_BEGIN = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN"
FRAME_END = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_END"


def _frame_result(output: str) -> dict:
    lines = output.splitlines()
    if len(lines) != 3 or lines[0] != FRAME_BEGIN or lines[2] != FRAME_END:
        raise AssertionError(f"expected one native result frame, got {output!r}")
    raw = base64.urlsafe_b64decode(lines[1] + "=" * ((4 - len(lines[1]) % 4) % 4))
    return json.loads(raw.decode("utf-8"))


class NotificationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix="nb-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "h"
        self.home.mkdir()
        self.local_app_data = self.root / "l"
        self.local_app_data.mkdir()
        self.config = self.home / "config.yaml"
        self.write_policy()

        env = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home),
            "LOCALAPPDATA": str(self.local_app_data),
        })
        env.start()
        self.addCleanup(env.stop)

        self.package = self.home / "plugins" / PLUGIN_ID
        self.package.parent.mkdir(parents=True)
        shutil.copytree(SOURCE, self.package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.module_name = "review_notification_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(
            self.module_name, self.package / "__init__.py",
            submodule_search_locations=[str(self.package)])
        if spec is None or spec.loader is None:
            raise AssertionError("native package import spec unavailable")
        package_module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = package_module
        spec.loader.exec_module(package_module)
        self.addCleanup(self._unload_package)

        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID, source="user",
                                  path=str(self.package))
        self.ctx = PluginContext(manifest, manager)
        package_module.register(self.ctx)
        registration = manager._cli_commands[COMMAND_ID]
        parser = argparse.ArgumentParser()
        registration["setup_fn"](parser)
        self.native_handler = registration["handler_fn"]
        self.native_parser = parser
        self.notifier = importlib.import_module(self.module_name + ".notifier")
        self.approval = importlib.import_module(self.module_name + ".approval_bridge")
        self.state = self.home / "plugin-data" / PLUGIN_ID
        self.network = []
        self.next_message_id = 900

    def _unload_package(self):
        for name in tuple(sys.modules):
            if name == self.module_name or name.startswith(self.module_name + "."):
                sys.modules.pop(name, None)

    def write_policy(self, *, service=True, completion=True, attention=True,
                     approvals=False, invalid=False):
        values = {
            "service_enabled": service,
            "completion_notifications_enabled": completion,
            "attention_notifications_enabled": attention,
            "clarify_replies_enabled": True,
            "completion_followups_enabled": True,
            "replies_enabled": False,
            "approvals_enabled": approvals,
        }
        entries = "\n".join(f"        {key}: {'not-a-bool' if invalid and key == 'attention_notifications_enabled' else str(value).lower()}"
                             for key, value in values.items())
        self.config.write_text(
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n"
            "    session-complete-telegram:\n      settings:\n" + entries +
            "\napprovals:\n  timeout: 300\n",
            encoding="utf-8")

    def invoke_native(self, request):
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        args = self.native_parser.parse_args(["--request", encoded])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.native_handler(args)
        output = stdout.getvalue()
        self.assertLessEqual(len(output), 48_000)
        return _frame_result(output), output

    def telegram_stub(self, _token, method, payload):
        self.network.append((method, json.loads(json.dumps(payload))))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {
                "message_id": self.next_message_id,
                "chat": {"id": payload["chat_id"], "type": "private"},
                "text": payload["text"],
                "message_thread_id": payload.get("message_thread_id"),
                "from": {"id": 77, "is_bot": True},
            }}
        return {"ok": False, "error_code": 400,
                "description": "Bad Request: message is not modified"}

    def dispatch(self, request):
        with (
            patch.object(self.notifier, "credentials",
                         return_value=("77:synthetic-token", 99, None)),
            patch.object(self.notifier, "telegram", side_effect=self.telegram_stub),
            patch.dict(self.notifier.deliver.__kwdefaults__, {"api": self.telegram_stub}),
            patch.dict(self.notifier.deliver_attention.__kwdefaults__,
                       {"api": self.telegram_stub}),
        ):
            return self.invoke_native(request)

    def make_final(self, sid, text="요청 작업을 마쳤습니다.", *, timestamp=None):
        home = self.home
        db = SessionDB(home / "state.db")
        db.create_session(sid, source="desktop")
        db.set_session_title(sid, f"알림 경계 fixture {sid}")
        db.append_message(sid, "user", "테스트 요청", timestamp=time.time() - 180)
        final_id = db.append_message(sid, "assistant", text,
                                    timestamp=time.time() - 120 if timestamp is None else timestamp)
        db.close()
        return {"action": "send", "profile": "default", "session_key": sid,
                "final_row_id": final_id, "desktop_observed": True}

    def approval_fixture(self):
        from tools.approval_detection import detect_dangerous_command

        command = "rm -rf notification-boundary-fixture && echo password=synthetic-secret-value-739102"
        dangerous, key, description = detect_dangerous_command(command)
        self.assertTrue(dangerous)
        request = {"id": "srq-aabbcc112233", "method": "approval", "params": {
            "session_id": "runtime-notification-boundary", "request_id": "a" * 32,
            "command": command, "description": description, "pattern_key": key,
            "pattern_keys": [key], "allow_permanent": True, "allow_session": True,
            "choices": ["once", "session", "always", "deny"],
        }}
        pending = {key: request["params"][key] for key in (
            "request_id", "command", "description", "pattern_key", "pattern_keys",
            "allow_permanent", "allow_session")}
        route = {"mode": "local", "connectionId": "native-notification-boundary",
                 "profile": "default", "targetProfile": "default"}
        session = {"id": request["params"]["session_id"],
                   "session_key": "durable-notification-boundary", "status": "waiting"}
        return command, request, pending, route, session

    def renderer_notices(self, request, pending, route, session, heartbeat, owner):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the monitor-only renderer harness")
        script = r'''
import {readFileSync} from 'node:fs';
import {registerHooks} from 'node:module';
registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk')
    return {url:'data:text/javascript,export const host={}', shortCircuit:true};
  // Monitor only: this harness must never claim to render real React.
  if (specifier === 'react/jsx-runtime')
    return {url:'data:text/javascript,export const jsx=()=>{throw Error("unexpected UI render")}', shortCircuit:true};
  return nextResolve(specifier, context);
}});
const x=JSON.parse(readFileSync(0,'utf8'));
const {CompletionMonitor}=await import(x.entry);
const helperCalls=[],profileCalls=[],notices=[];
const snapshot={epoch:'native-notification-epoch',open_requests:[x.request]};
const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[x.route],
  notify:n=>notices.push(n),requestProfile:async(_route,method,args)=>{
    profileCalls.push({method,args});
    if(method==='session.events.since')return snapshot;
    if(method==='approval.pending')return {approvals:[x.pending]};
    throw Error('unexpected RPC '+method);
  }};
const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});
monitor.approvalCapable=true;monitor.replyOwner=x.owner;
monitor.helper=async request=>{
  helperCalls.push(request);
  if(request.action==='approval_owner_heartbeat')return x.heartbeat;
  if(request.action==='approval_notice')return {ok:true,status:'sent',notification_only:true};
  if(request.action==='approval_register')throw Error('approval register must remain gated');
  if(request.action==='attention')throw Error('supported approval must not use generic attention');
  throw Error('unexpected helper '+request.action);
};
const record={sid:x.session.id,ask:null,approval:null,askChecking:false};
await monitor.checkAsk(record,x.route,x.session,snapshot,x.request);
// A repeated observation may cross the helper boundary again; the native
// sender's durable dedupe claim must prevent a second Telegram message.
await monitor.checkAsk(record,x.route,x.session,snapshot,x.request);
console.log(JSON.stringify({helperCalls,profileCalls,notices,recordAsk:record.ask,
  approval:record.approval}));
'''
        payload = {
            "entry": (self.package / "desktop" / "plugin.js").as_uri(),
            "owner": owner, "route": route, "session": session,
            "request": request, "pending": pending, "heartbeat": heartbeat,
        }
        child_env = dict(os.environ)
        child_env["HERMES_HOME"] = str(self.home)
        child_env["LOCALAPPDATA"] = str(self.local_app_data)
        result = subprocess.run([node, "--input-type=module", "-e", script],
                                input=json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8", capture_output=True, timeout=30,
                                env=child_env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_renderer_notice_uses_attention_not_approval_consent_and_dedupes(self):
        self.write_policy(completion=False, attention=True, approvals=False)
        command, request, pending, route, session = self.approval_fixture()
        owner = "native-notification-owner"
        heartbeat, _ = self.invoke_native({"action": "approval_owner_heartbeat",
            "capability": self.approval.CAPABILITY, "owner": owner})
        self.assertFalse(heartbeat["feature_ready"])
        rendered = self.renderer_notices(request, pending, route, session, heartbeat, owner)
        actions = [item["action"] for item in rendered["helperCalls"]]
        self.assertEqual(actions, ["approval_owner_heartbeat", "approval_notice"] * 2)
        self.assertEqual(rendered["recordAsk"]["id"], request["id"])
        self.assertIsNone(rendered["approval"])
        self.assertNotIn("attention", actions)
        self.assertNotIn("approval_register", actions)
        self.assertNotIn("approval.respond", [item["method"] for item in rendered["profileCalls"]])

        notice_requests = [item for item in rendered["helperCalls"]
                           if item["action"] == "approval_notice"]
        self.assertEqual(len(notice_requests), 2)
        with self.assertRaisesRegex(ValueError, "redaction_omission"):
            self.approval.review_card(request, 300)
        first, first_frame = self.dispatch(notice_requests[0])
        second, _ = self.dispatch(notice_requests[1])
        self.assertTrue(first["ok"] and first["notification_only"], first)
        self.assertTrue(second["duplicate"], second)
        self.assertNotIn(command, first_frame)
        self.assertEqual([method for method, _ in self.network].count("sendMessage"), 1)
        sent = next(payload for method, payload in self.network if method == "sendMessage")
        self.assertNotIn(command, sent["text"])
        self.assertNotIn("synthetic-secret-value-739102", sent["text"])
        self.assertNotIn("reply_markup", sent)
        self.assertIn("Desktop에서", sent["text"])
        self.assertIs(sent["protect_content"], False)
        approval_db = self.state / "approval-inbox" / "approvals.sqlite3"
        self.assertFalse(approval_db.exists(), "notification-only path must not create approval rows")

    def test_attention_off_blocks_notice_even_when_approval_consent_is_on(self):
        self.write_policy(completion=True, attention=False, approvals=True)
        _command, request, pending, route, session = self.approval_fixture()
        owner = "native-notification-owner-off"
        heartbeat, _ = self.invoke_native({"action": "approval_owner_heartbeat",
            "capability": self.approval.CAPABILITY, "owner": owner})
        self.assertFalse(heartbeat["feature_ready"])
        rendered = self.renderer_notices(request, pending, route, session, heartbeat, owner)
        notice = next(item for item in rendered["helperCalls"]
                      if item["action"] == "approval_notice")
        result, _ = self.dispatch(notice)
        self.assertFalse(result["ok"])
        self.assertFalse(self.network)
        self.assertFalse((self.state / "notifications.sqlite3").exists())
        self.assertFalse((self.state / "approval-inbox" / "approvals.sqlite3").exists())

    def test_persisted_final_notification_classes_are_independently_gated(self):
        cases = [
            (False, True, "계속 진행할까요?", "question"),
            (False, True, "요청 작업을 마쳤습니다.", None),
            (True, False, "계속 진행할까요?", None),
            (True, False, "요청 작업을 마쳤습니다.", "completion"),
            (True, True, "계속 진행할까요?", "question"),
            (True, True, "요청 작업을 마쳤습니다.", "completion"),
        ]
        for index, (completion, attention, text, expected_kind) in enumerate(cases):
            with self.subTest(completion=completion, attention=attention, text=text):
                self.write_policy(completion=completion, attention=attention)
                request = self.make_final(f"final-class-{index}", text)
                before = len([m for m, _ in self.network if m == "sendMessage"])
                result, _ = self.dispatch(request)
                after = len([m for m, _ in self.network if m == "sendMessage"])
                if expected_kind is None:
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(after, before)
                elif expected_kind == "question":
                    self.assertTrue(result["ok"] and result["verified"], result)
                    self.assertEqual(result["kind"], "question")
                    self.assertEqual(after, before + 1)
                    sent = [p for m, p in self.network if m == "sendMessage"][-1]
                    self.assertIn("질문이 있습니다", sent["text"])
                    self.assertIn(text, sent["text"])
                    self.assertNotIn("세션 작업 종료", sent["text"])
                else:
                    self.assertTrue(result["ok"] and result["verified"], result)
                    self.assertNotIn("kind", result)
                    self.assertEqual(after, before + 1)
                    sent = [p for m, p in self.network if m == "sendMessage"][-1]
                    self.assertIn("세션 작업 종료", sent["text"])

    def test_disabled_service_or_invalid_policy_blocks_before_state_read(self):
        cases = [
            {"completion": False, "attention": False, "text": "계속 진행할까요?"},
            {"completion": False, "attention": False, "text": "일반 작업 완료입니다."},
            {"completion": True, "attention": True, "service": False,
             "text": "계속 진행할까요?"},
            {"completion": True, "attention": True, "invalid": True,
             "text": "계속 진행할까요?"},
        ]
        for index, case in enumerate(cases):
            with self.subTest(policy=case):
                options = {key: value for key, value in case.items()
                           if key in {"completion", "attention", "service", "invalid"}}
                self.write_policy(**options)
                request = self.make_final(f"blocked-before-read-{index}", case["text"])
                with patch.object(self.notifier, "inspect_session",
                                  wraps=self.notifier.inspect_session) as inspect:
                    result, _ = self.invoke_native(request)
                self.assertFalse(result["ok"], result)
                inspect.assert_not_called()
                self.assertFalse(self.network)

    def test_question_notice_keeps_lease_latest_tip_quiet_period_and_durable_dedupe(self):
        self.write_policy(completion=False, attention=True)

        lease_req = self.make_final("question-lease", "계속 진행할까요?")
        lease_db = sqlite3.connect(self.home / "state.db")
        try:
            lease_db.execute("INSERT INTO session_turn_leases VALUES(?,?,?,?)",
                             (lease_req["session_key"], "fixture-holder", time.time(), time.time() + 90))
            lease_db.commit()
        finally:
            lease_db.close()
        blocked, _ = self.dispatch(lease_req)
        self.assertEqual(blocked["reason"], "active_turn_lease")

        latest_req = self.make_final("question-latest", "계속 진행할까요?")
        db = SessionDB(self.home / "state.db")
        db.append_message(latest_req["session_key"], "user", "새 요청", timestamp=time.time() - 80)
        db.close()
        blocked, _ = self.dispatch(latest_req)
        self.assertEqual(blocked["reason"], "newer_or_unfinished_work")

        quiet_req = self.make_final("question-quiet", "계속 진행할까요?", timestamp=time.time() - 5)
        blocked, _ = self.dispatch(quiet_req)
        self.assertEqual(blocked["reason"], "quiet_period")

        dedupe_req = self.make_final("question-dedupe", "계속 진행할까요?")
        first, _ = self.dispatch(dedupe_req)
        second, _ = self.dispatch(dedupe_req)
        self.assertTrue(first["ok"] and first["kind"] == "question", first)
        self.assertTrue(second["duplicate"], second)
        self.assertEqual([method for method, _ in self.network].count("sendMessage"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
