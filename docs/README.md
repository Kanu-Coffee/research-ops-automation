# 문서 목차

## 사용과 운영

| 문서 | 내용 |
|---|---|
| [설치](USER_DEPLOYMENT.md) | 사용자 소유 디렉터리와 systemd 서비스 |
| [운영 가이드](PRODUCTION_OPERATIONS.md) | 발신·수신 설정, Task 저장, 실행·예약, 진단 |
| [로그인과 권한](WEB_UI_AUTH.md) | 최초 관리자, 역할·소유권, 세션·복구 |
| [백업과 업그레이드](BACKUP_AND_UPGRADE.md) | 자료 보존, 오프라인 복원, 같은 버전으로 서비스 전환 |
| [Reverse proxy](../deploy/nginx/README.md) | TLS, loopback/원격 proxy, Host·peer·CSRF |
| [릴리스 범위](STATUS.md) | v1.0.0 기능·검증 범위·알려진 제약 |
| [설계 결정](DECISIONS.md) | 현재 구현의 주요 선택과 이유 |

## 제품·구조·개발

[01 제품 개요](01_PRODUCT_BRIEF.md) · [02 범위와 원칙](02_SCOPE_AND_PRINCIPLES.md) · [03 아키텍처](03_ARCHITECTURE.md) · [04 도메인](04_DOMAIN_MODEL.md) · [05 Task](05_TASK_DEFINITION.md) · [06 Run 수명주기](06_RUN_LIFECYCLE.md) · [07 Runner](07_CODEX_RUNNER.md) · [08 결과와 메일](08_RESULT_AND_RENDERING.md) · [09 SMTP·비밀정보](09_EMAIL_AND_CREDENTIALS.md) · [10 저장·로그](10_ARTIFACTS_STATE_LOGS.md) · [11 예약](11_SCHEDULING_AND_OPERATIONS.md) · [12 보안](12_SECURITY.md) · [13 CLI·Web](13_CLI_AND_WEB_UI.md) · [14 후속 개발](14_IMPLEMENTATION_ROADMAP.md) · [15 테스트](15_TESTING_STRATEGY.md) · [16 수용 기준](16_ACCEPTANCE_CRITERIA.md) · [17 배포](17_DEPLOYMENT.md) · [18 CLI 호환성](18_OPENAI_REFERENCE_NOTES.md) · [19 개발 가드레일](19_DEVELOPMENT_GUARDRAILS.md)

## 전문 계약

| 문서 | 내용 |
|---|---|
| [HARDENING](HARDENING.md) | 구현 불변조건과 핵심 검증 경계 |
| [STAGE_MODEL_SETTINGS](STAGE_MODEL_SETTINGS.md) | 단계별 AI와 재실행 계획 |
| [RESULT_SUBMISSION](RESULT_SUBMISSION.md) | 엄격한 JSON·파일 참조 제출·진단 |
| [LARGE_TRACE_AND_RECIPIENT_ROUTING](LARGE_TRACE_AND_RECIPIENT_ROUTING.md) | 로그 예산과 그룹 이름 해석 |
| [MEDIA_ARTIFACTS](MEDIA_ARTIFACTS.md) | PDF·이미지 취득, 파일 진단, v4 전달 |
| [RESEARCH_FILE_ACCESS](RESEARCH_FILE_ACCESS.md) | Research 중 파일 활용과 출처 보존 |
| [RESEARCH_CONTEXT_AND_HISTORY](RESEARCH_CONTEXT_AND_HISTORY.md) | 조사 기간과 검증된 전달 이력 |
| [EMAIL_RETRY](EMAIL_RETRY.md) | 전송 전용 실행, 자동·수동 SMTP 재시도 |
| [RUN_TIMELINE](RUN_TIMELINE.md) | 실행·모델 호출·전송 시간 해석 |
| [MCP_INTEGRATION_PLAN](MCP_INTEGRATION_PLAN.md) | Native MCP 연동과 제공자별 제약 |
| [RUNNER_VALIDATION](RUNNER_VALIDATION.md) | 합성 Research/Compose 검증 |
| [RUNNER_TOOL_VALIDATION](RUNNER_TOOL_VALIDATION.md) | 선택적 native 도구 검증 |
| [RUNNER_BOUNDARY_VALIDATION](RUNNER_BOUNDARY_VALIDATION.md) | 별도 namespace/cgroup 개발 검증 |

파일명의 숫자와 `PLAN`은 문서 링크 호환을 위해 유지합니다. 내용은 v1.0.0의 현재 계약이며 특정 운영환경의 설치·사고 이력을 포함하지 않습니다.
