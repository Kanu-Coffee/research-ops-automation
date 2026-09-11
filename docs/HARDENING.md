# 구현 불변조건

v1.0.0의 현재 코드 계약을 요약합니다. 기능 제공과 host 격리 보증을 구분하며 실제 릴리스 검증 범위는 [STATUS](STATUS.md)를 확인합니다.

| 경계 | 현재 동작 |
|---|---|
| 실행 | 원자적 enqueue·claim, lease/fencing, Task 직렬 실행, terminal·cleanup 확인 |
| 버전 | 불변 package hash, 저장 충돌 검사, 원본 Run의 설정·날짜 보존 |
| 결과 | 엄격한 JSON·고정 파일 참조, 경로/링크/크기/hash 검증, 원본 보존 |
| 업무 record | 선택 정보·coverage 부족을 warning으로 유지, content dedupe 기본 OFF |
| 메일 | record/date/CID·HTML/CSS 안전성 검사, 제목·본문 무수정 전달 |
| 파일 | 명시적 취득, 파일별 진단, Compose 입력·report·파일 hash 결합 |
| 그룹 | 소유자 범위의 불변 이름/ID snapshot, 정확한 한 그룹 해석 |
| SMTP | 보호된 계정·주소, 고정 MIME/envelope/revision, 개별 attempt/receipt, DATA 후 자동 재발송 금지 |
| Web | 관리자/사용자/조회자·불변 소유권·Task grant, 실제 peer·Host·Origin·CSRF |
| 감사 | 호출 trace와 최종 결과 예산 분리, 부분 진단과 완료 증거 구분, archive 보존 |
| 운영 | offline locked 설치, 별도 SMTP 서비스, guarded backup/restore, 전체 서비스 버전 전환 |

Production의 신뢰 운영 경로는 native Codex/Agy를 사용합니다. 개발용 output-only·native 도구·namespace/cgroup 검증 경로는 서로 다른 목적과 보장을 가지며 운영 경로의 완전한 적대적 Task 격리를 의미하지 않습니다.

세부 기준: [보안](12_SECURITY.md), [최종 제출](RESULT_SUBMISSION.md), [파일](MEDIA_ARTIFACTS.md), [그룹/로그](LARGE_TRACE_AND_RECIPIENT_ROUTING.md), [메일 재시도](EMAIL_RETRY.md), [권한](WEB_UI_AUTH.md), [단계별 AI](STAGE_MODEL_SETTINGS.md).
