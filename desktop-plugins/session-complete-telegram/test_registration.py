"""Registration is metadata; only invocation grants checked runtime access."""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import test_support

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
