import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_support
from hermes_state import SessionDB
import notifier
from agent.secret_scope import (
    UnscopedSecretError, get_secret, reset_multiplex_context, reset_secret_scope,
    set_multiplex_context, set_secret_scope,
)


class CredentialScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix='credential-scope-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env_path = self.home / '.env'
        self.env_path.write_text(
            'TOKEN=dotenv-token\nTOPIC=6\n'
            'TELEGRAM_BOT_TOKEN=123456:${TOKEN}\n'
            'TELEGRAM_HOME_CHANNEL=98:${TOPIC}\n', encoding='utf-8')

    def scoped(self, values, *, home=None):
        return set_secret_scope(values, profile_home=str(home or self.home))

    def test_credentials_uses_real_default_scope_not_env_file_or_process_fallback(self):
        import dotenv

        original_read_text = Path.read_text
        env_path = self.env_path.resolve()

        def trap_fixture_env(path, *args, **kwargs):
            if Path(path).resolve() == env_path:
                raise AssertionError('credentials must not read the fixture .env')
            return original_read_text(path, *args, **kwargs)

        multiplex = set_multiplex_context(True)
        scope = self.scoped({'TELEGRAM_BOT_TOKEN': '654321:scoped-token',
                             'TELEGRAM_HOME_CHANNEL': '99:7'})
        try:
            with (patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '111111:process-token',
                                          'TELEGRAM_HOME_CHANNEL': '88:8'}),
                  patch.object(Path, 'read_text', new=trap_fixture_env),
                  patch.object(dotenv, 'dotenv_values', side_effect=AssertionError('dotenv_values must not read credentials'))):
                self.assertEqual(notifier.credentials(self.home), ('654321:scoped-token', 99, 7))
        finally:
            reset_secret_scope(scope)
            reset_multiplex_context(multiplex)

    def test_secret_scope_preserves_literal_dollar_and_braces_without_interpolation(self):
        import dotenv

        literal = '654321:literal-$${MISSING}-{brace}'
        original_read_text = Path.read_text
        env_path = self.env_path.resolve()
        def trap_fixture_env(path, *args, **kwargs):
            if Path(path).resolve() == env_path:
                raise AssertionError('credentials must not read the fixture .env')
            return original_read_text(path, *args, **kwargs)

        multiplex = set_multiplex_context(True)
        scope = self.scoped({'TELEGRAM_BOT_TOKEN': literal,
                             'TELEGRAM_HOME_CHANNEL': '99:7'})
        try:
            with (
                patch.object(Path, 'read_text', new=trap_fixture_env),
                patch.object(dotenv, 'dotenv_values', side_effect=AssertionError('dotenv_values must not read credentials')),
            ):
                self.assertEqual(get_secret('TELEGRAM_BOT_TOKEN'), literal)
                with self.assertRaisesRegex(ValueError, 'telegram_token_missing'):
                    notifier.credentials(self.home)
        finally:
            reset_secret_scope(scope)
            reset_multiplex_context(multiplex)

    def test_default_and_named_scope_are_context_owned_and_unscoped_multiplex_fails_closed(self):
        default = {'TELEGRAM_BOT_TOKEN': '654321:default-token',
                   'TELEGRAM_HOME_CHANNEL': '99:7'}
        named = {'TELEGRAM_BOT_TOKEN': '765432:named-token',
                 'TELEGRAM_HOME_CHANNEL': '88:6'}
        named_home = self.home / 'profiles' / 'taxkeeper'
        named_home.mkdir(parents=True)
        multiplex = set_multiplex_context(True)
        try:
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '111111:process-token',
                                         'TELEGRAM_HOME_CHANNEL': '77:5'}):
                default_scope = self.scoped(default, home=self.home)
                try:
                    self.assertEqual(notifier.credentials(named_home), ('654321:default-token', 99, 7))
                finally:
                    reset_secret_scope(default_scope)
                named_scope = self.scoped(named, home=named_home)
                try:
                    # ``root`` remains the authority/state context; secret selection follows
                    # Hermes' active scope and never borrows another profile's process env.
                    self.assertEqual(notifier.credentials(self.home), ('765432:named-token', 88, 6))
                    missing_scope = set_secret_scope({}, profile_home=str(named_home))
                    try:
                        with self.assertRaisesRegex(ValueError, 'telegram_token_missing'):
                            notifier.credentials(self.home)
                    finally:
                        reset_secret_scope(missing_scope)
                finally:
                    reset_secret_scope(named_scope)
                cleared = set_secret_scope(None)
                try:
                    with self.assertRaises(UnscopedSecretError):
                        notifier.credentials(self.home)
                finally:
                    reset_secret_scope(cleared)
        finally:
            reset_multiplex_context(multiplex)

    def test_standalone_unscoped_secret_failure_stays_denied_without_error_text(self):
        multiplex = set_multiplex_context(True)
        cleared = set_secret_scope(None)
        fake_token = '111111:synthetic-process-token'
        try:
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': fake_token,
                                         'TELEGRAM_HOME_CHANNEL': '77:5'}):
                result = notifier.dispatch_request({'action': 'probe'}, root=self.home,
                                                   state=self.home / 'state')
        finally:
            reset_secret_scope(cleared)
            reset_multiplex_context(multiplex)
        self.assertEqual(result, {'ok': False, 'reason': 'UnscopedSecretError'})
        self.assertNotIn(fake_token, json.dumps(result))

    def test_credentials_rejects_malformed_home_and_topic_without_dotenv_parsing(self):
        multiplex = set_multiplex_context(True)
        try:
            malformed_token = self.scoped({'TELEGRAM_BOT_TOKEN': 'not-a-telegram-token',
                                           'TELEGRAM_HOME_CHANNEL': '99:7'})
            try:
                with self.assertRaisesRegex(ValueError, 'telegram_token_missing'):
                    notifier.credentials(self.home)
            finally:
                reset_secret_scope(malformed_token)
            malformed_chat = self.scoped({'TELEGRAM_BOT_TOKEN': '654321:scoped-token',
                                          'TELEGRAM_HOME_CHANNEL': 'private-chat'})
            try:
                with self.assertRaisesRegex(ValueError, 'telegram_home_missing_or_unsupported'):
                    notifier.credentials(self.home)
            finally:
                reset_secret_scope(malformed_chat)
            malformed_topic = self.scoped({'TELEGRAM_BOT_TOKEN': '654321:scoped-token',
                                           'TELEGRAM_HOME_CHANNEL': '99:topic{7}'})
            try:
                with self.assertRaisesRegex(ValueError, 'telegram_home_missing_or_unsupported'):
                    notifier.credentials(self.home)
            finally:
                reset_secret_scope(malformed_topic)
        finally:
            reset_multiplex_context(multiplex)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix='notify-test-')
        self.root = Path(self.temp.name)
        self.home = self.root
        self.state = self.root / 'sender-state'
        self.sid = 'desktop-fixture'
        self.home.mkdir(parents=True, exist_ok=True)
        nl = chr(10)
        (self.home / 'config.yaml').write_text(nl.join((
            'plugins:', '  enabled: [session-complete-telegram]', '  entries:',
            '    session-complete-telegram:', '      settings: {}', ''
        )), encoding='utf-8')
        self.home_patch = patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.make_session(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def make_session(self, home, source='desktop', sid=None):
        sid = sid or self.sid
        home.mkdir(parents=True, exist_ok=True)
        # Real Hermes schema + real persistence, not a hand-invented schema.
        db = SessionDB(home / 'state.db')
        db.create_session(sid, source=source)
        db.set_session_title(sid, '알림 검증')
        db.append_message(sid, 'user', 'Complete the fixture.', timestamp=time.time()-180)
        final_id = db.append_message(sid, 'assistant', '작업을 완료했습니다.', timestamp=time.time()-120)
        db.close()
        return final_id

    def request(self, profile='default', home=None):
        home = home or self.home
        with notifier.db_read(home / 'state.db') as c:
            final_id = c.execute('SELECT max(id) FROM messages WHERE session_id=?',(self.sid,)).fetchone()[0]
        return {'profile':profile,'session_key':self.sid,'final_row_id':final_id,'desktop_observed':True}

    def inspect(self, req=None):
        return notifier.inspect_session(req or self.request(),root=self.root)

    def execute(self, sql, params=()):
        with sqlite3.connect(self.home / 'state.db', factory=notifier.ClosingConnection) as c:
            c.execute(sql, params)

    def test_real_schema_completed_session(self):
        self.assertTrue(self.inspect()['ok'])

    def test_short_preview_preserves_newlines_and_literal_markup(self):
        text='결과입니다.\n<b>태그</b> & **강조** 😊'
        self.assertEqual(notifier.final_preview(text),'결과입니다.\n<b>태그</b> & 강조 😊')
        self.assertEqual(notifier.final_preview('가'*1200),'가'*1200)

    def test_markdown_plain_prose_and_code(self):
        result=notifier.final_preview('# 제목\n\n**굵게** *기울임* ~~취소~~\n\n- 항목\n\n[문서](https://example.com)\n\n```python\nx = 2 ** 3\n```')
        self.assertIn('굵게 기울임 취소',result)
        self.assertIn('• 항목',result)
        self.assertIn('문서 (https://example.com)',result)
        self.assertIn('x = 2 ** 3',result)
        self.assertNotIn('```',result)
        self.assertNotIn('# 제목',result)
        self.assertEqual(notifier.final_preview('`file_name ** glob`'),'file_name ** glob')

    def test_approval_commands_are_not_markdown(self):
        command='printf "**keep**"; echo *.py _name_'
        self.assertIn(command,notifier.ask_preview('approval',{'command':command}))
        self.assertIn(command,notifier.ask_preview('sudo',{'command':command}))

    def test_clarify_markdown_choices_are_readable(self):
        result=notifier.ask_preview('clarify',{'question':'**어떤 방법**인가요?','choices':['**첫 번째**','`두 번째`']})
        self.assertIn('어떤 방법인가요?',result)
        self.assertIn('1) 첫 번째',result)
        self.assertIn('2) 두 번째',result)
        self.assertNotIn('**',result)

    def test_markdown_is_removed_before_preview_cut(self):
        result=notifier.final_preview('**'+('가'*2000)+'**')
        self.assertEqual(len(result),1200)
        self.assertNotIn('**',result)
        self.assertIn(notifier.PREVIEW_MARKER,result)

    def test_long_preview_keeps_head_and_tail_with_middle_marker(self):
        text='시작-'+('가나다😊'*900)+'-마지막 결론'
        result=notifier.final_preview(text)
        self.assertEqual(len(result),1200)
        self.assertTrue(result.startswith(text[:900]))
        self.assertTrue(result.endswith('-마지막 결론'))
        self.assertEqual(result.count(notifier.PREVIEW_MARKER),1)

    def test_preview_redacts_before_truncation(self):
        from agent.redact import redact_sensitive_text
        token='sk-'+('A1b2C3d4'*8)
        text=('가'*880)+' API_KEY='+token+'\n'+('나'*1400)
        result=notifier.final_preview(text)
        self.assertNotEqual(redact_sensitive_text(text,force=True),text)
        self.assertNotIn(token,result)
        self.assertEqual(result,notifier.final_preview(redact_sensitive_text(text,force=True,redact_url_credentials=True)))

    def test_emoji_preview_and_maximum_header_fit_single_telegram_message(self):
        text=notifier.notification_text({'title':'😊'*200,'profile':'p'*80,'final_preview':notifier.final_preview('😊'*6000)})
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)

    def test_preview_stays_out_of_normal_inspection_and_uses_named_home(self):
        self.assertNotIn('final_preview',self.inspect())
        sid='named-preview'
        final_id=self.make_session(self.root/'profiles'/'claire',sid=sid)
        result=notifier.inspect_session({'profile':'default','session_key':sid,'final_row_id':final_id,'desktop_observed':True},root=self.root,include_preview=True)
        self.assertEqual(result['final_preview'],'작업을 완료했습니다.')
        self.assertEqual(result['profile'],'claire')

    def test_delivery_uses_only_persisted_final_preview_and_same_edit_text(self):
        raw='시작-'+('가나다😊'*900)+'-마지막 결론'
        self.execute("UPDATE messages SET content=? WHERE role='assistant'",(raw,))
        payloads=[]
        def api(token,method,payload):
            payloads.append((method,payload))
            return self.fake_api([])(token,method,payload)
        req={**self.request(),'final_preview':'renderer-injected-content'}
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            result=notifier.deliver(req,root=self.root,state=self.state,api=api)
        sent=payloads[0][1]
        self.assertTrue(result['ok'] and result['verified'])
        self.assertIn(notifier.final_preview(raw),sent['text'])
        self.assertNotIn('renderer-injected-content',sent['text'])
        self.assertNotIn('Complete the fixture.',sent['text'])
        self.assertNotIn('parse_mode',sent)
        self.assertEqual(sent['text'],payloads[1][1]['text'])
        self.assertIs(sent['protect_content'],False)
        self.assertNotIn('final_preview',result)

    def test_plain_markdown_and_code_notifications_remain_copyable(self):
        cases=(('일반 텍스트 복사 확인.','일반 텍스트 복사 확인.'),
               ('**강조**와 `inline_code` 완료.','강조와 inline_code 완료.'),
               ('```python\nvalue = 2 ** 3\nprint(value)\n```','value = 2 ** 3\nprint(value)'))
        for index,(raw,expected) in enumerate(cases):
            with self.subTest(content=raw):
                self.execute("UPDATE messages SET content=? WHERE role='assistant'",(raw,))
                payloads=[]
                with patch.object(notifier,'credentials',return_value=('test',12,None)):
                    result=notifier.deliver(self.request(),root=self.root,state=self.state/str(index),api=self.capture_api(payloads))
                self.assertTrue(result['ok'] and result['verified'])
                sent=payloads[0][1]
                self.assertIs(sent['protect_content'],False)
                self.assertIn(expected,sent['text'])
                self.assertEqual(sent['text'],payloads[1][1]['text'])

    def test_copyable_approval_notification_preserves_literal_command(self):
        command='printf "**keep**"; echo *.py _name_'
        payloads=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            result=notifier.deliver_attention(self.ask_request(ask_method='approval',ask_params={'command':command}),root=self.root,state=self.state,api=self.capture_api(payloads))
        self.assertTrue(result['ok'] and result['verified'])
        self.assertIs(payloads[0][1]['protect_content'],False)
        self.assertIn(command,payloads[0][1]['text'])

    def test_actual_queued_user_row_prevents_notification(self):
        req=self.request()
        db=SessionDB(self.home / 'state.db')
        db.append_message(self.sid,'user','One more queued request.')
        db.close()
        self.assertEqual(self.inspect(req)['reason'],'newer_or_unfinished_work')

    def test_real_active_lease_prevents_notification(self):
        self.execute('INSERT INTO session_turn_leases VALUES(?,?,?,?)',(self.sid,'fixture-holder',time.time(),time.time()+90))
        self.assertEqual(self.inspect()['reason'],'active_turn_lease')

    def test_quiet_time_is_enforced_server_side(self):
        self.execute('UPDATE messages SET timestamp=?',(time.time(),))
        self.assertEqual(self.inspect()['reason'],'quiet_period')

    def test_child_never_notifies(self):
        self.execute("UPDATE sessions SET source='subagent'")
        self.assertEqual(self.inspect()['reason'],'not_desktop_session')

    def test_user_answer_required_never_notifies(self):
        self.execute("UPDATE messages SET content='승인해 주실까요?' WHERE role='assistant'")
        self.assertEqual(self.inspect()['reason'],'awaiting_answer')

    def test_pending_todo_block_and_finished_todo_pass(self):
        db=SessionDB(self.home / 'state.db')
        db.append_message(self.sid,'tool',json.dumps({'todos':[{'id':'1','status':'pending'}],'revision':1}),tool_name='todo_list',timestamp=time.time()-180)
        db.append_message(self.sid,'assistant','중간 보고입니다.',timestamp=time.time()-120)
        db.close()
        self.assertEqual(self.inspect()['reason'],'unfinished_todos')
        self.execute("UPDATE messages SET content=? WHERE role='tool'",(json.dumps({'todos':[{'id':'1','status':'completed'}],'revision':2}),))
        self.assertTrue(self.inspect()['ok'])

    def test_pending_delegation_delivery_blocks(self):
        with sqlite3.connect(self.home/'state.db', factory=notifier.ClosingConnection) as c:
            # Use the actual schema's nullable/default fields.
            c.execute("INSERT INTO async_delegations(delegation_id,origin_session,parent_session_id,state,dispatched_at,updated_at,delivery_state) VALUES(?,?,?,?,?,?,?)",('child','fixture-origin',self.sid,'completed',time.time()-100,time.time()-90,'pending'))
        self.assertEqual(self.inspect()['reason'],'pending_delegation_delivery')

    def test_all_current_and_new_profile_names_are_isolated(self):
        profiles=['default','claire','ireum','morgan','postmaster','pychrong','satoshi','shinyoung','taxkeeper','vmaicreator','wallace','future_profile_fixture']
        results=[]
        for name in profiles:
            home=self.root if name=='default' else self.root/'profiles'/name
            if name!='default': self.make_session(home)
            results.append(notifier.inspect_session(self.request(name,home),root=self.root))
        self.assertEqual([r['profile'] for r in results],profiles)
        self.assertTrue(all(r['ok'] for r in results))

    def test_default_launch_alias_resolves_exact_session_in_named_home(self):
        sid='named-home-only'
        final_id=self.make_session(self.root/'profiles'/'claire',sid=sid)
        req={'profile':'default','session_key':sid,'final_row_id':final_id,'desktop_observed':True}
        result=notifier.inspect_session(req,root=self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['profile'],'claire')
        self.assertEqual(req['profile'],'default')

    def test_ambiguous_default_alias_fails_closed(self):
        sid='copied-session'
        final_id=self.make_session(self.root/'profiles'/'claire',sid=sid)
        self.make_session(self.root/'profiles'/'wallace',sid=sid)
        result=notifier.inspect_session({'profile':'default','session_key':sid,'final_row_id':final_id,'desktop_observed':True},root=self.root)
        self.assertEqual(result['reason'],'ambiguous_default_profile')

    def test_named_profile_never_falls_back_to_other_home(self):
        sid='claire-only'
        final_id=self.make_session(self.root/'profiles'/'claire',sid=sid)
        self.make_session(self.root/'profiles'/'wallace')
        result=notifier.inspect_session({'profile':'wallace','session_key':sid,'final_row_id':final_id,'desktop_observed':True},root=self.root)
        self.assertFalse(result['ok'])

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):notifier.profile_home('../outside',self.root)

    def test_compression_lineage_dedup_identity(self):
        db=SessionDB(self.home/'state.db')
        db.create_session('compression-parent',source='desktop')
        db.close()
        self.execute("UPDATE sessions SET end_reason='compression' WHERE id='compression-parent'")
        self.execute("UPDATE sessions SET parent_session_id='compression-parent' WHERE id=?",(self.sid,))
        self.assertEqual(self.inspect()['lineage'],'compression-parent')

    def fake_api(self, calls):
        def api(token,method,payload):
            calls.append(method)
            if method=='sendMessage':
                return {'ok':True,'result':{'message_id':88,'chat':{'id':12},'text':payload['text']}}
            return {'ok':False,'error_code':400,'description':'Bad Request: message is not modified'}
        return api

    def test_persistent_dedup_across_process_style_instances(self):
        calls=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            a=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
            b=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
        self.assertTrue(a['ok'] and a['verified'])
        self.assertTrue(b['duplicate'])
        self.assertEqual(calls,['sendMessage','editMessageText'])

    def test_followup_in_same_session_sends_again_but_each_final_deduplicates(self):
        calls=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            first=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
            db=SessionDB(self.home/'state.db')
            db.append_message(self.sid,'user','Follow-up request.',timestamp=time.time()-180)
            db.append_message(self.sid,'assistant','Follow-up finished.',timestamp=time.time()-120)
            db.close()
            second=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
            duplicate=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
        self.assertTrue(first['ok'] and second['ok'])
        self.assertFalse(second.get('duplicate',False))
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(calls.count('sendMessage'),2)

    def test_legacy_session_claim_does_not_block_new_completion(self):
        key=hashlib.sha256(json.dumps([str(self.root.resolve()),'default',self.sid],separators=(',',':')).encode()).hexdigest()
        with notifier.state_db(self.state) as c:
            c.execute('INSERT INTO deliveries(dedupe_key,profile,session_key,status,created) VALUES(?,?,?,?,?)',(key,'default',self.sid,'sent',time.time()-300))
        calls=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            result=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
        self.assertTrue(result['ok'])
        self.assertFalse(result.get('duplicate',False))
        with notifier.state_db(self.state) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM deliveries').fetchone()[0],2)

    def test_concurrent_windows_claim_only_once(self):
        calls=[]
        # Initialize before concurrent readers; exercises real BEGIN IMMEDIATE.
        notifier.state_db(self.state).close()
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                results=list(pool.map(lambda _:notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls)),range(5)))
        self.assertEqual(calls.count('sendMessage'),1)
        self.assertEqual(sum(not r.get('duplicate',False) for r in results),1)

    def test_ambiguous_delivery_never_retried(self):
        calls=[]
        def api(*args):calls.append(args[1]);return {'ok':False,'ambiguous':True,'error_code':0}
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            a=notifier.deliver(self.request(),root=self.root,state=self.state,api=api)
            b=notifier.deliver(self.request(),root=self.root,state=self.state,api=api)
        self.assertEqual(a['status'],'unknown');self.assertTrue(b['duplicate']);self.assertEqual(len(calls),1)

    def test_read_only_gate_does_not_mutate_transcript(self):
        with notifier.db_read(self.home/'state.db') as c:
            before=c.execute('SELECT count(*) FROM messages').fetchone()[0]
        self.inspect()
        with notifier.db_read(self.home/'state.db') as c:
            after=c.execute('SELECT count(*) FROM messages').fetchone()[0]
        self.assertEqual(before,after)


    def capture_api(self, payloads):
        def api(token,method,payload):
            payloads.append((method,payload))
            return self.fake_api([])(token,method,payload)
        return api

    def ask_request(self, **overrides):
        req={'kind':'ask','profile':'default','session_key':self.sid,'ask_id':'srq-a1b2c3d4e5f6',
             'ask_method':'clarify','ask_params':{'question':'어떤 색으로 할까요?','choices':['빨강','파랑']},
             'desktop_observed':True}
        req.update(overrides)
        return req

    def test_ask_preview_formats_each_human_prompt(self):
        self.assertIn('1) 빨강',notifier.ask_preview('clarify',{'question':'색?','choices':['빨강','파랑']}))
        self.assertIn('2) 둘',notifier.ask_preview('clarify',{'questions':[{'question':'하나?','choices':['하나','둘']}]}))
        self.assertIn('$ rm -rf x',notifier.ask_preview('approval',{'description':'위험한 정리','command':'rm -rf x'}))
        self.assertIn('$ sudo ls',notifier.ask_preview('sudo',{'command':'sudo ls'}))
        self.assertIn('(SECRET_TOKEN)',notifier.ask_preview('secret',{'prompt':'키 입력','env_var':'SECRET_TOKEN'}))
        self.assertIn('Bitwarden (bitwarden)',notifier.ask_preview('vault.unlock_prompt',{'display_name':'Bitwarden','backend':'bitwarden'}))
        self.assertIn('example.com (https://example.com)',notifier.ask_preview('vault.save_login',{'site':'example.com','origin':'https://example.com'}))
        self.assertIn('2FA',notifier.ask_preview('vault.code',{'site':'example.com','hint':'2FA'}))
        self.assertEqual(notifier.ask_preview('clarify',{}),'')

    def test_ask_notification_sends_and_dedupes_per_request(self):
        calls=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            a=notifier.deliver_attention(self.ask_request(),root=self.root,state=self.state,api=self.fake_api(calls))
            b=notifier.deliver_attention(self.ask_request(),root=self.root,state=self.state,api=self.fake_api(calls))
            c=notifier.deliver_attention(self.ask_request(ask_id='srq-0987654321ab',ask_method='approval',ask_params={'command':'rm -rf x'}),root=self.root,state=self.state,api=self.fake_api(calls))
        self.assertTrue(a['ok'] and a['verified'] and a['kind']=='ask')
        self.assertTrue(b['duplicate'])
        self.assertTrue(c['ok'] and not c.get('duplicate'))
        self.assertEqual(calls.count('sendMessage'),2)

    def test_ask_rejects_non_human_methods_bad_ids_and_unproven_origin(self):
        self.assertEqual(notifier.inspect_ask(self.ask_request(ask_method='window.read'),root=self.root)['reason'],'not_a_human_ask')
        self.assertEqual(notifier.inspect_ask(self.ask_request(ask_method='tour'),root=self.root)['reason'],'not_a_human_ask')
        self.assertEqual(notifier.inspect_ask(self.ask_request(ask_id='evil'),root=self.root)['reason'],'invalid_ask')
        self.assertEqual(notifier.inspect_ask(self.ask_request(desktop_observed=False),root=self.root)['reason'],'unproven_desktop_origin')
        self.assertEqual(notifier.inspect_ask(self.ask_request(ask_params=['not','a','dict']),root=self.root)['reason'],'invalid_ask_params')
        self.execute("UPDATE sessions SET source='subagent'")
        self.assertEqual(notifier.inspect_ask(self.ask_request(),root=self.root)['reason'],'not_desktop_session')

    def test_ask_uses_same_home_rules_and_requires_a_live_session_row(self):
        sid='ask-named'
        self.make_session(self.root/'profiles'/'claire',sid=sid)
        result=notifier.inspect_ask(self.ask_request(session_key=sid),root=self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['profile'],'claire')
        self.assertEqual(notifier.inspect_ask(self.ask_request(session_key='missing'),root=self.root)['reason'],'not_desktop_session')

    def test_ask_text_is_redacted_and_bounded(self):
        token='sk-'+('A1b2C3d4'*8)
        result=notifier.inspect_ask(self.ask_request(ask_params={'question':'run '+token,'choices':['가'*3000]}),root=self.root)
        self.assertNotIn(token,result['ask_preview'])
        self.assertLessEqual(len(result['ask_preview']),1200)

    def test_attention_text_fits_single_telegram_message(self):
        text=notifier.attention_text({'title':'😊'*100,'profile':'p'*80,'ask_label':'🔐 명령 승인 요청','ask_preview':notifier.final_preview('😊'*6000)})
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        self.assertIn('입력',text)

    def test_question_final_sends_input_needed_notification(self):
        self.execute("UPDATE messages SET content='빨강과 파랑 중 무엇으로 할까요?' WHERE role='assistant'")
        payloads=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            first=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.capture_api(payloads))
            second=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.capture_api(payloads))
        self.assertTrue(first['ok'] and first['verified'] and first['kind']=='question')
        self.assertTrue(second['duplicate'])
        text=payloads[0][1]['text']
        self.assertIn('질문이 있습니다',text)
        self.assertIn('빨강과 파랑',text)
        self.assertEqual(len([m for m,_ in payloads if m=='sendMessage']),1)

    def test_question_final_requires_the_question_to_stay_the_tip(self):
        self.execute("UPDATE messages SET content='계속 진행할까요?' WHERE role='assistant'")
        db=SessionDB(self.home/'state.db')
        db.append_message(self.sid,'user','Yes.',timestamp=time.time()-180)
        db.close()
        result=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.capture_api([]))
        self.assertEqual(result['reason'],'newer_or_unfinished_work')

    def test_question_final_quiet_period_is_enforced(self):
        self.execute("UPDATE messages SET content='계속 진행할까요?', timestamp=? WHERE role='assistant'",(time.time(),))
        result=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.capture_api([]))
        self.assertEqual(result['reason'],'quiet_period')
        direct=notifier.deliver_attention({**self.request(),'kind':'question'},root=self.root,state=self.state,api=self.capture_api([]))
        self.assertEqual(direct['reason'],'quiet_period')

    def test_question_and_completion_use_distinct_ledger_keys(self):
        self.execute("UPDATE messages SET content='계속 진행할까요?' WHERE role='assistant'")
        calls=[]
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            question=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
        db=SessionDB(self.home/'state.db')
        db.append_message(self.sid,'user','Yes.',timestamp=time.time()-180)
        db.append_message(self.sid,'assistant','이어서 완료했습니다.',timestamp=time.time()-120)
        db.close()
        with patch.object(notifier,'credentials',return_value=('test',12,None)):
            done=notifier.deliver(self.request(),root=self.root,state=self.state,api=self.fake_api(calls))
        self.assertTrue(question['ok'] and question['kind']=='question')
        self.assertTrue(done['ok'] and 'kind' not in done)
        self.assertEqual(calls.count('sendMessage'),2)

    def test_question_final_completion_gate_does_not_suppress_attention_and_dedupes(self):
        self.execute("UPDATE messages SET content='계속 진행할까요?' WHERE role='assistant'")
        calls=[]
        def status(name, root=None, ctx=None):
            ready=name == 'attention_notifications_enabled'
            return ready, 'ready' if ready else 'completion_notifications_disabled'
        with (
            patch.object(notifier, '_action_status', side_effect=status),
            patch.object(notifier, 'credentials', return_value=('test',12,None)),
        ):
            first=notifier.deliver(self.request(),root=self.root,state=self.state,
                                   api=self.fake_api(calls))
            second=notifier.deliver(self.request(),root=self.root,state=self.state,
                                    api=self.fake_api(calls))
        self.assertTrue(first['ok'] and first['verified'] and first['kind']=='question')
        self.assertTrue(second['duplicate'])
        self.assertEqual(calls.count('sendMessage'),1)

    def test_completion_disabled_does_not_promote_an_ordinary_final_to_attention(self):
        self.assertTrue(self.inspect()['ok'])
        calls=[]
        def status(name, root=None, ctx=None):
            ready=name == 'attention_notifications_enabled'
            return ready, 'ready' if ready else 'completion_notifications_disabled'
        with (
            patch.object(notifier, '_action_status', side_effect=status),
            patch.object(notifier, 'credentials', return_value=('test',12,None)),
        ):
            result=notifier.deliver(self.request(),root=self.root,state=self.state,
                                    api=self.fake_api(calls))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'completion_notifications_disabled')
        self.assertEqual(calls,[])

    def test_attention_unknown_kind_rejected(self):
        self.assertEqual(notifier.deliver_attention({'kind':'mystery'},root=self.root,state=self.state,api=self.fake_api([]))['reason'],'unknown_attention_kind')


if __name__=='__main__':unittest.main(verbosity=2)
