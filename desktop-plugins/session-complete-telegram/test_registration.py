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
                    refused = context.factories[0](mismatch, object())
                    self.assertIs(refused, False)
                    matching = Application.builder().token('765432:scoped-token').build()
                    accepted = context.factories[0](matching, object())
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
