# 18. Native CLI 호환성 점검

ResearchOps의 실행 계약은 설치된 CLI의 capability와 이 저장소 adapter를 기준으로 합니다. `codex --version`, `codex exec --help`, `agy --version`, `agy --help`를 각 운영 계정에서 확인합니다. 문서에 적힌 provider 모델명이나 플래그가 모든 CLI 버전에 그대로 적용된다고 가정하지 않습니다.

Codex adapter는 non-interactive 실행, JSONL 이벤트와 최종 structured response를 사용합니다. 모델 카탈로그는 app-server model/list의 메타데이터를 읽고, 가능한 경우 마지막 정상 캐시를 유지합니다. 인증 파일을 모델에 넘기거나 메타데이터 조회를 실제 추론 성공으로 집계하지 않습니다.

Research와 Compose는 별도 invocation입니다. Fresh session이어도 Task project는 Run 사이에 유지될 수 있으므로 대화 수명과 filesystem 수명을 구분합니다. 최종 응답은 [고정 파일 참조](RESULT_SUBMISSION.md)로 엄격하게 반입합니다.

실제 사용되는 명령 조립은 `researchops/runners/commands.py`, capability 조회는 `researchops/runners/probe.py`, 모델 선택은 `researchops/services/model_catalog.py`에 있습니다. 변경 시 native 승인·sandbox·MCP·terminal 이벤트를 각각 검증하고 전역 설정을 자동 변경하지 않습니다. [Runner 검증](RUNNER_VALIDATION.md)과 [MCP 계약](MCP_INTEGRATION_PLAN.md)을 참고하세요.
