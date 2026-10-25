# 변경 이력

## 1.0.2 — 2026-10-25

- Antigravity 공급자의 계정 확인 요구(`Verification Required`)를 일반 종료 코드 오류보다 우선 표시하고, 계정 확인 또는 Compose 모델 변경 후 Research 재사용을 안내합니다. 실패 결과를 성공으로 바꾸거나 계정 검증을 우회하지 않습니다.

- OpenCode 공급자의 사용량 소진과 일시 요청 제한을 일반 session 실패에서 구분해 CLI/Web 실행 오류에 복구 안내를 표시합니다. 원본 공급자 응답 대신 안전한 분류/상태 코드만 감사에 보존합니다.

- OpenCode에서 차단된 외부 파일 작업 이후 정상 제출로 복구해도 전체 Run이 실패하던 처리를 수정했습니다. 차단은 그대로 유지하고 경고로 보존하며 최종 hash·파일·cleanup 검증은 계속 적용합니다. worker는 공유 임시 폴더 대신 호출별 작업 경로를 사용하도록 안내합니다.

- 내부 신뢰 사용자용 MVP로 MCP 조회 정책을 정리하고 미지원 tool catalog 호출을 제거했습니다. 이미지 완료 원장은 controller 메모리에서 앱으로 직접 전달하며, 새 이미지의 CID 발송과 모델 없는 재전송을 연결합니다. 요청·관찰 모델과 미검증 상태를 보존하고 명시적인 모델 불일치는 거부합니다.

- OpenCode API의 큰 JSON 응답을 pipe 대신 크기가 제한된 임시 파일로 수집하여 CLI 종료 시 잘림을 방지하고, message page 전체 예산과 개별 message 제한을 구분합니다. 명시한 작업 디렉터리 자체 접근을 허용하되 상위 폴더는 계속 차단합니다.
- OpenCode v2를 세 번째 Research/Compose 엔진(`opencode_exec`)으로 추가했습니다. 인증된 control process의 공식 session API, phase별 빈 session, task-local 환경, 호출별 permissions, file-reference 제출과 소유 session interrupt를 사용합니다. 모델 catalog는 2-provider cache를 보존하며 OpenCode 항목을 추가/정규화합니다.
- OpenCode 리뷰 보완으로 live shell/PTY/persistent terminal 정리 확인, 최종 assistant finish/completed/model/agent 결합, shell exit·truncated·MCP `isError` 구분, prompt 전 취소/deadline 검사, trace 예산 집행, catalog alias 보존, MCP 호출 전 service 확인, code-repair 실패→수정→재성공 증거를 추가했습니다.
- 2차 보완으로 native tool suite의 cleanup 실패를 단일·마지막 case에서도 성공 exit로 끝내지 않고, `python -m unittest` 실제 명령·대상·실행 test 수·결과 marker와 정확한 `metrics.py` write만 인정하도록 강화했습니다.
- 3차 보완으로 shell simple-command 경계를 지켜 다른 명령·redirection·comment의 파일 인자를 unittest 대상으로 오인하지 않도록 했습니다.
- OpenCode MCP tool/resource 조회를 Research/Compose에 허용하고 공급자 표시 모델 label, Task별 이미지 생성 엔진(`codex`/`opencode`/`bailian`/`openrouter`)·모델 설정, 공통 이미지 service와 CLI/Web 편집 경로를 추가했습니다.
- Task별 선택적 Research HTTP 조회와 읽기 목적 JSON POST, hostname 기반 loopback CONNECT proxy profile을 추가했습니다. 기본값은 꺼짐이며 Compose에는 제공하지 않습니다.
- 앱의 공식 이미지 취득에도 같은 proxy snapshot을 전달하고 공개 목적지·redirect·TLS 검증을 유지합니다.
- Research에서 양 provider가 사용할 수 있는 native Codex 이미지 생성 helper를 추가했습니다. 생성 파일은 기존 artifact 검증을 거칩니다.
- Task의 명시적 식별자 별칭을 검증하고, 검증된 발송 이력의 동일 내용에 한해 양방향 중복 비교를 지원합니다.
- 메일의 가시 날짜와 참조 표기는 Task 명세에 맡기며 숨은 날짜·record·CID 및 원문 전달 검증은 유지합니다.
- 공개 v1.0.1의 익명화·가상 예제·패키지 구성과 기존 수정 사항을 유지합니다. 운영 데이터는 릴리스 소스에 포함하지 않습니다.

## 1.0.1 — 2026-10-08

- SMTP 유휴 폴링에서 MIME 본문 조회를 제거하고 완료 시도 감사 파일 복구를 60초·100건 단위로 제한했습니다. 누락 파일은 재시작 후 SMTP 재전송 없이 복구합니다.
- Task별 숨은참조(BCC) 선택을 추가하고 수신자 주소가 메일 헤더에 노출되지 않도록 처리했습니다.
- Compose 결과 제출에 HTML 태그 균형 검사와 안전한 오류 진단을 추가했습니다.
- Antigravity의 활성 MCP 도구 schema 탐색과 system_message 처리를 개선하고 단일 에이전트 실행 계약을 강화했습니다.
- 공식 이미지 취득의 정확한 HTTPS 호스트 허용 목록을 보완했습니다.
- 기존 DB schema v3, Task·실행 이력과 결과 제출 계약을 유지합니다.

## 1.0.0 — 2026-09-11

첫 stable 배포본이다. 신뢰 내부 사용자의 조사·메일 자동화를 위한 현재 구현을
독립된 소스 기준선으로 정리했다. 이 버전 표시는 기존 설치의 DB·Task를 바꾸지 않는다.

- CLI와 Web이 공유하는 조사 예약, Research → Compose, 결과 검증, 불변 archive,
  내장 SMTP 큐와 전달 이력을 제공한다. 서울 시간 기준의 논리 날짜를 재시도에도 유지한다.
- 관리자·사용자·조회자 로그인, Task별 열람 권한, Task·발신 계정·수신자 그룹의 소유권을
  제공한다. 실제 Task 일반 폼과 고급 YAML 편집, 단계별 AI 설정, 모바일 화면을 지원한다.
- 검증된 조사·파일 재사용, 완성된 메일만 전송하는 복구, SMTP 단계별 진단과
  DATA 이후 불확실한 발송의 자동 재전송 방지를 포함한다.
- 앱 소유 파일 취득과 PDF·이미지 출처·무결성 검사, 큰 모델 도구 로그의 별도 예산,
  고정 결과 파일 제출, 수신자 그룹 이름을 불변 ID로 연결하는 계약을 포함한다.
- 설치·초기 관리자·TLS 프록시·SMTP·예약·백업·업그레이드 가이드를 현재 동작에 맞춰
  정리하고, 큰 한글 Task 입력을 수용하도록 Nginx 예제 body limit을 2m로 맞췄다.
- 가상 소프트웨어 릴리스 조사 예제와 합성 테스트만 포함한다. 운영 Task·메일 명세,
  운영 전용 마이그레이션·현장 스크립트, 개인 식별값·실제 설정·이전 Git 이력은 제외했다.
- Python 버전별 CI, wheel 설치 후 독립 경로 검증, 소스 파일 목록·SHA-256 검사와
  배포 artifact 체크섬을 추가했다.

DB schema는 v3이다. v2에서 업그레이드할 때 먼저 전체 서비스의 유휴 상태와 검증된
백업을 확보한다. v3는 구버전으로 열 수 없고, 신규 쓰기 이후 자동 DB 롤백을 하지 않는다.
현재 검증 범위와 알려진 한계는 [STATUS](docs/STATUS.md), 상세 절차는
[배포 안내](docs/USER_DEPLOYMENT.md)를 따른다.
