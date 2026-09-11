# 04. 도메인 모델

| 개념 | 의미 |
|---|---|
| Task | 고정 ID·소유자·workspace를 가진 반복 업무 |
| TaskVersion | 설정·지시문·schema·보조 파일의 검증된 불변 버전 |
| Run | 한 번의 실행과 enqueue 때 고정된 서울 논리 날짜 |
| ExecutionPlan | scope·단계별 AI 설정·원본 입력 참조를 고정한 실행 계획 |
| Claim / lease / fencing | 유효 worker의 독점 실행 소유권과 stale 실행 방어 |
| CompositionInput | 검증 record·파일·그룹 스냅샷을 결합한 불변 메일 작성 입력 |
| Handoff | 검증 원문·첨부·전달 대상 snapshot의 전달 단위 |
| SMTP attempt / receipt | 개별 전송 시도와 증거를 가진 결과 |
| Catalog entry | Task·발신 계정·그룹의 관리번호·이름·비활성 상태·불변 소유자 |
| User / session / Task grant | 로그인 계정·만료 세션·조회자 허용 범위 |

Run과 Task 버전의 과거 참조는 편집으로 변경하지 않습니다. 이름을 바꾸어도 고정 키·소유권·이력을 유지합니다. Compose-only·delivery-only는 새로운 Run이며 부모의 검증 입력 또는 원문을 재사용합니다. [수명주기](06_RUN_LIFECYCLE.md)와 [권한](WEB_UI_AUTH.md)을 참고하세요.
