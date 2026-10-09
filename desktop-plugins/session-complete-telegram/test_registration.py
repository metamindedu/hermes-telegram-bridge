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
        for package, parent in [(FRONTEND, 'desktop-plugins'), (INGRESS, 'plugins')]:
            module = self.load(REPO / parent / package, package.replace('-', '_'))
            ctx = RecordingContext()
            with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
                module.register(ctx)
            self.assertEqual(len(ctx.commands), int(package == FRONTEND))
            self.assertEqual(len(ctx.factories), int(package == INGRESS))
            self.assertEqual(list(self.home.iterdir()), [])

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

    def test_recording_and_foreign_ingress_factory_refuse_before_sibling_load(self):
        module = self.load(self.install(INGRESS), 'ingress_foreign')
        foreign = self.root / 'foreign home'
        foreign.mkdir()
        for owner, active, reason in [('hermes_validate_probe_plugin', self.home, 'native_plugin_owner_mismatch'),
                                      (INGRESS, foreign, 'foreign_hermes_home_refused')]:
            with self.subTest(owner=owner), patch.dict(os.environ, {'HERMES_HOME': str(active)}):
                ctx = RecordingContext(owner)
                module.register(ctx)
                with patch.object(module, '_sibling_package', side_effect=AssertionError('unauthorized sibling access')) as load:
                    with self.assertRaisesRegex(RuntimeError, reason):
                        ctx.factories[0](object(), object())
                    load.assert_not_called()
        self.assertFalse((self.home / 'plugin-data').exists())
        self.assertEqual(list(foreign.iterdir()), [])

    def test_named_profile_factory_and_cli_cannot_run_after_registration(self):
        named = self.home / 'profiles' / 'arbitrary-user'
        named.mkdir(parents=True)
        with patch.dict(os.environ, {'HERMES_HOME': str(named)}):
            for package in [FRONTEND, INGRESS]:
                module = self.load(self.install(package, named), 'named_' + package.replace('-', '_'))
                ctx = RecordingContext(package)
                module.register(ctx)
                if package == FRONTEND:
                    self.assertIs(self.invoke(ctx.commands[0])['ok'], False)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'named_profile_home_refused'):
                        ctx.factories[0](object(), object())
        self.assertFalse((named / 'plugin-data').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
