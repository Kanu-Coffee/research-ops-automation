# 웹 로그인·역할·소유권

앱 로그인은 모든 업무 화면·API·미리보기·다운로드를 보호합니다. 사용자 계정과 실제 운영 비밀번호는 공개 예제에 포함하지 않습니다. [설치 가이드](USER_DEPLOYMENT.md)의 TLS proxy를 준비한 뒤 최초 계정을 개설합니다.

## 계정과 초기 설정

앱 로그인은 Web의 모든 업무 페이지·API·미리보기·다운로드를 보호한다.
최초 관리자가 없으면 로그인 화면은 관리자 개설 화면으로 안내한다. 공개 회원가입은 없다.
보호된 서버 터미널에서 30분 유효 일회용 토큰을 발급하고 브라우저에서 사용자가
아이디와 비밀번호를 입력한다. 토큰은 비밀값이므로 채팅·로그·버전 관리에 보관하지 않는다.

```bash
"$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/researchctl" --config "$RESEARCHOPS_INSTALL_DIR/config/settings.yaml" auth setup-token
```

첫 관리자 생성과 토큰 소비는 하나의 트랜잭션이다. 계정이 개설된 후에는 토큰을 다시
발급할 수 없다. 관리자는 사용자 관리에서 관리자·사용자·조회자를 추가한다.
추가 계정의 임시 비밀번호는 최초 로그인 후 반드시 변경한다.
비밀번호는 15~128자이며 SMTP 비밀번호와 별개다. 이메일 초대·이메일 복구는 제공하지 않는다.

관리자는 임시 비밀번호 재설정·계정 비활성화·복구와 Task 권한 변경을 할 수 있다.
마지막 활성 관리자의 비활성화·비관리자 전환은 동시 요청에서도 차단한다.
서버에서 복구할 때는 다음 명령의 숨김 입력 프롬프트를 사용한다. 비밀번호를 명령행
인수나 환경변수로 전달하지 않는다.

```bash
"$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/researchctl" --config "$RESEARCHOPS_INSTALL_DIR/config/settings.yaml" auth reset-password ADMIN_USERNAME
```

## 권한과 세션 계약

- 관리자는 전체 Task 운영·SMTP·수신자·시스템·사용자 관리를 수행한다.
- 사용자는 본인이 소유한 Task의 생성·수정·복제·예약·실행·삭제/복구와 결과 조회,
  본인 발신 계정·수신자 그룹 관리를 수행한다. Run·메일·파일은 Task 소유권을 따른다.
- 조회자는 지정된 불변 Task ID의 개요·과거 실행·결과·메일·파일·로그만 읽는다.
  지정 목록이 비어 있으면 접근 가능한 Task도 0개다. 새 Task·복제 Task는 자동 허용하지 않는다.
  기존 Task의 이름·버전 변경과 삭제 후 보존 이력은 기존 권한을 유지한다.
- 목록·검색·집계·페이지 제한 전에 SQL에서 권한을 적용한다. 개별 Run, Handoff,
  SMTP 상태와 파일은 소속 Task를 확인하고 파일을 연다. 미허용 자료는 404,
  조회자의 변경 요청·관리자 기능은 403이다. HEAD와 부분 갱신도 같은 경계를 거친다.
- Task 권한 회수와 계정 상태 변경은 다음 요청부터 반영된다. 파일 다운로드처럼 이미
  시작한 응답의 bytes를 회수한다는 의미는 아니다.
- 비활성화는 로그인·세션만 차단한다. 기존 Task 예약·실행·SMTP 발송은 계속된다.
  관리자·scheduler의 대신 실행도 소유권을 변경하지 않는다. 새 사용자는 내부 신뢰 사용자다.
- Task·발신 계정·그룹의 `entity_catalog.owner_user_id`는 서버에서 결정하는 불변 값이다.
  기존 자료는 최초 관리자에게 귀속하고 신규 자료는 생성 계정에 귀속한다. 소유권 이전은 없다.
  관리자도 다른 사람의 Task에는 그 소유자의 발신 계정·그룹만 선택한다.
- `catalog_name` 그룹 스냅샷, `legacy_ids` 검사와 SMTP revision은 소유자 범위로 계산한다.
  다른 사용자의 그룹 변경은 대기 중 메일을 무효화하지 않는다. 생성 요청 키도 소유자별로 분리한다.
  계정이 없는 사용자에게 관리자 `default` 계정을 자동 선택하지 않는다. 전역 발송 사용 설정은
  관리자만 변경하며 사용자 폼에 값을 위조해도 보존한다.
- scrypt(`N=131072,r=8,p=1`)·개별 salt로 비밀번호를 해시한다. 비밀번호 계산은 동시 2건,
  로그인은 계정별 5분 10회·전체 5분 60회로 제한하며 DB에 시도 횟수를 보존한다.
- 세션 토큰은 32-byte 난수이며 DB에는 SHA-256 digest만 저장한다. 세션별 CSRF를 사용한다.
  로그인·비밀번호 변경 시 토큰을 교체하고 이전 세션을 폐기한다. 로그아웃은 CSRF 보호 POST다.
- 세션은 미사용 2시간·생성 후 12시간에 만료한다. API polling과 부분 갱신은 미사용 시간을
  연장하지 않는다. 초기 로그인/개설용 세션은 15분이며 만료 정리와 수량 제한을 적용한다.
- 운영 쿠키는 `__Host-researchops_session`, Secure, HttpOnly, SameSite=Lax, Path=/다.
  외부 접속은 HTTPS가 필요하다. 실제 loopback 전용의 독립 시험에서만
  `web.allow_insecure_local_auth: true`로 별도 HTTP 개발 쿠키를 사용할 수 있다.
  remote-proxy와 함께 이 옵션을 활성화할 수 없다.
- Web 감사 actor는 `user:<고정 ID>`다. 요청별 ContextVar와 연결별 TEMP trigger가
  기존 일반 운영 이벤트에 주체를 전달하며 CLI/worker의 명시적 주체는 유지한다.
  영속 schema에 SQLite 사용자 함수 의존 trigger를 추가하지 않는다.

비밀번호·세션·설정 토큰은 AI worker, 결과 archive, SMTP 메시지에 전달하지 않는다.
이 Web 권한은 기존 신뢰 운영 runner의 동일 UID host 접근을 OS 격리로 바꾸지 않는다.
비밀번호 해시 설정은 [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)의
scrypt 기준을 따르며, 입력 단계 탭의 역할·키보드 이동은
[WAI 탭 패턴](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/)을 기준으로 구현한다.

## 화면과 저장 동작

- 데스크톱 사이드바와 모바일 56px 헤더/서랍을 사용한다. 파란색·한국어 용어와 44px
  주요 조작 영역을 공유하고, 모바일 입력 글자는 16px 이상으로 표시한다.
  주 기준은 1280×1024, iPhone 16 Pro의 402×874·874×402(DPR 3)이다.
  전환점은 900px이고 헤더·본문·패널·저장 바에 safe-area를 반영한다.
  브라우저 viewport 검증과 실제 Safari의 키보드·주소창·safe-area 검증은 구분한다.
- 대시보드는 확인 필요·진행 중·다음 예약·최근 완료를 표시한다. 조회자의 집계에는
  허용되지 않은 Task와 전역 운영 설정을 포함하지 않는다.
- 새 Task는 조사 내용 → 메일·수신자 → 실행·일정 → 확인 순서다.
  `launch_mode=save|run|schedule`이 완료 행동이며 기본값은 save다. 값이 없는 기존
  요청은 기존 action/schedule_enabled 계약을 유지한다. 수정은 기존 예약 상태,
  복제는 예약 OFF를 유지한다. 지시문·수신자 선택 계약·고급 설정을 자동 재작성하지 않는다.
- 기존 편집은 조사 / 메일·수신자 / 실행·일정 탭으로 이동한다. 필수 입력 오류는 해당
  탭을 열고 초점을 이동한다. 요약과 고정 저장 바, 미저장 이탈 안내를 제공한다.
  로그인 만료 시 현재 입력을 보존하고 별도 로그인 후 명시적으로 다시 저장한다.
  요청 키는 보존하고 같은 사용자로 로그인했는지 확인한다. 변경 요청을 자동 재전송하지 않는다.
- 발신 계정·그룹이 필요하면 같은 단계의 패널에서 생성하고 원래 입력으로 돌아온다.
  부모 화면으로 비밀번호나 실제 수신자 주소를 전달하지 않고 ID·표시 이름·인원 수만 갱신한다.
- SMTP는 서비스 선택 → 계정 정보 → 저장으로 구성한다. Gmail 기본값은 STARTTLS 587이며,
  다른 SMTP와 TLS 465도 지원한다. `앱 비밀번호` 빈 값은 기존 값을 유지한다.
  계정 이름은 수정 폼에서 변경한다. 이름만 수정하면 SMTP 파일·자격증명·revision을 그대로 둔다.
  저장·연결 확인·테스트 메일은 별도 행동이다. 테스트와 연결 확인은 운영 선행조건이 아니다.
- 실행 상세는 요약 / 이메일 / 파일 / 로그·진단 독립 URL 탭이다. 선택 영역만 렌더하고
  상태 자동 갱신은 페이지 전체를 다시 불러오지 않는다. 펼침·스크롤·선택 텍스트를 보존한다.
  발송 불확실성·부분 진단·원문 및 hash 보존 등 기존 의미를 바꾸지 않는다.
- 관리 이벤트 검색은 표시된 관리 기록을 대상으로 함을 명시한다. 원본 로그 미리보기는
  64KiB씩 읽고 범위를 표시하며, 전체 원문 다운로드는 기존 경로와 bytes를 유지한다.
- 용량은 1,000 단위 B/KB/MB/GB/TB, 소수 최대 한 자리로 표시한다. 정확한 바이트와
  읽기 범위는 펼쳐 확인할 수 있다. DB/API 정수와 UTF-8 offset 처리는 변경하지 않는다.
- 파란색 자동화 순환 경로·연결 노드·AI 스파크 아이콘을 공통/로그인 화면에 적용한다.
  ICO·16/32px PNG·180px Apple touch 아이콘의 고정 GET/HEAD만 미인증 제공하며 세션을 만들지 않는다.
- 작성 중 사본/Draft UI·Web 명령·`task draft` CLI·서비스는 제거한다.
  `GET/POST /tasks/{task_id}/advanced`는 실제 Task에 새 불변 버전을 저장한다.
  YAML·두 문서·부가 파일·예약 상태·버전 충돌 검사를 유지하며 저장만으로 실행하지 않는다.
  보존 중인 Draft DB 행·파일·후보 버전은 삭제하거나 자동 발행하지 않는다.

## 인터페이스와 호환

계정 라우트: `/setup`, `/login`, POST `/logout`, `/account/password`, `/settings/users`.
`GET /api/session`은 로그인 정보와 현재 CSRF를 반환하며 미인증은 401이다.
Task 옵션은 `/api/task-options`, 서울 일정 미리보기는 `/api/schedule/preview`다.
원본 로그 부분 조회는 `/api/runs/<run-id>/logs?file=research.stdout&offset=0`이며
research/compose의 stdout/stderr만 허용한다.

기존 Task·Run·메일·artifact URL과 business service의 중복 방지·버전 검사는 유지한다.
v2→v3 migration은 역할 CHECK를 확장하고 소유권 및 최초 관리자 식별을 추가한다.
계정·세션·조회 권한·기존 업무 행·Task package·SMTP 설정을 보존한다.
기존 backup/restore에 인증·소유권 데이터도 포함된다. v2 worker는 소유자별 수신자 범위를
모르므로 혼합 실행하지 않으며 v3 DB에 대한 구버전 초기화는 차단한다.

## 운영 전환과 복구

Web·worker·SMTP·scheduler는 동일 코드·DB schema를 사용합니다. v3 migration을 검증한 private DB 복사본과 백업을 준비하고 실행/전송이 유휴일 때 전체 서비스를 함께 전환합니다. 구버전은 v3 DB를 열 수 없으며 소유권을 모르는 구형 worker와 혼합하지 않습니다.

신규 쓰기가 생긴 뒤에는 과거 DB를 자동 복원하지 않고 v3 호환 수정으로 복구합니다. 설정·계정·세션·Task grant·SMTP 상태를 함께 보존하며 상세 명령은 [백업과 업그레이드](BACKUP_AND_UPGRADE.md)를 따릅니다.
