# Run 실행 시간과 단계별 진단

Run의 등록·대기·실행·전송 시간을 구분해 표시합니다. 관리 이벤트 개수는 모델 호출 수나 전체 실행 단계 수와 다르며 단계별 타임라인과 별도로 해석합니다.

## 화면

- 등록·시작·종료 시각을 서울 시간으로 표시하고 대기·실행·전체 경과 시간을 구분한다.
  메일 전달 대기(`awaiting_receipt`)는 실행 전체의 완료가 아니다. 실제 SMTP 처리 후
  저장한 종료 시각까지 진행 중으로 표시한다.
- 단계별 시작·종료·소요 시간과 상태를 펼쳐 본다. 실행 준비, Research, 결과 검증,
  dedupe, 파일 취득, Compose 입력 준비, Compose, 메일 검증, 전달 준비, 결과 보관,
  SMTP 처리에 실제 발생한 기록만 사용한다. Compose-only에서 생략된 조사를 수행했다고
  표시하거나 실패 이후의 단계를 자동으로 완료 처리하지 않는다.
- 관련 단계에서 record·파일·첨부 수, 로그 byte 수와 exit code·cleanup 확인 등
  제한된 진단 수치를 확인한다. Research/Compose 로그 및 파일/MCP 진단으로 연결한다.
  응답 본문, command 인자, 주소, 자격증명, fencing token은 타임라인에 넣지 않는다.
- 관리 이벤트는 타임라인과 구분하고 표시 상한과 생략 건수를 알린다. 상태 API의
  `timeline_revision`이 바뀌면 같은 coarse phase 안에서 진행돼도 화면을 새로 갱신한다.
- 과거 Run의 DB에 있는 시작·종료 시각은 바로 표시한다. 단계별 시각이 없는 과거 기록은
  **상세 단계 기록 없음**으로 안내한다. 원본 로그에 없는 시각을 현재 시간이나 파일의
  수정 시각으로 추정해 넣지 않는다. 중단된 단계는 종료 미기록을 명확히 표시한다.

## 기록 계약

기존 `audit_events`에 run 소유의 `run_step_started`와 `run_step_finished`를 추가한다.
별도 DB migration이나 기존 행의 backfill은 없다. payload는 `schema_version: 1`,
`step_id`, `phase`, `label`, `state`, `attempt`, UTC `started_at`/`finished_at`,
`duration_ms`, 허용된 정수 `summary`만 가진다.

Worker의 단계 전환은 현재 run·attempt·lease·task claim의 fencing을 검증한 transaction에서
직전 단계 종료, 다음 단계 시작과 기존 coarse phase 갱신을 함께 기록한다. 진행 중인
단계는 시작 기록만으로 조회할 수 있다. 실패·timeout·취소·cleanup 미확인은 해당 실제
단계에 남긴다. 진단 쓰기 실패가 원래 오류나 cleanup 실패를 성공으로 바꾸지 않는다.

`timeline.json`은 새 Run archive의 보관 직전 스냅샷이며
`snapshot_scope: before_archive_publication`을 명시한다. 따라서 그 파일의 결과 보관
단계는 진행 중일 수 있다. 실제 보관 성공 이벤트와 Run DB 종료 시각은 archive 공개
후의 finalization transaction에 남긴다. 보관 실패 또는 DB commit 실패를 보관 성공으로
선언하지 않으며, 기존 archive를 완료 이벤트로 사후 수정하지 않는다.

SMTP의 대기·claim·DATA 시작·완료 이벤트는 기존 queue transaction 및 claim token 검증과
함께 기록한다. 완료 상태는 서버 수락, 실패, 취소, 전달 불확실을 구분한다. 관리용 시험
메일·연결 검사·별도 system alert를 업무 Run의 SMTP 단계로 오인하지 않는다. SMTP 시작은
실제 claim 시각이며 대기 등록 시각을 시작 시각으로 대체하지 않는다. 시작이 없는 구버전
작업이나 취소된 대기 작업은 시작·소요 시간을 미확인으로 둔다.

## 시간의 의미와 한계

- Worker 단계 시간은 UTC 관찰 시각과 monotonic 경과 시간으로 기록한다.
- native 도구·MCP 시간은 앱이 완전한 JSONL 시작/완료 이벤트를 받은 시각 기준이다.
  공급자 서버 내부 실행 시간이나 순수 네트워크 시간은 아니다. 반복 ACTIVE 이벤트는
  최초 시작을 바꾸지 않는다. 완료만 관찰하면 종료만, 중단되면 관찰한 시작만 남긴다.
  기존 500건 공개 목록 제한과 전체 호출 집계, 대용량 로그 예산은 유지한다.
- SMTP 시간은 재시작 이후에도 남는 DB의 UTC claim/완료 시각 차이다. 시계 역행처럼
  차이를 신뢰할 수 없으면 소요 시간만 미확인으로 두고 실제 완료 기록을 유지한다.
- 프로세스 강제 종료·lease 소실로 종료 이벤트가 없으면 상세 완료를 추정하지 않는다.
  Run 최종 상태와 해당 단계의 종료 미기록을 함께 확인한다.

공개 릴리스 검증 범위는 [STATUS](STATUS.md)와 해당 CI/Release 기록에서 확인한다. 운영 Task 재실행·실제 메일 발송을
개발 검증에 사용하지 않으며 기존 Task·예약·SMTP 원장·실패 archive는 보존한다.
