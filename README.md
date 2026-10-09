# Hermes Telegram Bridge

뉴메타W의 Hermes Desktop 텔레그램 알림·질문 응답·완료 카드 후속 답장을 함께 관리하는 비공개 소스 저장소다.

## 현재 범위

현재 검수·활성화된 설치본의 소스와 테스트를 그대로 보존한 **원격 관리 1단계**다. 기능 수정, plugin ID 변경, 설치 위치 이전, 자동 배포 및 Gateway 재시작은 이 저장소 생성에 포함되지 않는다. 아직 `hermes plugins install owner/repo`로 설치하는 공식 통합 패키지가 아니다.

- `desktop-plugins/session-complete-telegram/`: 앱 수준 Desktop 알림과 Python 브리지/헬퍼, 합성 테스트, 기존 구성 요소 README.
- `plugins/desktop-clarify-telegram/`: 기존 Telegram Gateway에 연결하는 Python 수신 핸들러.
- `docs/source-manifest.json`: 최초 등록한 설치본 소스의 상대 경로·크기·SHA-256. 이후 변경 파일의 현재 해시로 오해하지 않는다.
- `docs/operations.md`: 검증·반영·복구 경계.
- `docs/wiki/`: 장기 의사결정과 운영 함정 색인.

개발 원본은 `C:/WebApp/hermes-telegram-bridge`다. 실제 설치본은 기존 Hermes home의 두 대응 디렉터리에 유지된다. 이 초기 스냅샷에는 기존 Windows 설치 경로가 남아 있으므로 다른 PC/OS에 대한 이식성을 주장하지 않는다.

## 기능과 보존할 계약

- 모든 로컬 Desktop 프로필의 완료 알림: 실제 최종 응답과 관측 가능한 작업 종료 후 60초 quiet 검증.
- 질문 카드는 질문 프로필의 clarify 대기 설정을 따르며, 최소 1시간·최대 24시간 TTL.
- TTL과 45초 transport lease는 별개다. 일시적인 연결/관측 누락은 전달을 차단하고 카드는 보존한다.
- 불완전한 snapshot은 unverified, 확정된 질문 변경/응답/만료는 terminal이다.
- 정확한 소유자·메시지·질문 identity 검증과 one-shot claim을 유지한다. 결과 불명확한 쓰기는 재전송하지 않는다.
- Gateway가 Telegram 업데이트를 소유한다. 두 번째 polling consumer를 만들지 않는다.

## 검증

저장소의 해당 소스 디렉터리에서 실행한다.

```bash
cd desktop-plugins/session-complete-telegram
node --check plugin.js
node --test plugin.test.mjs reply.test.mjs followup.test.mjs
python -m unittest test_reply_bridge test_followup_bridge
```

Python에는 `python-telegram-bot`, PyYAML 및 필요 시 해당 설치본의 Hermes 의존성이 필요하다. 운영 자격증명을 테스트에 넣지 않는다. 테스트는 합성 데이터와 임시 DB, mock Telegram transport를 사용한다.

초기 설치 후보에서 확인한 실행 결과는 Python 브리지 68/68, Node 95/95 통과다. 이는 모든 테스트의 통과나 다른 환경의 실행 보장이 아니다. `test_notifier.py`는 실제 Hermes SessionDB와 설치 경로에 의존하며, 기존 bootstrap 재실행/ruamel 오류가 별도로 남아 있다. 미실행·실패를 CI 성공으로 표시하지 않는다. 현재 자동 CI/배포 workflow는 추가하지 않는다.

## 작업 흐름

저장소 수정 → 관련 테스트·필요한 독립 검수 → commit/push → 별도 승인된 설치 반영 → 실제 readback.

**Push는 설치 또는 재시작이 아니다.** Desktop JS는 지원되는 hot reload로, Gateway 상주 Python은 승인된 재시작 후 활성화를 확인한다. 자세한 경계는 `docs/operations.md`를 따른다.

향후 공식 통합 패키지화·경로 이식성 개선·CI 환경 독립화는 별도 단계다. 초기 원격 관리를 위해 현재 안정 코드를 다시 설계하지 않는다.
