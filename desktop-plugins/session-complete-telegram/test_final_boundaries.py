"""Actual regressions for managed fail-open and known-card consumption edges."""
import asyncio
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from test_light_corrections import LightCorrectionFixture, TOKEN, CHAT

class FinalBoundaryTests(LightCorrectionFixture):
    def test_real_managed_parse_failure_does_not_restore_raw_consent(self):
        self.write_config(replies=True, approvals=True)
        self.write_managed(False, feature='approvals_enabled')
        self.assertFalse(self.gateway.feature_enabled('approvals_enabled', root=self.home, ctx=self.ctx))
        (self.managed/'config.yaml').write_text('plugins: [', encoding='utf-8')
        self.assertFalse(self.gateway.feature_enabled('approvals_enabled', root=self.home, ctx=self.ctx))
        self.assertFalse(self.gateway.feature_enabled('replies_enabled', root=self.home, ctx=self.ctx))

    def test_managed_existing_nonmapping_and_read_failure_deny_then_recover(self):
        self.write_config(replies=True, approvals=True)
        path = self.managed / 'config.yaml'
        for content in ('plugins: [', '- invalid root', 'false', 'null'):
            path.write_text(content, encoding='utf-8')
            for feature in ('replies_enabled', 'approvals_enabled'):
                self.assertFalse(self.gateway.feature_enabled(feature, root=self.home, ctx=self.ctx))
        path.write_text('{}\n', encoding='utf-8')
        read = Path.read_text
        def denied_read(target, *args, **kwargs):
            if target == path:
                raise PermissionError('synthetic managed read refusal')
            return read(target, *args, **kwargs)
        with patch.object(Path, 'read_text', new=denied_read):
            self.assertFalse(self.gateway.feature_enabled('approvals_enabled', root=self.home, ctx=self.ctx))
        self.assertTrue(self.gateway.feature_enabled('approvals_enabled', root=self.home, ctx=self.ctx))

    def test_native_dispatcher_one_shot_quarantine_failure_never_falls_through(self):
        async def scenario():
            from telegram.ext import MessageHandler, filters
            from gateway.platforms.base import BasePlatformAdapter
            app, Update = await self.make_application()
            baseline=set(asyncio.all_tasks());later=[];failed=[False];original=type(app).add_handler
            adapter=SimpleNamespace(platform=SimpleNamespace(value='telegram'),name='fixture',_plugin_handler_native=None,_plugin_handlers_wired=None)
            self.add_approval_tombstone();self.add_clarify_tombstone()
            def add_once(target,handler,group=0):
                if target is app and group==-10000 and not failed[0]:
                    failed[0]=True
                    raise RuntimeError('quarantine-before-install')
                return original(target,handler,group=group)
            async def downstream(update,ctx):later.append(update.update_id)
            try:
                with patch.object(self.notifier,'credentials',return_value=(TOKEN,CHAT,None)),patch.object(self.notifier,'telegram',return_value={'ok':False}),patch('hermes_cli.plugins.get_plugin_manager',return_value=self.manager):
                    with patch.object(type(app),'add_handler',new=add_once):
                        BasePlatformAdapter._wire_plugin_handlers(adapter,app)
                    # Host dedups both failed and returning-False factories; no retry is available here.
                    BasePlatformAdapter._wire_plugin_handlers(adapter,app)
                    app.add_handler(MessageHandler(filters.ALL,downstream),group=0)
                    app.add_handler(MessageHandler(filters.ALL,downstream),group=1)
                    await app.process_update(self.approval_text_update(Update,app,100,880))
                    await app.process_update(self.clarify_text_update(Update,app,101,881))
                self.assertTrue(failed[0]);self.assertEqual(later,[])
            finally:
                tasks=set(asyncio.all_tasks())-baseline
                for t in tasks:t.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                await app.shutdown()
        asyncio.run(scenario())

    def test_native_telegram_add_post_registration_error_retains_quarantine(self):
        async def scenario():
            from telegram.ext import Application, ExtBot, MessageHandler, filters
            from plugins.platforms.telegram.update_admission import TelegramApplication
            # Use the real native add_handler (super registration -> admission wrapping).
            old_app, Update = await self.make_application()
            bot = old_app.bot
            adapter = SimpleNamespace(_update_admission=None)
            app = Application.builder().bot(bot).application_class(TelegramApplication, {'adapter': adapter}).build()
            await app.initialize()
            baseline = set(asyncio.all_tasks()); failed = [False]; later = []
            original = TelegramApplication._admit_callbacks
            def fail_after_add(target, handler):
                if target is app and not failed[0] and getattr(handler.callback, '__name__', '') == 'quarantine_known_reply':
                    failed[0] = True
                    raise RuntimeError('native admission wrapping after registration')
                return original(target, handler)
            async def downstream(update, ctx): later.append(update.update_id)
            self.add_approval_tombstone()
            try:
                with patch.object(self.notifier, 'credentials', return_value=(TOKEN, CHAT, None)), patch.object(self.notifier, 'telegram', return_value={'ok': False}):
                    with patch.object(TelegramApplication, '_admit_callbacks', new=fail_after_add):
                        self.factory(app, object())
                    app.add_handler(MessageHandler(filters.ALL, downstream), group=1)
                    # Base dispatch exercises the real installed native consumers
                    # without replacing the native wrapper or needing disk receipts.
                    await Application.process_update(app, self.approval_text_update(Update, app, 110, 890))
                    self.factory(app, object())
                    self.assertEqual(len(app.handlers[-10000]), 1)
                self.assertTrue(failed[0]); self.assertEqual(later, [])
            finally:
                tasks=set(asyncio.all_tasks())-baseline
                for t in tasks:t.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                await app.shutdown()
                await old_app.shutdown()
        asyncio.run(scenario())

    def test_managed_corruption_before_factory_keeps_known_card_boundary(self):
        async def scenario():
            from gateway.platforms.base import BasePlatformAdapter
            from telegram.ext import MessageHandler, filters
            app,Update=await self.make_application();baseline=set(asyncio.all_tasks());later=[]
            self.add_approval_tombstone();self.add_clarify_tombstone()
            (self.managed/'config.yaml').write_text('plugins: [',encoding='utf-8')
            adapter=SimpleNamespace(platform=SimpleNamespace(value='telegram'),name='fixture',_plugin_handler_native=None,_plugin_handlers_wired=None)
            async def downstream(update,ctx):later.append(update.update_id)
            try:
                with patch.object(self.notifier,'credentials',return_value=(TOKEN,CHAT,None)),patch.object(self.notifier,'telegram',return_value={'ok':False}),patch('hermes_cli.plugins.get_plugin_manager',return_value=self.manager):
                    BasePlatformAdapter._wire_plugin_handlers(adapter,app)
                    app.add_handler(MessageHandler(filters.ALL,downstream),group=0)
                    app.add_handler(MessageHandler(filters.ALL,downstream),group=1)
                    await app.process_update(self.approval_text_update(Update,app,120,900))
                    await app.process_update(self.clarify_text_update(Update,app,121,901))
                self.assertEqual(later,[])
                self.assertFalse(self.approval.receiver_info(state=self.state))
            finally:
                tasks=set(asyncio.all_tasks())-baseline
                for t in tasks:t.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                await app.shutdown()
        asyncio.run(scenario())

    def test_two_unattached_quarantine_failures_are_a_native_sdk_blocker(self):
        async def scenario():
            from gateway.platforms.base import BasePlatformAdapter
            from telegram.ext import MessageHandler,filters
            app,Update=await self.make_application();baseline=set(asyncio.all_tasks());later=[];attempts=[]
            original=type(app).add_handler
            self.add_approval_tombstone()
            adapter=SimpleNamespace(platform=SimpleNamespace(value='telegram'),name='fixture',_plugin_handler_native=None,_plugin_handlers_wired=None)
            def fail_guard(target,handler,group=0):
                if target is app and group==-10000:
                    attempts.append(1);raise RuntimeError('persistent attach refusal')
                return original(target,handler,group=group)
            async def downstream(update,ctx):later.append(update.update_id)
            try:
                with patch.object(self.notifier,'credentials',return_value=(TOKEN,CHAT,None)),patch.object(self.notifier,'telegram',return_value={'ok':False}),patch('hermes_cli.plugins.get_plugin_manager',return_value=self.manager):
                    with patch.object(type(app),'add_handler',new=fail_guard):BasePlatformAdapter._wire_plugin_handlers(adapter,app)
                    BasePlatformAdapter._wire_plugin_handlers(adapter,app)
                    app.add_handler(MessageHandler(filters.ALL,downstream),group=0)
                    await app.process_update(self.approval_text_update(Update,app,122,902))
                self.assertEqual(len(attempts),2)
                self.assertEqual(later,[], 'required consumption cannot be provided by current isolated factory SDK')
            finally:
                tasks=set(asyncio.all_tasks())-baseline
                for t in tasks:t.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                await app.shutdown()
        asyncio.run(scenario())

    def test_failed_clarify_rejection_notice_still_stops_other_groups(self):
        async def scenario():
            from telegram.ext import TypeHandler
            from telegram.error import TelegramError
            app,Update=await self.make_application();baseline=set(asyncio.all_tasks());later=[]
            self.add_clarify_tombstone()
            async def downstream(update,ctx):later.append(update.update_id)
            try:
                with patch.object(self.notifier,'credentials',return_value=(TOKEN,CHAT,None)),patch.object(self.notifier,'telegram',return_value={'ok':False}):
                    self.factory(app,object())
                    self.write_config(replies=False,approvals=False)
                    app.add_handler(TypeHandler(Update,downstream),group=1)
                    update=self.clarify_text_update(Update,app,102,882)
                    async def fail_notice(*a,**kw):raise TelegramError('synthetic notice failure')
                    with patch.object(type(update.message),'reply_text',new=fail_notice):
                        await app.process_update(update)
                self.assertEqual(later,[])
            finally:
                tasks=set(asyncio.all_tasks())-baseline
                for t in tasks:t.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                await app.shutdown()
        asyncio.run(scenario())
