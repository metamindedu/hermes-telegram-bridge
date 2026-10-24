"""Namespace transport tests; real-host mount/auth qualification is separate."""
from __future__ import annotations
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types
import unittest
from unittest.mock import patch
import test_support

SOURCE = Path(__file__).resolve().parent

class NativeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = test_support.temporary_directory(prefix='native-api-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.package = self.home / 'plugins' / 'session-complete-telegram'
        shutil.copytree(SOURCE, self.package, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        self.names = []
        self.addCleanup(self.clean_modules)
        self.native = self.load('hermes_plugins.bridge_api_fixture', self.package / '__init__.py', True)
        self.api = self.load('bridge_http_api_fixture', self.package / 'dashboard' / 'plugin_api.py')
    def load(self, name, file, package=False):
        self.names.append(name)
        spec = importlib.util.spec_from_file_location(name, file, submodule_search_locations=[str(file.parent)] if package else None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    def clean_modules(self):
        for name in list(sys.modules):
            if any(name == n or name.startswith(n + '.') for n in self.names): sys.modules.pop(name,None)
    def register(self, owner='session-complete-telegram'):
        commands=[]
        ctx=types.SimpleNamespace(plugin_id=owner,register_cli_command=lambda **kw:commands.append(kw),register_telegram_handler=lambda factory:None)
        self.native.register(ctx)
        return ctx,commands
    def token(self, value):
        return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app=FastAPI()
        app.include_router(self.api.router,prefix='/api/plugins/session-complete-telegram')
        return TestClient(app)
    def guards(self):
        return patch.object(self.api,'_checked_package_root',return_value=self.package)
    def test_registered_dispatch_uses_actual_captured_context_and_no_stdout(self):
        import contextlib,io
        ctx,_=self.register()
        with patch.object(self.native,'resolve_package_home',return_value=self.home), patch.object(self.native.notifier if hasattr(self.native,'notifier') else __import__(self.native.__name__+'.notifier',fromlist=['notifier']),'dispatch_request',return_value={'ok':True}) as dispatch:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):result=self.native.dispatch_api_request(self.token({'action':'settings_status'}))
        self.assertEqual(result,{'ok':True})
        self.assertEqual(output.getvalue(),'')
        self.assertIs(dispatch.call_args.kwargs['ctx'],ctx)
        self.assertIs(dispatch.call_args.kwargs['native'],True)
        self.assertEqual(dispatch.call_args.kwargs['root'],self.home)
    def test_unregistered_or_wrong_context_is_refused(self):
        token=self.token({'action':'settings_status'})
        self.assertEqual(self.native.dispatch_api_request(token)['reason'],'native_api_context_unavailable')
        self.register('foreign-owner')
        self.assertEqual(self.native.dispatch_api_request(token)['reason'],'native_plugin_owner_mismatch')
    def test_same_request_validation_is_preserved(self):
        self.register()
        with patch.object(self.native,'resolve_package_home',return_value=self.home):
            for token in ['bad=',self.token([]),base64.urlsafe_b64encode(b'{"action":"a","action":"b"}').decode().rstrip('=')]:
                self.assertIs(self.native.dispatch_api_request(token)['ok'],False)
    def test_capability_has_no_dispatch_and_exact_shape(self):
        ctx,_=self.register()
        with self.guards(), patch.object(self.api,'_registered_package',return_value=self.native), self.client() as client:
            response=client.get('/api/plugins/session-complete-telegram/bridge/v1/capabilities')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'ok':True,'transport':'desktop-telegram-api-v1','plugin':'session-complete-telegram','profile':'default'})
    def test_post_dispatches_once_and_returns_original_result(self):
        self.register()
        with self.guards(),patch.object(self.api,'_registered_package',return_value=self.native),patch.object(self.native,'dispatch_api_request',return_value={'ok':True,'read_only':True}) as dispatch,self.client() as client:
            response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',json={'request':self.token({'action':'settings_status'})})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'transport':'desktop-telegram-api-v1','dispatched':True,'result':{'ok':True,'read_only':True}})
        self.assertEqual(dispatch.call_count,1)
    def test_invalid_envelopes_never_resolve_dispatcher(self):
        with patch.object(self.api,'_registered_package',side_effect=AssertionError('invalid body reached owner')) as owner,self.client() as client:
            for raw in [b'[]',b'{"request":"x","request":"y"}',b'{"request":"x","extra":true}',b'{"request":true}',b'{"request":"x"}' + b' ' * 40000]:
                response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',content=raw,headers={'Content-Type':'application/json'})
                self.assertIn(response.status_code,[400,413])
        owner.assert_not_called()
    def test_guard_failure_and_post_exception_do_not_claim_success(self):
        with patch.object(self.api,'_checked_package_root',side_effect=RuntimeError('foreign_scope')),self.client() as client:
            response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',json={'request':'e30'})
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.json()['dispatched'])
        self.register()
        with self.guards(),patch.object(self.api,'_registered_package',return_value=self.native),patch.object(self.native,'dispatch_api_request',side_effect=RuntimeError('SECRET-MUST-NOT-LEAK')),self.client() as client:
            response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',json={'request':'e30'})
        self.assertEqual(response.status_code,500)
        self.assertTrue(response.json()['dispatched'])
        self.assertNotIn('SECRET-MUST-NOT-LEAK',response.text)
    def test_native_module_lookup_requires_unique_physical_source(self):
        self.register()
        with patch('hermes_cli.plugins.discover_plugins'):
            self.assertIs(self.api._registered_package(self.package),self.native)
            alias=types.ModuleType('hermes_plugins.ambiguous_api_fixture')
            alias.__file__=self.native.__file__
            alias.API_TRANSPORT=self.native.API_TRANSPORT
            alias.dispatch_api_request=self.native.dispatch_api_request
            alias.api_context_ready=self.native.api_context_ready
            sys.modules[alias.__name__]=alias
            self.names.append(alias.__name__)
            with self.assertRaises(RuntimeError):self.api._registered_package(self.package)

    def test_cancelled_unstarted_reservation_cannot_later_dispatch(self):
        self.assertTrue(self.api._slots.acquire(blocking=False))
        lease=self.api._SlotLease()
        lease.cancel_unstarted()
        lease.cancel_unstarted()
        with patch.object(self.api,'_dispatch',side_effect=AssertionError('cancelled worker ran')) as dispatch:
            response=lease.run('e30')
        self.assertEqual(response.status_code,503)
        dispatch.assert_not_called()
        acquired=[]
        try:
            for _ in range(4):
                self.assertTrue(self.api._slots.acquire(blocking=False))
                acquired.append(True)
            self.assertFalse(self.api._slots.acquire(blocking=False))
        finally:
            for _ in acquired:self.api._slots.release()
    def test_cancelled_started_requests_hold_capacity_until_workers_retire(self):
        import threading
        release=threading.Event()
        started=[threading.Event() for _ in range(4)]
        threads=[]
        leases=[]
        results=[]
        def blocked(token):
            started[int(token)].set()
            if not release.wait(5):raise RuntimeError('fixture release timed out')
            return {'ok':True}
        with patch.object(self.api,'_dispatch',side_effect=blocked):
            try:
                for i in range(4):
                    self.assertTrue(self.api._slots.acquire(blocking=False))
                    lease=self.api._SlotLease();leases.append(lease)
                    thread=threading.Thread(target=lambda l=lease,n=i:results.append(l.run(str(n))))
                    threads.append(thread);thread.start()
                for event in started:self.assertTrue(event.wait(3))
                for lease in leases:lease.cancel_unstarted()
                self.assertFalse(self.api._slots.acquire(blocking=False),'HTTP cancellation must not release running capacity')
            finally:
                release.set()
                for thread in threads:thread.join(5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(len(results),4)
        self.assertTrue(self.api._slots.acquire(blocking=False));self.api._slots.release()
    def test_wrong_media_type_and_invalid_result_are_non_replayed(self):
        with self.client() as client:
            self.assertEqual(client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',content=b'{}').status_code,415)
        self.register()
        for value in [[],{'ok':'truthy'},{'ok':True,'data':'x'*25000},{'ok':True,'data':float('nan')},
                      {'ok':False,'reason':'invalid_helper_result'},{'ok':False,'reason':'helper_result_too_large'}]:
            with self.guards(),patch.object(self.api,'_registered_package',return_value=self.native),patch.object(self.native,'dispatch_api_request',return_value=value) as dispatch,self.client() as client:
                response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch',json={'request':'e30'})
            self.assertEqual(response.status_code,500)
            self.assertTrue(response.json()['dispatched'])
            self.assertEqual(dispatch.call_count,1)
    def test_unload_retires_only_its_exact_registration(self):
        callbacks=[]
        def ctx():return types.SimpleNamespace(plugin_id='session-complete-telegram',register_cli_command=lambda **kw:None,register_telegram_handler=lambda f:None,on_unload=callbacks.append)
        self.native.register(ctx());self.native.register(ctx())
        callbacks[0]();self.assertTrue(self.native.api_context_ready())
        callbacks[1]();self.assertFalse(self.native.api_context_ready())

    def test_warm_context_does_not_repeat_global_discovery(self):
        self.register()
        with patch('hermes_cli.plugins.discover_plugins',side_effect=AssertionError('warm call re-entered native startup')) as discover:
            self.assertIs(self.api._registered_package(self.package),self.native)
        discover.assert_not_called()
    def test_explicit_foreign_or_duplicate_query_owner_is_denied_before_dispatch(self):
        with patch.object(self.api,'_registered_package',side_effect=AssertionError('query guard missed')) as owner,self.client() as client:
            for query in ['?profile=other','?profile=default&profile=default','?profile=default&extra=x','?profile=DEFAULT']:
                self.assertEqual(client.get('/api/plugins/session-complete-telegram/bridge/v1/capabilities'+query).status_code,503)
                response=client.post('/api/plugins/session-complete-telegram/bridge/v1/dispatch'+query,json={'request':'e30'})
                self.assertEqual(response.status_code,503)
                self.assertFalse(response.json()['dispatched'])
        owner.assert_not_called()

if __name__=='__main__':unittest.main()
