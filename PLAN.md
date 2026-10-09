# Native packages and reproducible verification — Phase 2

## Scope

Implement and verify two native Hermes directory packages; finish the authorized development-branch Git delivery and commit-bound CI. Only task-owned fake homes may be used for install/update/rollback and temporary enablement tests. No production installation, profile/config/secrets/data write, live Gateway change or Telegram message is authorized. Independent Heavy acceptance remains required before a release claim.

## Package boundaries and preserved contracts

- `desktop-plugins/session-complete-telegram/` is the native package `session-complete-telegram`; its canonical Desktop entry is `desktop/plugin.js`. Its Python `__init__.py` registers only fixed CLI command `desktop-telegram-bridge`.
- `plugins/desktop-clarify-telegram/` remains native ingress ID `desktop-clarify-telegram`; it registers only the existing Telegram factory on the existing `Application`.
- Ingress resolves the frontend package from the same verified default home through an owner/home-validated, per-home namespaced loader. There are no global bridge aliases or broad `sys.path` edits.
- Completion notifications and ingress are independently selectable. Ingress does not poll and disabling it does not disable the ordinary completion notification.
- Preserve IDs `session-complete-telegram` and `desktop-clarify-telegram`, `plugin-data/session-complete-telegram`, Telegram authentication, TTL/lease/claim/no-replay behavior, and existing schemas. No data migration or automatic enable/config write is in scope.
- Named/foreign homes fail closed. All-profile observation uses only the verified default local route for helper execution.

## Native CLI transport

- The Desktop sends fixed `argv`: `--profile default desktop-telegram-bridge --request <bounded-base64url-json>` through `cli.exec` with numeric inner and outer deadlines. The original Windows argv envelope is preserved at UTF-8 JSON ≤22,500 bytes / base64url token ≤30,000 characters; permanent local encoding/size failures are not truncated or launched and produce one user notice per ask fingerprint.
- No renderer-provided executable, path, raw command, or environment is accepted.
- The native handler calls the existing notifier action dispatcher; it does not implement a second action layer.
- Results use one bounded strict frame: UTF-8 JSON ≤24 KiB serialized with `ensure_ascii=False` inside the existing ASCII base64 frame, within the 48,000-character stdout ceiling. Startup logging may surround it; duplicate/malformed frames fail closed. Native `reply_poll` budgets the entire response before claiming; oversized frozen descriptor/result data uses bounded SHA-256-verified read-only chunks without a schema change or claim/TTL renewal. Unselected forms remain queued. Ambiguous writes are not retried and credential-bearing exception text is not returned.

## Verification and CI

- Preserve the existing Node completion/reply/followup and Python bridge suites; run `test_notifier` with real pinned-core SessionDB and redactor, never a stub/skip.
- Native tests use the real Hermes install API, PluginContext, and Plugin Doctor. Only local Git transport is mocked; fresh/update/rollback operate on fake homes and assert stable IDs plus sentinel config/plugin-data preservation.
- `requirements-test.txt` explicitly declares bridge-only test dependencies. CI uses contents-read-only permissions, verified immutable action SHAs, and public Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` with its locked dependency environment, owned by the job.
- Windows and Ubuntu run the same source tests. This is not cross-PC Desktop GUI/materialization or production activation evidence.
- `docs/source-manifest.json` remains the historic Phase 1 installed-source manifest; it is not regenerated as a current-file manifest.

## Release / activation boundary

See `docs/operations.md` for the exact two subdirectory install, pinned update and rollback commands. Production use remains separately authorized. The same pinned commands are exercised only in task-owned fake homes during this phase. Production plugin enablement, Desktop UI enablement, Gateway wiring/restart, production readback and live Telegram checks require separate approval after Heavy review.
