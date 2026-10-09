# Hermes Telegram Bridge

뉴메타W의 Hermes Desktop 완료 알림·질문 응답·완료 카드 후속 답장 구성요소를 함께 보존하는 비공개 소스 저장소다.

## Phase 2 현재 상태

저장소 브랜치 `feat/reproducible-install`은 native directory package, home 검증, 고정 Hermes CLI 호출, 테스트와 CI를 개발·검증하는 후보이다. 승인 범위는 소스 수정, 임시 home의 설치/업데이트/복구 검증과 Git 원격 전달이다. 운영 설치본/Hermes core/profile/config/credential/DB 및 운영 Gateway에는 반영하지 않는다. 독립 검수·원격 CI·실제 private pinned 설치 결과는 완료 보고와 해당 GitHub PR에서 확인하며, 개발 문서 자체가 운영 활성화의 증거는 아니다.

## 두 공식 package

하나의 저장소 안에 기존 구성요소 디렉터리를 native install subdirectory로 제공한다.

| Package directory | Native plugin ID / 역할 |
|---|---|
| `desktop-plugins/session-complete-telegram/` | `session-complete-telegram`; Hermes Desktop `desktop/plugin.js` half와 고정 `desktop-telegram-bridge` CLI command 등록 |
| `plugins/desktop-clarify-telegram/` | `desktop-clarify-telegram`; 기존 Telegram Gateway `Application`에 factory 하나만 등록 |

두 설치본은 global/default Hermes home의 `plugins/`에 놓인다. Desktop 앱은 통합 package의 `desktop/plugin.js`를 기존 방식으로 앱 단위 `desktop-plugins/session-complete-telegram/plugin.js`에 materialize한다. Desktop entry ID `session-complete-telegram`, Gateway ingress ID `desktop-clarify-telegram`, 상태 저장 위치 `plugin-data/session-complete-telegram`은 그대로다. ingress는 Telegram update polling을 추가하지 않는다. ingress를 disable해도 Desktop completion notification은 별도 경로로 유지된다.

모든 profile의 로컬 완료 상태 관측은 그대로 유지되며 helper/Telegram 발송은 오직 검증된 default local route에서 실행한다. Foreign/named Hermes home은 fail closed한다. `--profile default`는 renderer가 고정 전달하고, renderer는 실행 파일·경로·raw command·env를 보내지 않는다. Request는 원래 CLI argv 범위(UTF-8 JSON 22,500 bytes / base64url token 30,000 characters)를 보존하며, 로컬 encoding/size failure는 자르거나 실행하지 않고 같은 ask fingerprint의 재시도를 멈춘 뒤 사용자 안내를 표시한다. Native result는 UTF-8 JSON 24 KiB 이내로 직렬화해 기존 ASCII base64 frame에 담고, frame/log를 포함한 stdout 48,000 characters 제한을 지킨다. Native reply poll은 전체 응답 projection이 맞을 때만 claim하며, 큰 frozen descriptor/result는 bounded read-only SHA-256 fragment로 전달한다. Fragment read는 claim/TTL을 갱신하지 않고, 선택되지 않은 답변은 queued 상태로 남는다. Strict single-frame 검증을 유지하며 ambiguous write는 재시도하지 않는다.

기존 completion gate, 인증, TTL, 45초 lease, one-shot claim, durable dedup/tombstone 및 ambiguous-outcome no-replay 계약을 보존한다. 상태 schema 변경과 data migration은 없다. `docs/source-manifest.json`은 **Phase 1의 최초 설치본 snapshot**일 뿐, 현재 package tree의 해시 목록이 아니다.

기존 marker 없는 수동 설치본은 native 설치만으로 자동 교체되지 않을 수 있다. 최초 전환 시 entry 바이트 일치·native adoption·실제 loaded 상태 확인이 별도로 필요하며, entry 이외 운영 파일은 별도 승인된 보존/이동 절차를 adoption 전에 완료해야 한다. Entry-only adoption 승인으로 향후 unrelated-file 삭제가 승인되지는 않는다. Packaged update/rollback은 managed Desktop folder 전체를 교체할 수 있으므로 남아 있는 다른 파일이 삭제될 수 있다. 자세한 절차는 `docs/operations.md`를 따른다. Phase 1 스냅샷으로 복구하는 절차와 native package 버전 간 rollback을 혼동하지 않는다.

## Native install / update / rollback 명령 형태

운영 적용은 별도 승인·검수 후 수행한다. 두 package는 같은 immutable 40자리 release commit SHA로 각각 설치한다. 아래 명령은 사용법이며 이번 작업에서는 실행하지 않았다.

```bash
CANDIDATE_SHA=<verified-full-40-character-commit-sha>

hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

두 package를 같은 새 후보로 update하려면 기존 package를 강제 교체하되 사용자 enablement는 변경하지 않는다.

```bash
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
```

Rollback도 이전에 검수한 full SHA를 `--ref`로 지정하는 같은 native install/update 경로다.

```bash
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
```

`--no-enable --yes-deps`은 설치/교체가 `plugins.enabled`를 새로 쓰지 않게 한다. Python CLI plugin enable, ingress enable, Desktop UI enablement, Gateway 적용/restart는 각각 별도 승인과 확인이 필요한 단계다. 의도적으로 이 작업에서 수행하지 않았다.

Hermes native Git identifier의 `owner/repo/subdir` 동작과 install/update/Doctor contract는 pinned core의 `hermes_cli/plugins_cmd.py`, `plugins_cmd_install.py`, `plugin_dev.py` 기준으로 검증한다. `file://` transport mock은 임시 fake-home test fixture 안에서만 사용한다.

## 검증

Node 테스트는 Desktop entry의 현 경로에서 실행한다.

```bash
cd desktop-plugins/session-complete-telegram
node --check desktop/plugin.js
node --test plugin.test.mjs reply.test.mjs followup.test.mjs materialization.test.mjs
```

Python 테스트는 실제 Hermes core의 `SessionDB`, redactor, PluginContext, Plugin Doctor와 pinned dependencies가 설치된 job-owned environment가 필요하다. Hermes core는 `.github/workflows/tests.yml`에서 publicly verified immutable commit `74306c1d1b080837744faca3e371267dd09dda4b`와 `uv.lock`으로 동기화되고, 별도 `requirements-test.txt`가 bridge test dependency를 고정한다. 전체 Python 명령은 `test_notifier`를 포함한다.

```bash
python -m unittest test_notifier test_reply_bridge test_followup_bridge test_native_install -v
```

CI는 `windows-latest` baseline과 동일한 `ubuntu-latest` job을 수행한다. 이는 source/native contract test 결과이지 다른 PC의 Hermes Desktop GUI, package materialization, 운영 Gateway activation 검증은 아니다.
