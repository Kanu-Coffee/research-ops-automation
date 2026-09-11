# 13. CLI와 Web

두 인터페이스는 `services/application.py`의 공통 서비스를 사용합니다. Web 변경 요청은 로그인·역할·소유권·CSRF 검사를 거쳐 큐/설정을 변경하며 HTTP 요청 안에서 장시간 모델이나 SMTP를 실행하지 않습니다.

```bash
researchctl --help
researchctl doctor --json
researchctl task list --json
researchctl task show TASK_ID --json
researchctl task version list TASK_ID --json
researchctl run exec TASK_ID
researchctl run list --task TASK_ID --json
researchctl run show RUN_ID --json
researchctl run cancel RUN_ID
researchctl run compose-only RUN_ID
researchctl worker run --concurrency 1
researchctl delivery worker
researchctl web serve
```

`--config PATH` 또는 `RESEARCHOPS_CONFIG`로 설정을 명시합니다. 운영 설정의 `run exec`·retry·예약은 실제 발송과 연결될 수 있습니다. `--dry-run`은 전달 모드이며 모든 모델 호출까지 fake로 바꾸는 옵션은 아닙니다.

Web은 대시보드, Task 목록·편집·고급 YAML, Run 요약·이메일·파일·로그, 발신 계정·그룹과 사용자 관리 화면을 제공합니다. 새 Task 기본 행동은 저장만입니다. 기존 Task 저장은 새 불변 버전이고 Draft UI/CLI는 없습니다. 조회자는 명시적으로 허용된 Task만 볼 수 있습니다.

자세한 사용 흐름은 [운영](PRODUCTION_OPERATIONS.md), [권한](WEB_UI_AUTH.md), [단계별 AI](STAGE_MODEL_SETTINGS.md), [전송 재시도](EMAIL_RETRY.md)를 따릅니다.
