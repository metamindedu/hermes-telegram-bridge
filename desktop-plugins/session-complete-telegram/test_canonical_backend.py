"""Plugin-only canonical approval bridge tests.

These tests bind the native API adapter to the declared Hermes core generation.
Legacy core characterization must report the absent canonical client safely; the
current-generation integration uses real session authority/waiters and PTB with
only the websocket identity gate and Telegram transport isolated.
"""
from __future__ import annotations

import json
import time
import unittest

import newcanonical_backend as canonical


class CanonicalPromptShapeTests(unittest.TestCase):
    def prompt(self, **changes):
        value = {
            "kind": "approval",
            "prompt_id": "opaque.native.ID/reused",
            "execution_generation": 7,
            "command": "synthetic command body",
            "description": "synthetic full description",
            "choices": ["once", "deny"],
            "edit": {"path": "fixture.txt", "before": "old", "after": "new"},
        }
        value.update(changes)
        return value

    def test_native_opaque_prompt_identity_is_preserved_without_uuid_or_srq_repair(self):
        prompt = self.prompt()
        self.assertTrue(canonical.validate_prompt(prompt))
        self.assertEqual(canonical.prompt_identity(prompt), ("opaque.native.ID/reused", 7))
        opaque = self.prompt(prompt_id=" srq-fake ")
        self.assertTrue(canonical.validate_prompt(opaque))
        self.assertEqual(canonical.prompt_identity(opaque), (" srq-fake ", 7))

    def test_boolean_generation_missing_fields_and_unknown_native_fields_are_not_actionable(self):
        self.assertFalse(canonical.validate_prompt(self.prompt(execution_generation=True)))
        self.assertFalse(canonical.validate_prompt(self.prompt().copy() | {"request_id": "srq-fake"}))
        self.assertFalse(canonical.validate_prompt(self.prompt().copy() | {"future_field": "unknown"}))
        missing = self.prompt()
        del missing["edit"]
        # Edit is optional in the canonical schema; its absence is not repaired.
        self.assertTrue(canonical.validate_prompt(missing))
        missing = self.prompt()
        del missing["choices"]
        self.assertFalse(canonical.validate_prompt(missing))

    def test_snapshot_binding_is_full_and_generation_reuse_expires_prior_identity(self):
        import hashlib
        import json
        prompt = self.prompt()
        pending = {"session_id":"opaque-session", "prompt_id":prompt["prompt_id"],
            "execution_generation":7, "instance_id":"native-instance", "authority_epoch":9,
            "replay_epoch":"replay-epoch"}
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        descriptor = {
            "dispatch_mode": "canonical_native", "owner":"desktop-owner",
            "connectionId": "desktop-connection", "profile": "claire", "targetProfile": "claire",
            "root_binding":"a" * 64, "target_home_binding":"b" * 64,
            "sid": "opaque-session", "session_key":"stored-session-key",
            "instance_id": "native-instance", "authority_epoch": 9,
            "replay_epoch": "replay-epoch", "transport_protocol":"hermes-gateway-v1",
            "prompt_id": prompt["prompt_id"], "execution_generation":7,
            "request_fingerprint":digest(prompt), "pending_fingerprint":digest(pending),
        }
        snapshot = {
            "session_id": "opaque-session",
            "stored_session_id": "stored-session-key",
            "running": True,
            "authority_epoch": 9,
            "replay_epoch": "replay-epoch",
            "execution_generation": 7,
            "subscription_id": "owned-subscription",
            "prompts": [prompt],
            "messages": [{"role":"user", "content":"not returned by the adapter"}],
            "info":{"desktop_protocol":"hermes-gateway-v1"},
        }
        self.assertTrue(canonical.snapshot_matches(snapshot, descriptor, prompt))
        for key, value in (
            ("execution_generation", 8),
            ("authority_epoch", 10),
            ("replay_epoch", "new-replay"),
            ("session_id", "other-session"),
        ):
            changed = dict(snapshot)
            changed[key] = value
            self.assertFalse(canonical.snapshot_matches(changed, descriptor, prompt), key)
        changed_prompt = dict(prompt, execution_generation=8)
        self.assertFalse(canonical.snapshot_matches(snapshot, descriptor, changed_prompt))
        large_history = dict(snapshot, messages=[{"role":"user", "content":"x" * (5 * 1024 * 1024)}])
        self.assertTrue(canonical.snapshot_matches(large_history, descriptor, prompt),
                        "ordinary multi-megabyte transcript remains under the public 8 MiB receive bound")
        oversized_history = dict(snapshot, messages=[{"role":"user", "content":"x" * (8 * 1024 * 1024)}])
        self.assertFalse(canonical.snapshot_matches(oversized_history, descriptor, prompt),
                         "oversized full snapshot is not reduced to a prompt-only actionable view")



class CanonicalInboxBindingTests(unittest.TestCase):
    """Stored native IDs, generation replacement and postwrite uncertainty never downgrade."""
    def setUp(self):
        import time
        from pathlib import Path
        from unittest.mock import patch
        import approval_bridge as bridge_module
        import notifier as notifier_module
        import telegram_integration as gateway_module
        import test_support

        self.bridge=bridge_module
        self.notifier=notifier_module
        self.temp=test_support.temporary_directory(prefix='canonical-inbox-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'home'
        self.root.mkdir()
        self.state=self.root/'plugin-data'/'session-complete-telegram'
        (self.root/'config.yaml').write_text("""plugins:
  enabled: [session-complete-telegram]
  entries:
    session-complete-telegram:
      settings:
        service_enabled: true
        attention_notifications_enabled: true
        approvals_enabled: true
approvals:
  timeout: 90
""",encoding='utf-8')
        self.now=time.time()
        self.owner='canonical-owner-fixture'
        self.route={'mode':'local','connectionId':'canonical-connection',
            'profile':'default','targetProfile':'default'}
        self.session={'id':'canonical-session-opaque','session_key':'stored-session-public-key','status':'waiting'}
        self.prompt={'kind':'approval','prompt_id':'opaque.native.ID/reused','execution_generation':7,
            'command':'synthetic canonical command','description':'complete native review',
            'choices':['once','deny'],'edit':{'path':'fixture.txt','before':'old','after':'new'},'timeout_s':90}
        self.descriptor,self.pending=self.binding(self.prompt)
        self.net=patch.object(self.notifier,'telegram',side_effect=self.telegram)
        self.cred=patch.object(self.notifier,'credentials',return_value=('77:synthetic-token',99,None))
        self.action=patch.object(self.notifier,'action_enabled',return_value=True)
        self.gate=patch.object(gateway_module,'feature_status',return_value=(True,'ready'))
        self.net.start();self.cred.start();self.action.start();self.gate.start()
        self.addCleanup(self.gate.stop);self.addCleanup(self.action.stop);self.addCleanup(self.cred.stop);self.addCleanup(self.net.stop)
        self.bridge.bind_receiver(77,99,None,'canonical-boot',private_verified=True,
            state=self.state,now=self.now)
        self.bridge.owner_heartbeat(self.owner,state=self.state,now=self.now)

    def binding(self,prompt,*,authority_epoch=9,replay_epoch='canonical-replay',connection_id=None,
                instance_id='canonical-instance',session_id=None,session_key=None):
        import newcanonical_backend as backend
        route={**self.route,'connectionId':connection_id or self.route['connectionId']}
        sid=session_id or self.session['id']
        durable_key=session_key or self.session['session_key']
        pending={'session_id':sid,'prompt_id':prompt['prompt_id'],
            'execution_generation':prompt['execution_generation'],'instance_id':instance_id,
            'authority_epoch':authority_epoch,'replay_epoch':replay_epoch}
        descriptor={'dispatch_mode':'canonical_native','owner':self.owner,
            'connectionId':route['connectionId'],'profile':route['profile'],
            'targetProfile':route['targetProfile'],'root_binding':backend.root_binding(self.root),
            'target_home_binding':backend._path_binding(self.root),'sid':sid,
            'session_key':durable_key,'instance_id':instance_id,
            'authority_epoch':authority_epoch,'replay_epoch':replay_epoch,
            'transport_protocol':'hermes-gateway-v1','prompt_id':prompt['prompt_id'],
            'execution_generation':prompt['execution_generation'],
            'request_fingerprint':backend._digest(prompt),'pending_fingerprint':backend._digest(pending)}
        return descriptor,pending

    def telegram(self,token,method,payload):
        import copy
        if not hasattr(self,'calls'):self.calls=[]
        self.calls.append((method,copy.deepcopy(payload)))
        if method=='sendMessage':
            mid=500+len(self.calls)
            return {'ok':True,'result':{'message_id':mid,'chat':{'id':payload['chat_id'],'type':'private'},
                'text':payload['text'],'message_thread_id':payload.get('message_thread_id'),
                'from':{'id':77,'is_bot':True},'reply_markup':payload.get('reply_markup')}}
        if method=='editMessageText':return {'ok':True,'result':{'message_id':payload['message_id']}}
        return {'ok':False,'error_code':400,'description':'not modified'}

    def register(self,descriptor=None,prompt=None,pending=None,*,owner=None,now=None,route=None):
        descriptor=descriptor or self.descriptor
        prompt=prompt or self.prompt
        pending=pending or self.pending
        authority_owner=owner or descriptor['owner']
        request={'action':'approval_register','capability':self.bridge.CAPABILITY,'owner':authority_owner,
            'descriptor':descriptor,'request':prompt,'pending':pending,
            'route':route or {**self.route,'connectionId':descriptor['connectionId'],
                'profile':descriptor['profile'],'targetProfile':descriptor['targetProfile']},
            'session':{'id':descriptor['sid'],'session_key':descriptor['session_key'],'status':'waiting'}}
        return self.bridge.register_canonical(request,self.notifier,root=self.root,state=self.state,
            now=self.now if now is None else now)

    def row(self,approval_id):
        connection=self.bridge.connect_readonly(self.state)
        try:return dict(connection.execute('SELECT * FROM approvals WHERE id=?',(approval_id,)).fetchone())
        finally:connection.close()

    def observation(self,approval_id):
        row=self.row(approval_id);descriptor=json.loads(row['descriptor'])
        return {'id':approval_id,'request_fingerprint':row['request_fingerprint'],
            'pending_fingerprint':row['pending_fingerprint'],'authority':self.bridge._digest(descriptor)}

    def tap(self,approval_id,*,choice='once',event='canonical-click'):
        row=self.row(approval_id)
        identity={'bot':77,'boot':'canonical-boot','chat':99,'user':99,'topic':None,
            'mid':row['message_id'],'private':True,'human':True,'bot_author':True,
            'forwarded':False,'edited':False,'inline':False,'inaccessible':False,
            'message_text':row['review_text'],'event':event}
        return self.bridge.choose(row['token'],choice,identity,state=self.state,now=max(time.time(),self.now+.1))

    def test_same_native_id_new_generation_and_edit_only_replacement_expire_old_card(self):
        from unittest.mock import patch
        import newcanonical_backend as backend
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            first=self.register()
            self.assertTrue(first.get('ok'),first)
            old=self.row(first['id'])
            next_prompt={**self.prompt,'execution_generation':8}
            next_descriptor,next_pending=self.binding(next_prompt)
            second=self.register(next_descriptor,next_prompt,next_pending)
            self.assertTrue(second.get('ok'),second)
            self.assertEqual(self.row(first['id'])['status'],'expired')
            self.assertEqual(json.loads(self.row(second['id'])['descriptor'])['execution_generation'],8)
            self.assertFalse(self.tap(first['id'],event='old-generation-tap').get('ok'))

            edited={**next_prompt,'edit':{**next_prompt['edit'],'after':'edited-only replacement'}}
            edit_descriptor,edit_pending=self.binding(edited)
            third=self.register(edit_descriptor,edited,edit_pending)
            self.assertFalse(third.get('ok'),third)
            self.assertEqual(third.get('reason'),'immutable_mismatch')
            self.assertEqual(self.row(second['id'])['status'],'expired')
            self.assertNotIn('edited-only replacement',self.row(second['id'])['review_text'] or '')
            with self.bridge.connect_readonly(self.state) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM approvals").fetchone()[0],2)

    def test_native_mode_never_uses_legacy_settle_and_lost_ack_is_terminal_unknown(self):
        from unittest.mock import patch
        import newcanonical_backend as backend
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            registered=self.register()
        self.assertTrue(registered.get('ok'),registered)
        self.assertTrue(self.tap(registered['id'],event='canonical-unknown-tap')['ok'])
        observation=self.observation(registered['id'])
        polled=self.bridge.poll(self.owner,[observation],state=self.state,now=time.time())
        self.assertEqual(len(polled['items']),1)
        item=polled['items'][0]
        self.assertEqual(self.bridge.settle(registered['id'],item['claim'],'applied',state=self.state)['reason'],
                         'canonical_apply_required')
        calls=[]
        def lost_after_write(descriptor,prompt,**kwargs):
            calls.append(kwargs['choice'])
            gate=kwargs['before_dispatch']()
            self.assertTrue(gate['ok'],gate)
            return {'ok':False,'phase':'postwrite','status':'unknown','reason':'native_transport_unavailable'}
        with patch.object(backend,'apply_bound',side_effect=lost_after_write):
            request={'action':'approval_native_apply','capability':self.bridge.CAPABILITY,
                'owner':self.owner,'id':registered['id'],'claim':item['claim']}
            first=self.bridge.apply_canonical(request,self.notifier,root=self.root,state=self.state)
            second=self.bridge.apply_canonical(request,self.notifier,root=self.root,state=self.state)
        self.assertEqual(first['status'],'unknown')
        self.assertEqual(self.row(registered['id'])['status'],'unknown')
        self.assertEqual(calls,['once'])
        self.assertIn(first['receipt'],(None,'decision_resolved'))

    def test_lost_ack_and_old_owner_bound_history_block_new_owner_after_boot_and_lease_rotation(self):
        from unittest.mock import patch
        import newcanonical_backend as backend
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            registered=self.register()
        self.assertTrue(registered.get('ok'),registered)
        self.assertTrue(self.tap(registered['id'],event='canonical-owner-a-lost-ack')['ok'])
        observation=self.observation(registered['id'])
        polled=self.bridge.poll(self.owner,[observation],state=self.state,now=time.time())
        self.assertEqual(len(polled['items']),1)
        claim=polled['items'][0]
        native_writes=[]
        def lost_after_write(descriptor,prompt,**kwargs):
            native_writes.append(kwargs['choice'])
            self.assertTrue(kwargs['before_dispatch']()['ok'])
            return {'ok':False,'phase':'postwrite','status':'unknown','reason':'native_transport_unavailable'}
        with patch.object(backend,'apply_bound',side_effect=lost_after_write):
            result=self.bridge.apply_canonical({'action':'approval_native_apply',
                'capability':self.bridge.CAPABILITY,'owner':self.owner,
                'id':registered['id'],'claim':claim['claim']},self.notifier,
                root=self.root,state=self.state)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(self.row(registered['id'])['status'],'unknown')
        self.assertEqual(native_writes,['once'])

        # Rewrite only this disposable fixture row to the prior owner-bound v6
        # identity representation; historical JSON remains the protection source.
        old_keys=('dispatch_mode','owner','connectionId','profile','targetProfile',
            'root_binding','target_home_binding','sid','session_key','instance_id',
            'authority_epoch','replay_epoch','transport_protocol','prompt_id',
            'execution_generation','request_fingerprint','pending_fingerprint')
        old_identity=self.bridge._digest({key:self.descriptor[key] for key in old_keys})
        connection=self.bridge.connect(self.state)
        try:
            connection.execute('UPDATE approvals SET identity=? WHERE id=?',(old_identity,registered['id']))
        finally:
            connection.close()

        rotated_now=self.now + (3*self.bridge.LEASE) + 1
        self.assertTrue(self.bridge.bind_receiver(77,99,None,'canonical-boot-b',
            private_verified=True,state=self.state,now=rotated_now))
        owner_b='canonical-owner-b-new-monitor'
        heartbeat=self.bridge.owner_heartbeat(owner_b,state=self.state,now=rotated_now)
        self.assertTrue(heartbeat['feature_ready'],heartbeat)
        descriptor_b={**self.descriptor,'owner':owner_b}
        calls_before=len(self.calls)
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            result_b=self.register(descriptor_b,self.prompt,self.pending,owner=owner_b,now=rotated_now)
        self.assertFalse(result_b.get('ok'),result_b)
        self.assertEqual(result_b.get('reason'),'prior_owner')
        self.assertEqual(len(self.calls),calls_before,'owner B must not publish a second Telegram card')
        self.assertEqual(native_writes,['once'],'owner B must not issue a second canonical write')
        poll_b=self.bridge.poll(owner_b,[],state=self.state,now=rotated_now)
        self.assertEqual(poll_b['items'],[],'owner B must receive no new claim')
        with self.bridge.connect_readonly(self.state) as connection:
            rows=connection.execute('SELECT id,status,identity FROM approvals').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],registered['id'])
        self.assertEqual(rows[0]['status'],'unknown')
        self.assertEqual(rows[0]['identity'],old_identity)

    def test_concurrent_owner_a_and_b_registration_has_one_native_slot_card(self):
        import threading
        from unittest.mock import patch
        import newcanonical_backend as backend
        owner_b='canonical-owner-b-concurrent'
        heartbeat=self.bridge.owner_heartbeat(owner_b,state=self.state,now=self.now)
        self.assertTrue(heartbeat['feature_ready'],heartbeat)
        descriptor_b={**self.descriptor,'owner':owner_b}
        barrier=threading.Barrier(3)
        results=[None,None]
        errors=[]
        def attempt(index,descriptor,owner):
            try:
                barrier.wait(timeout=10)
                results[index]=self.register(descriptor,self.prompt,self.pending,owner=owner)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            threads=[
                threading.Thread(target=attempt,args=(0,self.descriptor,self.owner),daemon=True),
                threading.Thread(target=attempt,args=(1,descriptor_b,owner_b),daemon=True),
            ]
            for thread in threads:thread.start()
            barrier.wait(timeout=10)
            for thread in threads:thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads),'concurrent registration did not finish')
        self.assertEqual(errors,[])
        self.assertEqual(sum(bool(result and result.get('ok')) for result in results),1,results)
        self.assertEqual(sum(result.get('reason')=='prior_owner' for result in results if result),1,results)
        with self.bridge.connect_readonly(self.state) as connection:
            rows=connection.execute("SELECT id,status,owner FROM approvals").fetchall()
        self.assertEqual(len(rows),1)
        self.assertIn(rows[0]['owner'],{self.owner,owner_b})
        self.assertEqual(sum(method=='sendMessage' for method,_payload in self.calls),1)

    def test_native_realm_epochs_generation_prompt_and_session_are_distinct_slots(self):
        from unittest.mock import patch
        import newcanonical_backend as backend
        variants=[
            (self.prompt,{'instance_id':'another-native-instance'}),
            (self.prompt,{'authority_epoch':10}),
            (self.prompt,{'replay_epoch':'another-replay-epoch'}),
            ({**self.prompt,'execution_generation':8},{}),
            ({**self.prompt,'prompt_id':'another-opaque-prompt'},{}),
            (self.prompt,{'session_id':'another-opaque-session','session_key':'another-public-session-key'}),
        ]
        accepted=[]
        with patch.object(backend,'observe_bound',return_value={'ok':True}):
            for prompt,changes in variants:
                descriptor,pending=self.binding(prompt,**changes)
                result=self.register(descriptor,prompt,pending)
                self.assertTrue(result.get('ok'),result)
                accepted.append(result['id'])
        self.assertEqual(len(set(accepted)),len(variants))
        with self.bridge.connect_readonly(self.state) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM approvals').fetchone()[0],len(variants))


if __name__ == "__main__":
    unittest.main()
