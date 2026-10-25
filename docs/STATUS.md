# v1.0.2 릴리스 범위

이 문서는 공개 소스의 기능과 제약을 기록합니다. 특정 서버의 가동 상태나 계정·Task·실발송 이력을 공개하지 않습니다.

## 제공 범위

Research/Compose native 실행, 단계별 모델 선택, Task 불변 버전, 서울 예약, 내장 SMTP와 원문 재전송, 관리자·사용자·조회자와 자원 소유권, 파일 취득·첨부 검증, 실행별 감사 archive를 제공합니다. Linux 사용자 소유 설치와 systemd 예제, SQLite 백업·오프라인 복원 도구를 포함합니다.

기본 자동 검증은 합성 입력·독립 runtime·fake runner/CLI double·SMTP double로 수행합니다. 정확한 릴리스 검증 결과는 GitHub Release와 CI 기록을 기준으로 확인합니다. `--live` 개발 명령은 별도로 제공하지만 기본 테스트가 실제 모델·실수신자 발송·외부 proxy 왕복까지 검증하는 것은 아닙니다.

## v1.0.0 기준선 검증 이력

| 검증 | 결과 |
|---|---|
| Python 3.12.14 전체 애플리케이션 회귀 | 1,299건 중 1,293 통과, 조건부 6건 제외 |
| Python 3.14.4 전체 애플리케이션 회귀 | 1,299건 중 1,293 통과, 조건부 6건 제외 |
| 공개 배포 안전성 추가 회귀 | 5건 통과 |
| 독립 브라우저 QA | 274건 통과, 콘솔 오류 없음 |
| Fake 두 단계 pipeline | 합성 시나리오 4종 통과 |

조건부 제외는 전용 kernel/cgroup·quota 등의 호스트 조건을 요구하는 검사이며 통과로 계산하지 않습니다. 위 검증의 실제 AI 호출·SMTP 발송은 0건입니다. 새 배포 안전성 회귀를 포함한 전체 수집 대상은 1,304건입니다. 최종 wheel/sdist·CI·release asset 증거는 해당 GitHub Release 기록을 따릅니다.

## 알려진 제약

- 내부 신뢰 사용자용 native 실행입니다. 동일 UID의 host 파일·전체 egress·영구 디스크 quota의 적대적 Task 격리는 완성되지 않았습니다.
- Codex, Antigravity, OpenCode의 도구·MCP 예방 제어는 동일하지 않습니다. 특히 Agy Compose의 MCP 제한은 지시·사후 감사 수준이고, OpenCode shell은 host 사용자 권한을 유지합니다. OpenCode MCP 합성 도구 검증은 호출별 test MCP 주입 경로가 없어 unsupported로 차단합니다.
- 로컬 SQLite를 사용하는 단일 호스트 구조입니다. 다중 호스트 active-active, 분산 scheduler, 고가용성 자동 failover를 제공하지 않습니다.
- SMTP 수락은 수신함 도착 보증이 아닙니다. 불확실 전송은 확인과 명시적 수동 판단이 필요합니다.
- 초기 설치기는 빈 디렉터리만 지원합니다. 업그레이드는 새 버전 검증·유휴 전환·백업을 사용하는 수동 절차입니다.
- Web 계정 소유권 이전, 공개 회원가입, 이메일 초대·비밀번호 복구 메일은 제공하지 않습니다. 비활성 계정의 기존 예약·Task 실행·SMTP는 계속됩니다.
- Content dedupe는 전체 과거의 정확한 내용 일치 방식입니다. TTL·최근 상태 비교·주기적 재알림 정책은 제공하지 않습니다.
- 설치 호스트의 TLS·proxy·CLI 인증·모델 이용 권한·SMTP 정책·재부팅 복구는 각 운영자가 확인해야 합니다.

## 배포 및 호환

DB schema v3를 사용합니다. 오래된 schema는 앱 초기화 시 migration될 수 있으므로 복사본 검증과 백업을 먼저 수행합니다. Web·worker·SMTP·scheduler는 같은 릴리스로 전환합니다. 새 쓰기 후에는 과거 DB로 자동 복원하지 않고 현재 schema와 호환되는 수정으로 복구합니다.

소스 공개 시 실제 설정·DB·archive·운영 Task 지시문, 해당 Task 전용 migration/실행 스크립트와 과거 내부 기록·세션 프롬프트를 제외하고, 독립적인 가상 소프트웨어 릴리스 예제로 대체했습니다. [보안](12_SECURITY.md)과 [업그레이드](BACKUP_AND_UPGRADE.md)를 함께 확인하세요.

## v1.0.1 변경

SMTP 유휴 metadata 조회와 제한된 감사 파일 복구, Task별 BCC, Compose HTML 균형 검사, 활성 MCP schema 탐색과 단일 에이전트 감사 개선을 포함합니다. 실제 운영 설정·업무 지시문·내부 검토 기록은 공개하지 않습니다. 이번 릴리스의 검증 결과는 Release 첨부 `VALIDATION.json`과 CI 기록에서 확인합니다.

## 2026-10-24 main 통합 검증

공개 v1.0.1 기준에 최신 Research HTTP·선택적 CONNECT proxy·이미지 생성 helper·명시적
식별자 별칭을 통합했다. 변경 내용은 [변경 이력](../CHANGELOG.md)과
[HTTP 라우팅](HTTP_PROXY_ROUTING.md)을 따른다. 기존 릴리스의 가상 예제·익명화·패키지
구성과 MCP permission fixture 수정을 유지하며 내부 운영 기록과 Task 원본은 포함하지 않는다.

- Python 3.12.14 전체 회귀: 1,359건 중 1,353 통과, 조건부 6건 skip, 실패 0.
- Compileall, git diff whitespace 검사, shellcheck와 actionlint 통과.
- 앱 소스·테스트·문서 gitleaks 검사 통과.
- 소스 목록·버전·문서 링크·SHA-256 검사와 wheel/sdist 빌드 통과.
- 별도 환경에 설치한 wheel의 새 runner 모듈 import·의존성 검사와 fake pipeline 4종 통과.
- Wheel/sdist에 내부 Task·authoring export·handoff 기록이 포함되지 않음을 확인.

실제 모델·업무 실행·SMTP 발송과 운영 배포는 이번 Git 통합 검증에 포함하지 않는다.
다른 Python 버전의 전체 회귀는 이 로컬 검증에서 실행하지 않았으며 기존 CI matrix를 유지한다.

## 2026-10-24 OpenCode v2 provider

`opencode_exec`를 Research/Compose의 세 번째 엔진으로 연결했습니다. 인증된 control
process의 공식 `opencode api` session을 사용하며, phase별 빈 session, task-local 환경,
호출별 native permissions, strict file-reference 제출, 소유 session interrupt를
적용합니다. 기존 Codex/Agy 두-provider cache는 보존하면서 OpenCode metadata를
추가합니다. 실행 계약과 제한은 [OpenCode runner](OPENCODE_RUNNER.md)에 정리했습니다.

- Python 3.12.14 전체 회귀: 1,407건 중 1,401 통과, 조건부 6건 skip, 실패 0.
- OpenCode의 대용량 API JSON을 크기가 제한된 private regular file stdout으로 수집하고,
  message page 전체 예산과 개별 message 크기 제한을 분리했습니다. 명시 작업 경로
  자체와 하위만 허용하며 상위 디렉터리 접근 거부는 계속 실패로 보존합니다.
- 합성 live `validate-runners`: `opencode_exec` Research→Compose→검증→dry-run archive
  1건 통과(2 model responses, live shell/PTY/persistent terminal 0, cleanup 확인). 실제
  SMTP 발송 0건, 새 임시 runtime 사용. qwen 모델의 반복 1건은 240초 deadline을 넘겨
  timeout 실패로 보존했으며, `alibaba-token-plan/deepseek-v4.1-flash`로 수행한 별도
  합성 실행은 통과했습니다. timeout을 성공으로 바꾸거나 자동으로 시간을 늘려 재시도하지
  않았습니다.
- 합성 production adapter Research→Compose→검증→dry-run handoff 1건 통과. 두 세션 모두
  `cleanup_verified=true`, live shell/PTY/terminal 0, 제출 hash 일치, SMTP 0건입니다.
- 합성 live `validate-runner-tools`: `file-write`, `file-code`, `code-repair` 통과.
  `code-repair`는 모델이 실행한 실패 unittest → metrics 수정 → 같은 테스트 성공 순서,
  세 테스트, 테스트 파일 hash를 확인했습니다. `web-search`는 native 검색·조회는
  관찰됐으나 시험 중 write permission denial이 함께 관찰되어 blocked로 보존했습니다.
  MCP 합성 scenario는 provisioning 미지원으로 blocked입니다.
- OpenCode service 설정·로그인·설치를 자동 변경하거나 기존 session을 삭제하지 않았습니다.

## 2026-10-24 OpenCode MCP·공급자 표시·이미지 설정

OpenCode MCP tool/resource 조회를 Research와 Compose 모두에 허용하고, 구조화된
`mcp.list`/`mcp.resource.catalog` metadata를 사용했습니다. 모델 label은 모델명·
providerID·catalog ID를 함께 표시합니다. Task별 `image_generation` 설정과
Codex/OpenCode/Bailian/OpenRouter 공통 이미지 service, CLI `image`·`task image`,
Web Task 편집 필드를 추가했습니다.

- 전체 회귀: 1,417건 중 1,411 통과, 조건부 6건 skip, 실패 0.
- 이미지 adapter 단위 검증은 fake Codex/Bailian/OpenCode CLI와 OpenRouter HTTP double만
  사용했으며 실제 유료 이미지 생성은 0건입니다.
- Research 및 Compose worker에 staging되는 이미지 helper는 `ImageBroker` IPC를 통해
  Task에 구성된 엔진(Codex/OpenCode/Bailian/OpenRouter)으로 요청을 위임합니다.
  자격증명은 worker에 전달되지 않고 Controller가 관리합니다.
  단위·통합 검증은 double/mock을 사용했으며 실제 유료 모델 실호출은 포함하지 않습니다.
- MCP 합성 도구 주입은 여전히 unsupported입니다. 실제 등록 MCP의 조회 성공은 별도
  합성 live 검증이 필요합니다.

## 요청형 이미지 생성과 내부 신뢰 MCP의 MVP 범위

Task 저장/시작만으로 이미지를 생성하지 않습니다. Research/Compose가 helper를 호출할
때 Task의 엔진/모델로 요청하며, controller 완료 snapshot과 검증한 bytes를 새 CID·archive·
발송·모델 없는 재전송에 연결합니다. worker ledger 파일은 승인 근거에서 제외했습니다.
모델 목록은 후보이며 계정별 실시간 지원 조회는 제공하지 않습니다. native 이미지 도구의
실제 모델은 미검증일 수 있고 그 상태를 보존합니다. 명백한 모델 불일치는 실패 처리합니다.

OpenCode는 등록된 MCP 도구를 넓게 허용하고 알려진 변경 이름을 제한합니다. 미지원
tool catalog API에 의존하지 않으며 완전한 읽기 전용/egress 격리를 보증하지 않습니다.
계정별 capability discovery·Web credential 관리·전체 remote-job 증명은 후속 범위입니다.

- Python 3.12 전체 회귀: 1,467건 중 1,461 통과, 환경 조건 6건 skip, 실패 0.
- 실제 broker/service와 provider transport double을 통한 새 이미지 생성→CID→SMTP double
  동일 bytes, 미등록 원장 거부, 이미지 포함 delivery-only 회복을 확인했습니다.
- Compileall·release/hash·wheel/sdist 빌드와 별도 wheel 설치의 fake pipeline을 검증했습니다.
  실제 유료 모델·실 SMTP 및 운영 배포는 이 검증에 포함하지 않습니다.

Production OpenCode에서 외부 경로의 파일 작업이 차단된 뒤 정상 제출로 복구한 경우,
차단 이력을 경고로 남기고 최종 결과 검증을 진행합니다. 파일 접근 허용 범위나 제출
hash·schema·cleanup·발송 검증은 완화하지 않습니다.

## v1.0.2 최종 승인

OpenCode v2, 공급자 표시, 요청형 이미지 생성과 공급자 실패 진단을 v1.0.2로 확정했습니다. 운영자 수동 운영테스트의 최종 합격 승인을 받았습니다. 계정별 인증·quota·모델 지원은 공급자 상태에 따라 달라지며 자동 검증 결과와 구분합니다. DB schema v3와 기존 불변 실행·발송 계약을 유지합니다. 릴리스별 자동 검증 결과는 GitHub Release의 `VALIDATION.json` 및 CI를 따릅니다.
