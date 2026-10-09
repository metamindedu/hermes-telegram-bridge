"""Opt-in default-profile ingress; never owns Telegram polling."""
from pathlib import Path
import importlib.util
import sys


def register(ctx):
    # Global Desktop plugin owns this module; the native plugin is just ingress.
    root = Path(__file__).resolve().parents[2]
    directory = root / 'desktop-plugins' / 'session-complete-telegram'
    def telegram_factory(application, adapter):
        # The factory is called on the gateway loop with its existing PTB client.
        modules = {}
        for name in ('notifier', 'reply_bridge', 'followup_bridge'):
            path = directory / (name + '.py')
            spec = importlib.util.spec_from_file_location('_desktop_clarify_' + name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            modules[name] = module
        modules['reply_bridge']._NOTIFIER = modules['notifier']
        modules['reply_bridge'].wire(application, adapter, modules['followup_bridge'])
    ctx.register_telegram_handler(telegram_factory)
