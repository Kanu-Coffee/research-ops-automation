# 15. 테스트와 릴리스 검증

기본 테스트는 합성 자료, 임시 DB·workspace·설정, fake runner 또는 CLI double, SMTP double을 사용합니다. 실제 업무 Task·SMTP 계정·운영 DB를 재실행하거나 migration 테스트에 연결하지 않습니다.

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen python -m compileall -q researchops
uv run --frozen researchctl validate-runners --runner fake --json
uv run --frozen python scripts/release_check.py
uv build --no-sources
```

전체 회귀는 동시 claim·fencing·버전, 결과/파일/메일 안전성, 서울 날짜·예약, SMTP 불확실성과 재시도, 인증·소유권, HTTP·backup/restore·migration을 다룹니다. Kernel·특정 외부 도구가 필요한 조건부 테스트의 skip은 통과로 집계하지 않습니다.

실제 모델 개발 검증은 별도 `--live` opt-in 명령입니다. 고정 합성 입력과 새 runtime을 사용하고 SMTP는 dry-run이지만 모델 비용과 외부 공개 자료 조회가 발생할 수 있습니다. [두 단계 엔진](RUNNER_VALIDATION.md), [native 도구](RUNNER_TOOL_VALIDATION.md), [별도 실행 경계](RUNNER_BOUNDARY_VALIDATION.md)의 목적과 증거를 구분합니다.

CLI 실행 파일을 찾거나 모델 목록을 읽었다고 추론·MCP·실발송 성공으로 간주하지 않습니다. 단위/HTTP 테스트는 실제 TLS proxy·브라우저 렌더링·모바일 키보드·호스트 재부팅 검증을 대신하지 않습니다. 해당 작업을 수행했다면 실행 환경·범위·통과/실패/skip을 구분해 기록하고 개인정보가 포함된 원문 증거는 공개하지 않습니다.

소스·문서 변경 후 배포 목록과 checksum을 갱신하려면 변경 내용을 먼저 검토하고 `uv run --frozen python scripts/release_check.py --write`를 실행합니다. 검사는 버전 일치·로컬 문서 링크·배포 inventory와 hash를 확인합니다.
