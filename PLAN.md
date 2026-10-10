# Unified native Telegram package — Phase 3 candidate

## Scope and ownership

The user's approval of **one installation package** authorizes this source/test/documentation candidate only. `session-complete-telegram` remains the stable native package and Desktop ID. Main/Heavy owns the plan, independent final review, release approval, and catalog/PR updates. This Light task does not commit, push, publish, deploy, modify a live Hermes home, change user config, or send Telegram messages.

Only task-owned fake homes may be used for Hermes Package Manager install/Doctor/runtime tests. Windows PM fixture calls must isolate `LOCALAPPDATA` and `HERMES_HOME` and prove the dependency root, default home, and complete dependency-home set are fixture-owned before invoking PM. Preserve resolver/CAS/scanner/lease protections and classify bootstrap/dependency failures separately from product behavior.

## Unified package contract

- `desktop-plugins/session-complete-telegram/` installs as `session-complete-telegram`; its Desktop entry remains `desktop/plugin.js`, and native CLI remains `desktop-telegram-bridge`.
- One `register(ctx)` declares both the fixed CLI command and one Telegram handler factory with the public Hermes SDK. Declaration remains pure for RecordingContext/admission. Runtime factory authority comes from captured `ctx.plugin_id`, physical package location, and `runtime.resolve_package_home`; imports stay package-relative without a second poller, broad `sys.path`, global module aliases, or core overrides.
- Gateway wiring is limited to the existing configured Telegram bot/private home and the default installed-owner path. Handlers remain present to consume recognized stale/tombstoned cards even when optional actions are off. Existing profile, owner, bot/chat/topic/message, route/session/request/queue, TTL, lease, claim, no-replay and read-only validity checks remain in force.
- `plugins/desktop-clarify-telegram/` remains only as a clearly deprecated inert/no-factory compatibility stub. It is not a required or advertised install package. While that legacy ID remains effectively enabled, the unified factory and helper readiness return `legacy_package_migration_required`; no config cleanup, state deletion/copy, schema migration, or consent inheritance is automatic.
- Native settings live only at `plugins.entries.session-complete-telegram.settings`: `replies_enabled` (clarify + ordinary completion followups) and `approvals_enabled` (Allow Once/Deny), each declared as boolean with default `false`. The two gates are independent and never suppress outbound notification actions. Raw root/plugins/enabled/entries/settings types, unknown flags, and wrong-typed values fail closed. Reads use Hermes `PluginContext.get_config` at factory admission plus strict raw YAML validation and native `plugins.enabled`/`plugins.disabled` semantics; the plugin never writes user config.
- Package/helper/renderer/approval handshake version is **1.7.0** / `desktop-command-approval-v3`. v2/v3 mixed and cached combinations deny approval capability. Preserve all v2 approval authority checks, command-free fallback notices, exact original message/private owner/route/session/request/queue, `all:false`, no session/permanent grant, and ambiguous-write no-replay behavior.
- Minimum Hermes remains `>=0.21.5`; exact unrelated dependency pins remain unchanged.

## Migration and lifecycle

Historical public v1.6.1 at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` used two packages and is already released. The v1.7.0 source is an unreleased candidate. Migration is explicit: use Hermes CLI to disable the legacy ID, enable the primary package and set only desired optional switches, then restart the existing Gateway normally. An already-wired factory is not hot-reloaded by file replacement; callback/helper gates are re-read for each operation. A disabled package requires normal plugin loading/restart to install its factory. Rollback to historical 1.6.1 restores the old two-package runtime by its historical pin; keep config and all ledgers/tombstones intact. No automatic migration or guaranteed newer-core compatibility is claimed.

## Verification acceptance

- Start with meaningful failing registration/default-off gate regressions and keep private RED/GREEN receipts outside package directories.
- Preserve registration owner/location/home guards; real native PluginContext must declare one CLI and one factory, while pure RecordingContext performs no config/credential/state access. Legacy-enabled must prevent unified wiring and helper readiness.
- Exercise real PTB `Application.process_update` callback/text delivery, independent default-off/enabled/revoked flags, malformed YAML, known stale-card consumption, duplicate same-Application wiring, old receiver/helper/renderer skew, and synthetic native old-two-package → one-package → old rollback with config sentinel, package IDs, ledgers and tombstones preserved.
- Preserve resolver/CAS/scanner/lease and all notifier/reply/followup/approval/frame/claim/security regressions. Run the exact Python modules `test_notifier test_reply_bridge test_followup_bridge test_approval_bridge test_native_install test_registration test_feature_gates` and all five existing Node suites; no skips or core stubs.
- Existing `docs/source-manifest.json` is a historical Phase 1 snapshot and is not regenerated as a current manifest.

## Release boundary

The candidate SHA, commit-bound CI, Heavy GO, catalog disposition, public release, real installation, production config, Desktop/Gateway activation, restart, and Telegram round trip are not established by this task. Do not claim them. `docs/operations.md` and `docs/public-install.md` describe the one-package candidate and its separate activation/migration/rollback boundaries.
