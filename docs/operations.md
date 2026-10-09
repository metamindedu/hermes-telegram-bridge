# Phase 2 source verification, native install, and activation boundary

## Ownership and current state

The repository is the source of truth. The official native packages are the two existing directories installed from the same immutable Git commit:

- `metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram` → package/ID `session-complete-telegram`.
- `metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram` → package/ID `desktop-clarify-telegram`.

Hermes resolves `owner/repo/subdir` through its native installer. The first package includes both the Python CLI registration and `desktop/plugin.js`; the Desktop shell materializes that JS half by its existing package mechanism. The second package registers one factory on the existing Telegram `Application`. The Gateway remains the only Telegram update/polling owner.

Phase 2 authorizes source/tests/docs/CI, isolated fake-home installation checks and development-branch commit/push/PR delivery. It does not authorize production installation/config/credentials/data changes, Gateway/Desktop activation or live Telegram sends. Independent Heavy review, exact remote SHA, CI and actual private pinned install must be verified before completion; task-owned fixture writes are not production activation.

## Required pre-install candidate checks (future separately authorized release)

1. Pin both subdirectory installs to the same reviewed 40-character commit SHA. Verify the SHA exists on the intended repository/ref before invoking Hermes.
2. Preserve the exact package IDs and global state root `plugin-data/session-complete-telegram`. Do not copy or reset its SQLite files; there is no schema/data migration.
3. Run the pinned-core CI commands and native Plugin Doctor on both package directories. Install/Doctor tests use isolated fake homes; their local Git transport mock is not evidence that the private remote install has run.
4. Confirm install commands leave `plugins.enabled` unchanged. The frontend/CLI, ingress and Desktop renderer have independent enablement surfaces; do not infer one from another.

## Exact install, update, and rollback commands

Replace each variable with a previously verified full SHA. These commands are documented only and were **not executed** in this task.

Fresh install into the default/global home without enabling either agent plugin:

```bash
CANDIDATE_SHA=<verified-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

Update both existing package directories to one new candidate while preserving their enablement selection:

```bash
NEW_SHA=<verified-new-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
```

Rollback both packages to the previously reviewed commit through the same native installer transaction:

```bash
PREVIOUS_SHA=<verified-previous-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
```

Package-owned Python dependencies are declared explicitly in plugin.yaml (markdown-it-py, PyYAML and python-telegram-bot for the completion package; python-telegram-bot for ingress). Hermes PM owns installation and dependency consent; --yes-deps is explicit consent for the reviewed declarations, not permission for another candidate. --no-enable --yes-deps preserves selection instead of activating the packages. Do not use --no-deps or disabled dependency checks for an actual production rollout.

After a separately approved install, native verification uses the normal Doctor contract:

```bash
hermes --profile default plugins doctor session-complete-telegram --ci
hermes --profile default plugins doctor desktop-clarify-telegram --ci
```

## Separate activation gate (not performed)

### Existing manual Desktop entry migration

The native reconciler will not overwrite an existing marker-less `desktop-plugins/session-complete-telegram/plugin.js` when it differs from the new native package entry. A successful Python package install therefore does NOT prove the running Desktop half was upgraded.

For a separately approved transition from Phase 1: first install the two matching candidates and verify the fixed native CLI registration/enablement; compare and preserve any unrelated operator edits in the old Desktop folder. Then align ONLY its `plugin.js` with the reviewed native package's `desktop/plugin.js` and read back byte equality. The supported reconciler can adopt that identical entry and write its own marker, preserving other files. Do not forge the marker, delete the whole legacy folder or copy/reset plugin-data. Verify the actual loaded entry and Desktop toggle before any approved Gateway handler restart. First-phase code restoration uses its Git snapshot and the legacy layout, NOT the two-package installer commands; native package rollback is only to another previously verified packaged commit.

This migration is tested only with synthetic entries in a task-owned fake app root using the actual pinned-core reconciler. It has not been applied to the current Desktop instance.

Installing a package is not activation. Python command registration/ingress enablement, Desktop renderer enablement, Desktop hot reload, and Gateway handler loading are distinct. Any production enablement or restart needs a separate approval and readback. Ingress disablement must leave the completion package and ordinary Desktop completion notifications independently enabled. No plugin-enable, profile/config write, Desktop hot reload, Gateway restart, or live Telegram round trip was done here.

## Verification

- Node: `node --check desktop/plugin.js` and `node --test plugin.test.mjs reply.test.mjs followup.test.mjs materialization.test.mjs`.
- Python: `python -m unittest test_notifier test_reply_bridge test_followup_bridge test_native_install -v`; this runs every `test_notifier` case with the real pinned Hermes `SessionDB` and redactor, plus the existing reply/followup bridge suites and native install fixtures.
- CI: `.github/workflows/tests.yml` runs those exact suites on Windows and Ubuntu, with `contents: read`, pinned action SHAs, Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b`, and the core `uv.lock` inside a job-owned environment. It does not deploy, restart, upload secrets, or claim GUI verification on another PC.
- Fake-home native install tests use only a mock for Git transport. They exercise Hermes' actual install/publication/manifest/Doctor/context contracts and verify sentinels/IDs across fresh install, update and rollback. No live Telegram calls occur.

## State and rollback

The only rollback target is the exact previously reviewed package commit, installed through the same native Hermes command. Existing `plugin-data/session-complete-telegram` and configuration are retained; no database copy, reset, migration or ad-hoc backup is part of this phase. `docs/source-manifest.json` remains an explicitly historical Phase 1 snapshot.
