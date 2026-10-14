"""Native-CLI and real-Hermes-schema tests for completion recovery."""
from __future__ import annotations
import argparse, base64, contextlib, importlib, importlib.util, io, json, os, shutil, sqlite3, sys, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_support
from hermes_state import SessionDB

REPO=Path(__file__).resolve().parents[2]
PACKAGE=REPO/"desktop-plugins"/"session-complete-telegram"
PLUGIN_ID="session-complete-telegram"
TOKEN="123456:synthetic-token-never-used"
CHAT=-100123456
TOPIC=7
ROUTE={"mode":"local","connectionId":"local","profile":"default","targetProfile":"default"}
BEGIN="HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN"
END="HERMES_TELEGRAM_BRIDGE_RESULT_V1_END"

class CompletionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=test_support.temporary_directory(prefix="completion-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.home=self.base/"default-home";self.home.mkdir()
        self.state=self.home/"plugin-data"/PLUGIN_ID
        self.installed=self.home/"plugins"/PLUGIN_ID;self.installed.parent.mkdir()
        shutil.copytree(PACKAGE,self.installed,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        (self.home/".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_HOME_CHANNEL={CHAT}:{TOPIC}\n",encoding="utf-8")
        (self.home/"config.yaml").write_text("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings: {}\n",encoding="utf-8")
        self.home_patch=patch.dict(os.environ,{"HERMES_HOME":str(self.home)});self.home_patch.start();self.addCleanup(self.home_patch.stop)
        self.profile_patch=patch("hermes_cli.profiles.get_active_profile_name",return_value="default");self.profile_patch.start();self.addCleanup(self.profile_patch.stop)
        self.name="_completion_recovery_native_fixture"
        spec=importlib.util.spec_from_file_location(self.name,self.installed/"__init__.py",submodule_search_locations=[str(self.installed)])
        self.package=importlib.util.module_from_spec(spec);sys.modules[self.name]=self.package;spec.loader.exec_module(self.package)
        self.addCleanup(self.remove_modules)
        self.notifier=importlib.import_module(f"{self.name}.notifier")
        self.recovery=importlib.import_module(f"{self.name}.completion_recovery")
        from hermes_cli.plugins import PluginContext,PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        self.manager=PluginManager(scope_key=str(self.home))
        manifest=PluginManifest(name=PLUGIN_ID,key=PLUGIN_ID,source="user",path=str(self.installed))
        self.context=PluginContext(manifest,self.manager);self.package.register(self.context)
        registration=self.manager._cli_commands["desktop-telegram-bridge"]
        self.parser=argparse.ArgumentParser();registration["setup_fn"](self.parser);self.handler=registration["handler_fn"]
        self.sid="completion-recovery-session";self.final_id=self.make_session()
        self.policy=self.invoke({"action":"completion_recovery_policy"})
        self.assertTrue(self.policy["ok"],self.policy)

    def remove_modules(self):
        for name in tuple(sys.modules):
            if name==self.name or name.startswith(self.name+"."):sys.modules.pop(name,None)

    def make_session(self,sid=None,home=None):
        sid=self.sid if sid is None else sid;home=self.home if home is None else Path(home)
        home.mkdir(parents=True,exist_ok=True)
        db=SessionDB(home/"state.db");db.create_session(sid,source="desktop")
        db.set_session_title(sid,f"synthetic recovery test {sid}"[:80])
        db.append_message(sid,"user","Private test prompt.",timestamp=time.time()-240)
        final=db.append_message(sid,"assistant","Synthetic persisted completion.",timestamp=time.time()-120)
        db.close();return final

    def identity(self,*,created_at=None,quiet_at=None,final_id=None,runtime_id=None,session_key=None,
                 route=None,generation=None,**extra):
        created_at=time.time()-120 if created_at is None else created_at
        runtime_id=runtime_id or session_key or self.sid
        session_key=session_key or runtime_id
        if final_id is None:
            if session_key==self.sid:
                final_id=self.final_id
            else:
                final_id=self.make_session(session_key)
        value={"route":dict(route or ROUTE),"runtime_id":runtime_id,"session_key":session_key,
            "started_at":"1700000000.0","epoch":"epoch-fixture","seq":5,
            "final_row_id":final_id,"generation":generation or self.policy["generation"],
            "created_at":created_at,"quiet_at":created_at if quiet_at is None else quiet_at}
        value.update(extra);return value

    def invoke(self,request):
        raw=json.dumps(request,ensure_ascii=False,separators=(",",":")).encode()
        token=base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        args=self.parser.parse_args(["--request",token]);out=io.StringIO()
        with patch.dict(os.environ,{"HERMES_HOME":str(self.home)}),contextlib.redirect_stdout(out):self.handler(args)
        lines=out.getvalue().splitlines();self.assertGreaterEqual(len(lines),3)
        self.assertEqual(lines[0],BEGIN);self.assertEqual(lines[-1],END)
        result=json.loads(base64.urlsafe_b64decode(lines[1]+"="*(-len(lines[1])%4)))
        self.assertIs(type(result.get("ok")),bool);return result

    def record(self,identity=None):return self.invoke({"action":"completion_recovery_record","candidate":identity or self.identity()})
    def listing(self,route=None):return self.invoke({"action":"completion_recovery_list","route":route or ROUTE})
    def claim(self,item):return self.invoke({"action":"completion_recovery_claim","candidate":{"candidate_id":item["candidate_id"],"revision":item["revision"],"generation":item["generation"]},"route":item["route"]})
    def send_request(self,item,claim,final_id=None,**overrides):
        route=item["route"];sid=item["session_key"]
        value={"action":"completion_recovery_send","candidate":{"candidate_id":claim["candidate_id"],"revision":claim["revision"],"claim_token":claim["claim_token"],"generation":claim["generation"]},
            "route":route,"profile":route["targetProfile"],"session_key":sid,"final_row_id":item["final_row_id"] if final_id is None else final_id,
            "runtime_id":item["runtime_id"],"started_at":item["started_at"],
            "epoch":item["epoch"],"desktop_observed":True}
        value.update(overrides);return value
    @contextlib.contextmanager
    def db(self):
        connection=sqlite3.connect(self.state/"notifications.sqlite3")
        try:yield connection
        finally:connection.close()

    def test_registered_cli_records_metadata_only_and_duplicate_does_not_reset_watermark_or_ttl(self):
        now=time.time();first=self.record(self.identity(created_at=now-8,quiet_at=now-3))
        self.assertTrue(first["ok"],first);self.assertEqual(len(first["candidate_id"]),64)
        duplicate=self.record(self.identity(created_at=now-2,quiet_at=now-1,seq=10))
        self.assertTrue(duplicate["duplicate"]);self.assertEqual(duplicate["created_at"],first["created_at"])
        self.assertEqual(duplicate["expires_at"],first["expires_at"]);self.assertEqual(duplicate["quiet_at"],first["quiet_at"])
        self.assertEqual(duplicate["seq"],first["seq"])
        with self.db() as c:
            row=c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?",(first["candidate_id"],)).fetchone()
            columns={item[1] for item in c.execute("PRAGMA table_info(completion_candidates)")}
        for forbidden in ("title","preview","text","token","destination"):self.assertNotIn(forbidden,columns)
        self.assertNotIn(TOKEN,str(row));self.assertNotIn(str(CHAT),str(row));self.assertNotIn("synthetic recovery test",str(row))
        stale=self.record(self.identity(created_at=time.time()-self.recovery.TTL_SECONDS-2))
        self.assertFalse(stale["ok"]);self.assertEqual(stale["reason"],"candidate_expired")

    def test_cancel_tombstone_prevents_late_registration_and_route_destination_epoch_changes_cancel(self):
        ident=self.identity()
        cancelled=self.invoke({"action":"completion_recovery_cancel","identity":{
            "route":ROUTE,"runtime_id":self.sid,"final_row_id":self.final_id},"reason":"new_work"})
        self.assertTrue(cancelled["ok"])
        late=self.record(ident);self.assertFalse(late["ok"]);self.assertEqual(late["state"],"cancelled")
        self.assertEqual(self.listing()["items"],[])

        fresh=self.record(self.identity(runtime_id="fresh-runtime",session_key="fresh-session"));self.assertTrue(fresh["ok"])
        changed=self.record(self.identity(runtime_id="fresh-runtime",session_key="fresh-session",
                                          final_id=fresh["final_row_id"],epoch="other-epoch"))
        self.assertFalse(changed["ok"]);self.assertEqual(changed["reason"],"identity_changed")
        with patch.object(self.notifier,"credentials",return_value=("654321:different",-100999,12)):
            self.assertEqual(self.listing()["items"],[])
        with self.db() as c:
            self.assertEqual(c.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",(fresh["candidate_id"],)).fetchone()[0],"cancelled")

    def test_active_and_total_bounds_ttl_and_terminal_cleanup(self):
        with patch.object(self.recovery,"MAX_ACTIVE",2),patch.object(self.recovery,"MAX_TOTAL",3):
            a=self.record(self.identity(runtime_id="runtime-a"));b=self.record(self.identity(runtime_id="runtime-b"))
            self.assertTrue(a["ok"] and b["ok"])
            capped=self.record(self.identity(runtime_id="runtime-c"))
            self.assertEqual(capped["reason"],"candidate_limit")
            self.invoke({"action":"completion_recovery_cancel","identity":{"route":ROUTE,"runtime_id":"runtime-a","final_row_id":a["final_row_id"]},"reason":"new_work"})
            d=self.record(self.identity(runtime_id="runtime-d"));self.assertTrue(d["ok"],d)
            self.assertLessEqual(self.recovery.diagnostics(self.state)["awaiting_recovery"],2)
            with self.db() as c:self.assertLessEqual(c.execute("SELECT count(*) FROM completion_candidates").fetchone()[0],3)
            touched=self.invoke({"action":"completion_recovery_touch","candidate":{"candidate_id":d["candidate_id"],"revision":d["revision"],"generation":d["generation"]},"bookkeeping_seq":6,"quiet_at":time.time()})
            self.assertTrue(touched["ok"]);self.assertEqual(touched["expires_at"],d["expires_at"])

    def test_claim_cas_supersedes_only_expired_pre_dispatch_and_old_claim_cannot_send(self):
        item=self.record();first=self.claim(item);self.assertTrue(first["ok"])
        stale=self.invoke({"action":"completion_recovery_claim","candidate":{"candidate_id":first["candidate_id"],"revision":item["revision"]},"route":ROUTE})
        self.assertFalse(stale["ok"]);self.assertEqual(stale["reason"],"candidate_changed")
        with patch.object(self.recovery,"time",SimpleNamespace(time=lambda:time.time()+self.recovery.CLAIM_SECONDS+1)):
            second=self.invoke({"action":"completion_recovery_claim","candidate":{"candidate_id":first["candidate_id"],"revision":first["revision"]},"route":ROUTE})
        self.assertTrue(second["ok"]);self.assertNotEqual(first["claim_token"],second["claim_token"])
        key=self.notifier.completion_dedupe_key(self.home,"default",self.sid,self.sid,self.final_id)
        with self.notifier.state_db(self.state):pass
        late=self.recovery.begin_dispatch(self.state,self.home,{"candidate_id":first["candidate_id"],"revision":first["revision"],"claim_token":first["claim_token"]},
            ROUTE,dedupe_key=key,profile="default",session_key=self.sid,runtime_id=self.sid,started_at="1700000000.0",epoch="epoch-fixture",final_row_id=self.final_id)
        self.assertFalse(late["ok"]);self.assertEqual(late["reason"],"candidate_changed")
        with self.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM deliveries").fetchone()[0],0)

    def test_exact_connect_failure_receipt_can_retry_but_edit_failure_is_sent(self):
        item=self.record();claim=self.claim(item)
        credential=lambda *args,**kwargs:(TOKEN,CHAT,TOPIC)
        with patch.object(self.notifier,"credentials",side_effect=credential),patch.object(self.notifier,"completion_telegram",return_value={"kind":"proven_unsent"}):
            failed=self.invoke(self.send_request(item,claim))
        self.assertEqual(failed["status"],"unsent_retryable")
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status FROM deliveries").fetchone()[0],"unsent_retryable")
            proof=c.execute("SELECT state,proof_attempt FROM completion_candidates").fetchone()
        self.assertEqual(proof[0],"unsent_retryable");self.assertTrue(proof[1])
        restored=self.listing()["items"];self.assertEqual(len(restored),1);claim2=self.claim(restored[0])
        def transport(_token,method,payload):
            if method=="sendMessage":return {"kind":"response","response":{"ok":True,"result":{"message_id":77,"chat":{"id":CHAT,"type":"supergroup"},"text":payload["text"]}}}
            return {"kind":"unknown"}
        with patch.object(self.notifier,"credentials",side_effect=credential),patch.object(self.notifier,"completion_telegram",side_effect=transport) as api:
            sent=self.invoke(self.send_request(restored[0],claim2))
        self.assertTrue(sent["ok"],sent);self.assertEqual(sent["status"],"sent");self.assertFalse(sent["verified"])
        self.assertEqual([call.args[1] for call in api.call_args_list],["sendMessage","editMessageText"])
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status,message_id,verified FROM deliveries").fetchone(),("sent",77,0))
            self.assertEqual(c.execute("SELECT state FROM completion_candidates").fetchone()[0],"sent")
        self.assertEqual(self.listing()["items"],[])

    def test_old_delivery_states_are_never_overwritten_or_sent_again(self):
        for index,status in enumerate(("sending","unknown","failed","sent")):
            with self.subTest(status=status):
                sid=f"existing-{index}";final_id=self.make_session(sid)
                item=self.record(self.identity(runtime_id=sid,session_key=sid,final_id=final_id))
                claim=self.claim(item)
                checked=self.notifier.inspect_session({"profile":"default","session_key":sid,"final_row_id":final_id,"desktop_observed":True},root=self.home)
                key=self.notifier.completion_dedupe_key(self.home,"default",checked["lineage"],sid,final_id)
                with self.notifier.state_db(self.state) as c:
                    c.execute("INSERT INTO deliveries(dedupe_key,profile,session_key,status,created,message_id,verified) VALUES(?,?,?,?,?,?,?)",
                        (key,"default",sid,status,10.0,123 if status=="sent" else None,1 if status=="sent" else 0))
                with self.db() as c:before=c.execute("SELECT status,created,message_id,verified FROM deliveries WHERE dedupe_key=?",(key,)).fetchone()
                with patch.object(self.notifier,"credentials",return_value=(TOKEN,CHAT,TOPIC)),patch.object(self.notifier,"completion_telegram",side_effect=AssertionError("must not send")):
                    result=self.invoke(self.send_request(item,claim,final_id=final_id,runtime_id=sid,session_key=sid))
                self.assertTrue(result.get("duplicate") or result.get("reason") in {"identity_changed","candidate_changed"},result)
                with self.db() as c:after=c.execute("SELECT status,created,message_id,verified FROM deliveries WHERE dedupe_key=?",(key,)).fetchone()
                self.assertEqual(after,before)

    def test_status_is_pii_free_and_off_cleanup_preserves_dispatched_ledger(self):
        pending=self.record(self.identity(runtime_id="pending-runtime",session_key="pending-session"))
        dispatching=self.record(self.identity(runtime_id="dispatching-runtime",session_key="dispatching-session"))
        claimed=self.claim(dispatching)
        key=self.notifier.completion_dedupe_key(self.home,"default","dispatching-session","dispatching-session",dispatching["final_row_id"])
        with self.notifier.state_db(self.state):pass
        started=self.recovery.begin_dispatch(self.state,self.home,{"candidate_id":claimed["candidate_id"],"revision":claimed["revision"],"claim_token":claimed["claim_token"],"generation":claimed["generation"]},
            ROUTE,dedupe_key=key,profile="default",session_key="dispatching-session",runtime_id="dispatching-runtime",started_at="1700000000.0",epoch="epoch-fixture",final_row_id=dispatching["final_row_id"])
        self.assertTrue(started["ok"],started)
        dbpath=self.state/"notifications.sqlite3";before=dbpath.read_bytes()
        status=self.invoke({"action":"settings_status"})
        self.assertTrue(status["ok"],status);self.assertEqual(dbpath.read_bytes(),before)
        encoded=json.dumps(status,ensure_ascii=False)
        for private in (TOKEN,str(CHAT),str(TOPIC),self.sid,"synthetic recovery test"):self.assertNotIn(private,encoded)
        self.assertEqual(set(status["sender"]),{"version","available","awaiting_recovery","uncertain_delivery","sent","expired"})

        off=self.invoke({"action":"settings_set","key":"completion_notifications_enabled","value":False,"expected":True})
        self.assertTrue(off["ok"],off);self.assertGreaterEqual(off["cleanup"]["completion_recovery"],1)
        with self.db() as c:
            states={row[0]:row[1] for row in c.execute("SELECT candidate_id,state FROM completion_candidates")}
            delivery=c.execute("SELECT status FROM deliveries WHERE dedupe_key=?",(key,)).fetchone()[0]
        self.assertEqual(states[pending["candidate_id"]],"disabled")
        self.assertEqual(states[dispatching["candidate_id"]],"dispatching")
        self.assertEqual(delivery,"sending")
        on=self.invoke({"action":"settings_set","key":"completion_notifications_enabled","value":True,"expected":False})
        self.assertTrue(on["ok"],on);self.assertEqual(self.listing()["items"],[])
        with self.db() as c:self.assertEqual(c.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",(pending["candidate_id"],)).fetchone()[0],"disabled")

    def test_direct_transport_classification_is_narrow_no_retry_and_secret_free(self):
        import httpx
        class Response:
            def __init__(self,value=None,error=None):self.value=value;self.error=error
            def json(self):
                if self.error:raise self.error
                return self.value
        class Client:
            error=None;response=None;instances=[]
            def __init__(self,*,timeout,follow_redirects,trust_env,transport):
                self.options=(timeout,follow_redirects,trust_env,transport);self.calls=0;self.instances.append(self)
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def post(self,*args,**kwargs):
                self.calls+=1
                if self.error:raise self.error
                return self.response
        cases=((httpx.ConnectError("synthetic secret URL"),"proven_unsent"),(httpx.ConnectTimeout("synthetic secret URL"),"proven_unsent"),
            (httpx.ReadTimeout("synthetic secret URL"),"unknown"),(httpx.WriteError("synthetic secret URL"),"unknown"),
            (httpx.RemoteProtocolError("synthetic secret URL"),"unknown"))
        with patch.object(httpx,"Client",Client):
            for error,expected in cases:
                Client.error=error;Client.response=None
                value=self.notifier.completion_telegram(TOKEN,"sendMessage",{"text":"synthetic"})
                self.assertEqual(value["kind"],expected);self.assertNotIn("secret URL",str(value))
                client=Client.instances[-1];self.assertEqual(client.options[:3],(8,False,False))
                self.assertEqual(client.options[3]._pool._retries,0);self.assertEqual(client.calls,1)
            Client.error=None;Client.response=Response(error=ValueError("bad JSON"))
            self.assertEqual(self.notifier.completion_telegram(TOKEN,"sendMessage",{})["kind"],"unknown")
            Client.response=Response({"ok":False,"error_code":400,"description":"rejected"})
            self.assertEqual(self.notifier.completion_telegram(TOKEN,"sendMessage",{})["kind"],"rejected")

if __name__=="__main__":unittest.main(verbosity=2)
