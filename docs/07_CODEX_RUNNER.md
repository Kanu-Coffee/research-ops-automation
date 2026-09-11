# 07. AI runner

`codex_exec`, `antigravity_exec`, 개발용 `fake` adapter를 제공합니다. Research와 Compose는 별도 호출이며 Task의 `runner.stages`로 엔진·모델·사고 수준을 각각 선택할 수 있습니다. 기존 공통 runner 값은 단계 설정 생략 시 상속됩니다.

Production Codex는 호출별 workspace-write와 Research의 native 웹·MCP 기능을 사용합니다. Research의 승인 요청은 native on-request/auto_review 계약을 따르며 거부·timeout을 성공으로 바꾸지 않습니다. Antigravity는 호출별 자동 승인과 `--sandbox=false`를 사용하는 신뢰 운영 경로입니다. 전역 CLI 설정을 고치거나 인증 파일을 workspace에 복사하지 않습니다.

이름뿐인 sandbox 플래그·출력 상한·작업 디렉터리는 적대적 Task의 OS 격리 증거가 아닙니다. Native 실행의 한계는 [보안](12_SECURITY.md)을 따릅니다. Compose의 외부 도구 제한은 provider마다 다르므로 [MCP 계약](MCP_INTEGRATION_PLAN.md)을 확인합니다.

CLI exit 0, 도구 DONE, 모델 자기보고만으로 성공을 확정하지 않습니다. Terminal 응답, 권한 거부·pending 도구, 실제 산출물·파일 hash, 로컬 cleanup과 원격 완료 증거를 함께 확인합니다. Production 파일 참조 제출은 [RESULT_SUBMISSION](RESULT_SUBMISSION.md), 선택적 개발 검증은 [RUNNER_VALIDATION](RUNNER_VALIDATION.md)을 따릅니다.
