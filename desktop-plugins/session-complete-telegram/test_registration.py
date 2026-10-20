"""Registration is metadata; only invocation grants checked runtime access."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_support
from agent import secret_scope

REPO = Path(__file__).resolve().parents[2]
FRONTEND = 'session-complete-telegram'
INGRESS = 'desktop-clarify-telegram'


class RecordingContext:
    def __init__(self, owner='hermes_validate_probe_plugin'):
        self.plugin_id = owner
        self.commands = []
        self.factories = []

    def register_cli_command(self, **kwargs):
        self.commands.append(kwargs)

    def register_telegram_handler(self, factory):
        self.factories.append(factory)


class CatalogRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = test_support.temporary_directory(prefix='registration-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'fake home'
        self.home.mkdir()
        self.modules = []
        self.addCleanup(self.cleanup_modules)

    def cleanup_modules(self):
        for prefix in self.modules:
            for name in tuple(sys.modules):
                if name == prefix or name.startswith(prefix + '.'):
                    sys.modules.pop(name, None)

    def load(self, directory, suffix):
        name = '_bridge_registration_' + suffix
        self.modules.append(name)
        spec = importlib.util.spec_from_file_location(name, directory / '__init__.py',
                                                       submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def install(self, package, home=None):
        target = (home or self.home) / 'plugins' / package
        source = REPO / ('desktop-plugins' if package == FRONTEND else 'plugins') / package
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return target

    def invoke(self, command):
        parser = argparse.ArgumentParser()
        command['setup_fn'](parser)
        token = base64.urlsafe_b64encode(b'{"action":"unknown-registration-test"}').rstrip(b'=').decode()
        args = parser.parse_args(['--request', token])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            command['handler_fn'](args)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN')
        self.assertEqual(lines[-1], 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_END')
        return json.loads(base64.urlsafe_b64decode(lines[1] + '=' * (-len(lines[1]) % 4)))

    def test_uninstalled_recording_context_declares_both_without_home_or_state_access(self):
        module = self.load(REPO / 'desktop-plugins' / FRONTEND, 'single_primary')
        ctx = RecordingContext(FRONTEND)
        cleared_scope = secret_scope.set_secret_scope(None)
        multiplex = secret_scope.set_multiplex_context(True)
        try:
            with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
                module.register(ctx)
            self.assertEqual([item['name'] for item in ctx.commands], ['desktop-telegram-bridge'])
            self.assertEqual(len(ctx.factories), 1)
            self.assertEqual(list(self.home.iterdir()), [])

            legacy = self.load(REPO / 'plugins' / INGRESS, 'deprecated_legacy')
            old_ctx = RecordingContext(INGRESS)
            with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
                legacy.register(old_ctx)
            self.assertEqual(old_ctx.commands, [])
            self.assertEqual(old_ctx.factories, [])
            self.assertEqual(list(self.home.iterdir()), [])
        finally:
            secret_scope.reset_multiplex_context(multiplex)
            secret_scope.reset_secret_scope(cleared_scope)

    def test_primary_manifest_declares_default_off_independent_native_settings(self):
        import yaml
        manifest = yaml.safe_load((REPO / 'desktop-plugins' / FRONTEND / 'plugin.yaml').read_text(encoding='utf-8'))
        self.assertEqual(manifest['config_schema']['replies_enabled']['type'], 'bool')
        self.assertIs(manifest['config_schema']['replies_enabled']['default'], False)
        self.assertEqual(manifest['config_schema']['approvals_enabled']['type'], 'bool')
        self.assertIs(manifest['config_schema']['approvals_enabled']['default'], False)
        expected = {
            'service_enabled': True,
            'completion_notifications_enabled': True,
            'attention_notifications_enabled': True,
            'clarify_replies_enabled': True,
            'completion_followups_enabled': True,
        }
        for key, default in expected.items():
            self.assertEqual(manifest['config_schema'][key]['type'], 'bool')
            self.assertIs(manifest['config_schema'][key]['default'], default)
        self.assertEqual(manifest['version'], '1.8.1')

    def test_recording_cli_owner_cannot_execute_even_at_valid_location(self):
        module = self.load(self.install(FRONTEND), 'frontend_recording')
        ctx = RecordingContext()
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            module.register(ctx)
            result = self.invoke(ctx.commands[0])
        self.assertEqual(result, {'ok': False, 'reason': 'native_plugin_owner_mismatch'})
        self.assertFalse((self.home / 'plugin-data').exists())

    def test_correct_cli_owner_in_foreign_home_cannot_dispatch(self):
        module = self.load(self.install(FRONTEND), 'frontend_foreign')
        foreign = self.root / 'foreign home'
        foreign.mkdir()
        ctx = RecordingContext(FRONTEND)
        with patch.dict(os.environ, {'HERMES_HOME': str(foreign)}):
            module.register(ctx)
            result = self.invoke(ctx.commands[0])
        self.assertIs(result['ok'], False)
        self.assertFalse((self.home / 'plugin-data').exists())
        self.assertEqual(list(foreign.iterdir()), [])

    def test_primary_factory_refuses_unowned_and_foreign_contexts_before_imports(self):
        target = self.install(FRONTEND)
        module = self.load(target, 'unified_gateway_guard')
        gateway = __import__(module.__name__ + '.telegram_integration', fromlist=['telegram_integration'])
        foreign = self.root / 'foreign home'
        foreign.mkdir()

        wrong = RecordingContext('hermes_validate_probe_plugin')
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            module.register(wrong)
            with patch.object(gateway, 'resolve_package_home', side_effect=AssertionError('owner guard missed')) as resolve:
                wrong.factories[0](object(), object())
                resolve.assert_not_called()

        ctx = RecordingContext(FRONTEND)
        with patch.dict(os.environ, {'HERMES_HOME': str(foreign)}):
            module.register(ctx)
            with patch.object(gateway, 'resolve_package_home', wraps=gateway.resolve_package_home) as resolve:
                ctx.factories[0](object(), object())
                resolve.assert_called_once()
        self.assertFalse((self.home / 'plugin-data').exists())
        self.assertFalse((foreign / 'plugin-data').exists())
        self.assertEqual(list(foreign.iterdir()), [])

    def test_named_profile_factory_and_cli_cannot_run_after_registration(self):
        named = self.home / 'profiles' / 'arbitrary-user'
        named.mkdir(parents=True)
        with patch.dict(os.environ, {'HERMES_HOME': str(named)}):
            module = self.load(self.install(FRONTEND, named), 'named_primary')
            ctx = RecordingContext(FRONTEND)
            module.register(ctx)
            self.assertIs(self.invoke(ctx.commands[0])['ok'], False)
            ctx.factories[0](object(), object())

            legacy = self.load(REPO / 'plugins' / INGRESS, 'named_legacy')
            old_ctx = RecordingContext(INGRESS)
            legacy.register(old_ctx)
            self.assertEqual(old_ctx.factories, [])
        self.assertFalse((named / 'plugin-data').exists())

    def test_native_cli_default_home_envloader_uses_fake_credentials_in_a_child_process(self):
        core = Path(secret_scope.__file__).resolve().parents[1]
        managed = self.root / 'managed'
        managed.mkdir()
        self.env_path = self.home / '.env'
        self.env_path.write_text(
            'TELEGRAM_BOT_TOKEN=765432:subprocess-fixture-token\n'
            'TELEGRAM_HOME_CHANNEL=99:7\n', encoding='utf-8')
        script = '''
import os, sys
from pathlib import Path
core = Path(os.environ["HERMES_CORE_ROOT"]).resolve(strict=True)
sys.path.insert(0, str(core))
sys.path.insert(0, os.environ["BRIDGE_PACKAGE"])
from hermes_cli.env_loader import load_hermes_dotenv
from agent.secret_scope import get_secret
from notifier import credentials
home = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
loaded = load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
assert home / ".env" in loaded
value = credentials(home)
assert value == ("765432:subprocess-fixture-token", 99, 7)
assert get_secret("TELEGRAM_BOT_TOKEN") == value[0]
assert Path(__import__("agent.secret_scope", fromlist=["get_secret"]).__file__).resolve().is_relative_to(core)
print("native_default_home_envloader_ok")
'''
        child_env = os.environ.copy()
        for key in tuple(child_env):
            if key.upper().startswith('TELEGRAM_'):
                child_env.pop(key, None)
        child_env.update({
            'HERMES_HOME': str(self.home),
            'HERMES_CORE_ROOT': str(core),
            'HERMES_MANAGED_DIR': str(managed),
            'BRIDGE_PACKAGE': str(REPO / 'desktop-plugins' / FRONTEND),
            'HERMES_DISABLE_LAZY_INSTALLS': '1',
        })
        result = subprocess.run([sys.executable, '-c', script], cwd=str(self.home),
                                env=child_env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'native_default_home_envloader_ok')

    def native_adapter(self, token, chat='99', topic='7'):
        from types import SimpleNamespace
        from gateway.config import HomeChannel, Platform, PlatformConfig
        config = PlatformConfig(enabled=True, token=token,
                                home_channel=HomeChannel(Platform.TELEGRAM, chat, 'Fixture Home', thread_id=topic))
        runner = SimpleNamespace(config=SimpleNamespace(platforms={Platform.TELEGRAM: config}))
        return SimpleNamespace(platform=Platform.TELEGRAM, config=config, gateway_runner=runner)

    def test_default_multiplex_factory_uses_host_bound_native_transport_without_secret_scope(self):
        """Cold default connect is unscoped; only the host-bound native transport is authority."""
        from telegram.ext import Application

        target = self.install(FRONTEND)
        (self.home / 'config.yaml').write_text(
            'plugins:\n  enabled: [session-complete-telegram]\n  entries:\n'
            '    session-complete-telegram:\n      settings: {}\n', encoding='utf-8')
        token = '765432:native-transport-fixture-token'
        adapter = self.native_adapter(token)
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            module = self.load(target, 'native_cold_connect')
            ctx = RecordingContext(FRONTEND)
            module.register(ctx)
            cleared = secret_scope.set_secret_scope(None)
            multiplex = secret_scope.set_multiplex_context(True)
            try:
                reply = __import__(module.__name__ + '.reply_bridge', fromlist=['reply_bridge'])
                async def exercise():
                    application = Application.builder().token(token).build()
                    try:
                        accepted = ctx.factories[0](application, adapter)
                        self.assertIs(accepted, True)
                        self.assertFalse(application.running)
                    finally:
                        task = application.bot_data.get(reply._WIRING_STATE, {}).get('task')
                        if task is not None:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                asyncio.run(exercise())
            finally:
                secret_scope.reset_multiplex_context(multiplex)
                secret_scope.reset_secret_scope(cleared)

    def test_native_binding_identity_lifetime_policy_and_unscoped_flush(self):
        import time
        from telegram.ext import Application
        token = '765432:native-transport-fixture-token'
        target = self.install(FRONTEND)
        config_file = self.home / 'config.yaml'
        config_file.write_text(
            'plugins:\n  enabled: [session-complete-telegram]\n  entries:\n'
            '    session-complete-telegram:\n      settings: {replies_enabled: true}\n', encoding='utf-8')
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            module = self.load(target, 'native_lifetime')
            ctx = RecordingContext(FRONTEND)
            module.register(ctx)
            gateway = __import__(module.__name__ + '.telegram_integration', fromlist=['telegram_integration'])
            reply = __import__(module.__name__ + '.reply_bridge', fromlist=['reply_bridge'])
            base = __import__(module.__name__ + '.notifier', fromlist=['notifier'])
            cleared = secret_scope.set_secret_scope(None)
            multiplex = secret_scope.set_multiplex_context(True)
            try:
                async def exercise():
                    applications = []
                    def app(tok=token):
                        value = Application.builder().token(tok).build()
                        applications.append(value)
                        return value
                    try:
                        adapter = self.native_adapter(token)
                        foreign = self.native_adapter(token)
                        foreign.gateway_runner = adapter.gateway_runner
                        missing = self.native_adapter(token)
                        missing.gateway_runner = None
                        for label, candidate, native in [
                            ('named-config-in-default-context', foreign, app()),
                            ('missing-owner-provenance', missing, app()),
                            ('native-token-mismatch', self.native_adapter(token), app('111111:wrong-native-token')),
                            ('nonprivate-destination', self.native_adapter(token, '-99'), app()),
                            ('conflicting-topic', self.native_adapter(token, '99:8', '7'), app()),
                            ('malformed-topic', self.native_adapter(token, '99', 'bad'), app()),
                        ]:
                            with self.subTest(boundary=label):
                                self.assertIs(ctx.factories[0](native, candidate), False)
                                self.assertEqual(native.handlers, {})
                        application = app()
                        self.assertIs(ctx.factories[0](application, adapter), True)
                        binding = application.bot_data[gateway._NATIVE_BINDING_KEY]
                        first_task = application.bot_data[reply._WIRING_STATE]['task']
                        first_handlers = sum(map(len, application.handlers.values()))
                        self.assertGreater(first_handlers, 0)
                        self.assertIs(ctx.factories[0](application, adapter), True)
                        self.assertIs(application.bot_data[reply._WIRING_STATE]['task'], first_task)
                        self.assertEqual(sum(map(len, application.handlers.values())), first_handlers)
                        with self.assertRaises(secret_scope.UnscopedSecretError):
                            base.credentials()
                        self.assertEqual(await asyncio.to_thread(binding.credentials), (token, 99, 7))
                        # Exercise the REAL deferred flush, including its credential read,
                        # SQLite commit and exactly one mocked Telegram request.
                        now = time.time()
                        c = reply.connect(reply.STATE)
                        with c:
                            c.execute('''INSERT INTO forms
                                (id,identity,owner,descriptor,params,questions,bot,boot,chat,topic,
                                 created,expires,seen,status,mids,texts,dirty)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                ('native-fixture','native-fixture-identity','fixture-owner','{}','{}',
                                 '[{"qid":"q1","question":"fixture","choices":[],"multi_select":false}]',
                                 765432,'fixture-boot',99,7,now,now+3600,now,'expired',
                                 '{"0":123}','{"0":"fixture"}',1))
                        c.close()
                        with patch.object(base, 'telegram', return_value={'ok': True}) as send:
                            self.assertFalse(binding.telegram(token, 'editMessageText', {'chat_id':100})['ok'])
                            self.assertEqual(send.call_count, 0)
                            await asyncio.to_thread(reply.flush, binding)
                            self.assertEqual(send.call_count, 1)
                            self.assertEqual(send.call_args.args[1], 'editMessageText')
                            self.assertEqual(send.call_args.args[2]['chat_id'], 99)
                        c = reply.connect(reply.STATE)
                        self.assertEqual(c.execute("SELECT dirty FROM forms WHERE id='native-fixture'").fetchone()[0], 0)
                        c.close()
                        # Replacing even an application that NEVER started retires
                        # only its owned watcher and rejects stale-client reuse.
                        replacement = app()
                        self.assertIs(ctx.factories[0](replacement, adapter), True)
                        await asyncio.gather(first_task, return_exceptions=True)
                        self.assertTrue(first_task.done())
                        self.assertFalse(binding.binding_valid())
                        self.assertIs(ctx.factories[0](application, adapter), False)
                        newer = replacement.bot_data[gateway._NATIVE_BINDING_KEY]
                        self.assertTrue(newer.binding_valid())
                        adapter.config.home_channel.chat_id = '100'
                        self.assertFalse(newer.binding_valid())
                        with self.assertRaises(RuntimeError):newer.credentials()
                        self.assertIs(ctx.factories[0](replacement, adapter), False)
                        never_started = app()
                        self.assertIs(ctx.factories[0](never_started, self.native_adapter(token)), True)
                        idle_binding = never_started.bot_data[gateway._NATIVE_BINDING_KEY]
                        idle_binding._startup_deadline = time.monotonic() - 1
                        idle_task = never_started.bot_data[reply._WIRING_STATE]['task']
                        await asyncio.wait_for(asyncio.shield(idle_task), timeout=2)
                        self.assertTrue(idle_task.done())
                        self.assertFalse(never_started.bot_data[reply._WIRING_STATE]['ready'])
                        for field in ('token', 'topic', 'enabled'):
                            changed_adapter = self.native_adapter(token)
                            changed_app = app()
                            self.assertIs(ctx.factories[0](changed_app, changed_adapter), True)
                            changed_binding = changed_app.bot_data[gateway._NATIVE_BINDING_KEY]
                            if field == 'token':changed_adapter.config.token = '111111:rotated-token'
                            elif field == 'topic':changed_adapter.config.home_channel.thread_id = '8'
                            else:changed_adapter.config.enabled = False
                            with self.subTest(changed=field):
                                self.assertFalse(changed_binding.binding_valid())
                                changed_adapter.config.token = token
                                changed_adapter.config.home_channel.thread_id = '7'
                                changed_adapter.config.enabled = True
                                self.assertFalse(changed_binding.binding_valid(), 'a retired binding must not revive')
                        # Host-equivalent split and inline topics agree, but an
                        # installed foreign secret scope is never overridden.
                        inline_adapter = self.native_adapter(token, '99:7', None)
                        inline = app()
                        self.assertIs(ctx.factories[0](inline, inline_adapter), True)
                        live = inline.bot_data[gateway._NATIVE_BINDING_KEY]
                        self.assertEqual(live.credentials(), (token,99,7))
                        scoped = secret_scope.set_secret_scope({'TELEGRAM_BOT_TOKEN':'111111:foreign-token',
                                                               'TELEGRAM_HOME_CHANNEL':'99:7'}, profile_home=str(self.home))
                        try:
                            self.assertFalse(live.binding_valid())
                            self.assertIs(ctx.factories[0](app(), self.native_adapter(token)), False)
                        finally:secret_scope.reset_secret_scope(scoped)
                        self.assertTrue(live.action_enabled('clarify_replies_enabled'))
                        config_file.write_text(
                            'plugins:\n  enabled: [session-complete-telegram]\n  entries:\n'
                            '    session-complete-telegram:\n      settings: {service_enabled: false, replies_enabled: true}\n',
                            encoding='utf-8')
                        self.assertFalse(live.action_enabled('clarify_replies_enabled'))
                    finally:
                        tasks = [value.bot_data.get(reply._WIRING_STATE, {}).get('task') for value in applications]
                        tasks = [task for task in tasks if task is not None]
                        for task in tasks:task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                asyncio.run(exercise())
            finally:
                secret_scope.reset_multiplex_context(multiplex)
                secret_scope.reset_secret_scope(cleared)

    def test_default_gateway_factory_matches_the_current_scoped_bot_token(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from telegram.ext import Application

        target = self.install(FRONTEND)
        (self.home / 'config.yaml').write_text(
            'plugins:\n  enabled: [session-complete-telegram]\n  entries:\n'
            '    session-complete-telegram:\n      settings: {}\n', encoding='utf-8')
        (self.home / '.env').write_text(
            'TELEGRAM_BOT_TOKEN=111111:env-only-token\n'
            'TELEGRAM_HOME_CHANNEL=88:8\n', encoding='utf-8')
        module_name = 'actual_gateway_factory_scope'
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            module = self.load(target, module_name)
            context = RecordingContext(FRONTEND)
            module.register(context)
            self.assertEqual(len(context.factories), 1)
            scope = set_secret_scope({'TELEGRAM_BOT_TOKEN': '765432:scoped-token',
                                      'TELEGRAM_HOME_CHANNEL': '99:7'}, profile_home=str(self.home))
            try:
                import asyncio
                reply_bridge = __import__('_bridge_registration_' + module_name + '.reply_bridge', fromlist=['reply_bridge'])
                async def exercise():
                    mismatch = Application.builder().token('111111:env-only-token').build()
                    adapter = self.native_adapter('765432:scoped-token')
                    refused = context.factories[0](mismatch, adapter)
                    self.assertIs(refused, False)
                    matching = Application.builder().token('765432:scoped-token').build()
                    accepted = context.factories[0](matching, adapter)
                    self.assertIs(accepted, True)
                    self.assertFalse(matching.running)
                    task = matching.bot_data[reply_bridge._WIRING_STATE].get('task')
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                asyncio.run(exercise())
            finally:
                reset_secret_scope(scope)


if __name__ == '__main__':
    unittest.main(verbosity=2)
