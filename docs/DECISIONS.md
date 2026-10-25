# v1.0.0 설계 결정

공개 릴리스에서 유지하는 기술 선택입니다. 특정 운영자·호스트의 승인 또는 사고 이력을 기록하지 않습니다.

| 선택 | 이유와 대안 |
|---|---|
| Python, 공통 application service, 얇은 CLI/Web | 실행·권한·중복 방지 계약을 한곳에서 유지한다. 프런트엔드별 별도 업무 로직을 두지 않는다. |
| SQLite와 단일 호스트 큐 | 설치와 백업을 단순하게 한다. 다중 호스트 확장은 별도 설계가 필요하다. |
| Research와 Compose 분리 | 검증·파일 취득·수신자 스냅샷을 고정한 후 메일을 작성하고 조사 없이 재작성할 수 있다. |
| 원문과 hash 보존 | 검증한 내용과 실제 전달 내용의 일치를 검사한다. 앱이 worker의 HTML/text를 재작성하지 않는다. |
| 앱 소유 내장 SMTP | 실제 주소·자격증명을 모델에서 제외하고 영속 attempt·receipt를 관리한다. 외부 outbox/receipt 호환 경로도 유지한다. |
| 불확실 전송 자동 재시도 금지 | DATA 후 응답 유실에서 중복 전달을 방지한다. 수동 재시도는 중복 가능성을 인지한 별도 기록이 필요하다. |
| Task 불변 버전·자원 불변 소유권 | 실행 중 편집과 다른 사용자의 전달 설정을 분리한다. 소유권 이전은 제공하지 않는다. |
| 이름 기반 수신자 선택 | Compose 입력의 활성·유효 그룹 스냅샷에서 정확한 한 이름을 해석한다. 유사 문자열·첫 그룹으로 대체하지 않는다. |
| Content dedupe 기본 OFF | 조사 record를 보존한다. 명시적으로 켠 경우 검증된 과거 발송의 key/content 정확 일치만 제외한다. |
| Asia/Seoul 논리 날짜 | 예약·사용자 표시·SMTP Date의 해석을 맞추고 retry에서 원래 날짜를 유지한다. |
| 신뢰된 native CLI 운영 | 기존 CLI·MCP 기능을 사용한다. 동일 UID 접근을 적대적 Task 격리로 표현하지 않는다. |
| 빈 루트 초기 설치·수동 업그레이드 | 기존 설정과 원장을 덮어쓰지 않는다. 새 쓰기 뒤 DB 자동 rollback을 제공하지 않는다. |
| 합성 공개 예제 | 운영 지시문·계정·주소·업무 자료를 공개하지 않고 재현 가능한 개발 입력을 제공한다. |

## 2026-10-24 — MCP 조회, 공급자 표시, Task별 이미지 설정

OpenCode의 MCP tool/resource 조회를 Research와 Compose 모두에 허용하고, Code Mode
`execute`를 열어 nested MCP tool이 자체 permission을 검사하도록 한다. 직접 web/HTTP는
기존 phase network 정책을 유지한다. 모델 picker label은 모델명·providerID·catalog ID를
함께 표시해 동일 이름 모델을 구분한다. 이미지 생성은 Research/Compose LLM과 독립된
`image_generation` Task 설정으로 분리하고, Codex/OpenCode/Bailian/OpenRouter adapter를
두되 credential은 controller/보호 설정에만 둔다. 기존 Task에 필드가 없으면 Codex native
helper 동작을 유지한다.

## 2026-10-24 — OpenCode v2는 API 세션 제어로 통합

OpenCode CLI의 `run`에 host HOME과 provider 인증을 넘기는 대신, 인증된 control process의
공식 `opencode api`로 phase별 빈 session을 제어한다. worker session에는 task-local
HOME/TMPDIR과 최소 환경만 설정하고, 전체 지시문은 staging 파일로 전달한다. 호출별 native
permissions는 broad deny 후 필요한 파일·shell·(Research public only) web/MCP만 허용하며,
`subagent`·Code Mode `execute`는 차단한다. 완료는 session outcome/idle·최종 assistant
message·남은 shell·제출 파일 hash/size를 함께 확인한다. Timeout/취소 시 소유권을 기록한
session에만 interrupt하고 공유 server나 다른 session은 건드리지 않는다. OpenCode shell은
host 사용자 권한을 가지므로 적대적 Task 격리로 표현하지 않는다.

OpenCode control의 큰 JSON은 private 임시 regular file stdout으로 수집한다. CLI의
pipe flush 이전 종료로 생기는 잘림을 피하고 child file-size limit, 합산 출력 예산과
취소/정리를 유지한다. message page의 raw bytes는 전체 trace 예산으로 한 번 계산하고,
event 크기 제한은 개별 message에 적용한다. 허용 작업 디렉터리 자체와 하위 경로만
명시하며 상위 디렉터리 접근 거부는 성공으로 바꾸지 않는다.

## 2026-10-24 — 내부 신뢰 사용자용 MVP 범위

MCP는 등록된 서버를 사용하고 알려진 변경 이름을 제한하는 실용 정책을 적용한다.
모든 외부 도구의 의미를 증명하는 capability registry는 MVP 선행 조건이 아니다.
이미지 생성은 Task 설정에 따라 Research/Compose의 명시적인 helper 요청 때만 실행한다.
완료 job은 controller 메모리에서 runner 결과로 전달하고 worker ledger 파일을 승인
근거로 사용하지 않는다. 검증한 snapshot과 bytes는 앱 archive와 재전송에 보존한다.
Codex/OpenCode의 관찰 불가능한 실제 이미지 모델은 미검증으로 표시하고 정상 bytes를
허용하되, 명시적 모델 불일치는 실패 처리한다. 계정별 모델 실시간 discovery와 Web
credential 관리, 적대적 동일 UID 격리와 완전한 remote-job 증명은 후속 범위다.
기존 파일 검증·불변 실행 이력·cleanup·SMTP 중복 방지와 원문 전달 계약은 유지한다.

## 2026-10-08 — SMTP 유휴 감사 복구와 결과 제출 보완

완료된 SMTP 시도의 일상 조회는 MIME BLOB을 제외한 metadata를 사용합니다. 완료 즉시 감사 파일을 보존하고, 누락 복구는 60초마다 100건씩 cursor로 순회합니다. 재시작 복구가 SMTP 재전송이나 receipt 변경을 일으키지 않으며 변조된 감사 파일을 덮어쓰지 않습니다. Task별 BCC와 HTML 균형 검사는 기존 불변 버전·본문 보존 계약을 유지합니다.
