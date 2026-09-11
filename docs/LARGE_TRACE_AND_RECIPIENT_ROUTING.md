# 대용량 trace와 수신자 그룹명 계약

Production의 원본 로그 수집 예산은 최종 결과 반입 예산과 별도입니다. 완료된 호출의 부분 진단을 보존하고, Compose가 선택한 이름은 소유자 범위의 불변 그룹 스냅샷에서 정확한 ID로 해석합니다.

## 1. 로그와 최종 결과의 별도 예산

다음은 Production Research/Compose **호출별** 기본값이다. `settings.yaml`의 `runner`
설정으로 조정하며 이벤트/미리보기 제한은 전체 trace 제한을 초과할 수 없다.

| 설정 | 기본값 | 측정 범위 |
|---|---:|---|
| `trace_max_bytes` | 67,108,864 bytes (64 MiB) | stdout+stderr 원본 합계, 개행 포함 |
| `trace_max_event_bytes` | 8,388,608 bytes (8 MiB) | stdout JSONL 이벤트 하나, 끝 개행 제외 |
| `trace_max_events` | 10,000 | 비어 있지 않은 JSONL 이벤트 |
| `trace_preview_bytes` | 65,536 bytes (64 KiB) | stdout/stderr 각각의 미리보기 |
| 최종 구조화 응답·결과 반입 | 1,000,000 bytes | 기존 결과 계약 유지, trace 한도와 독립 |

원본 stdout/stderr는 worker 밖 애플리케이션 감사 영역의 제한된 파일에 순차 저장한다.
`RunnerExecutionResult`는 파일 참조·크기와 작은 문자열 미리보기를 제공한다. UTF-8 문자의
중간에서 끊긴 바이트도 원본 파일에는 그대로 남기며 미리보기만 표시용으로 디코딩한다.
Archive 복사·SHA-256 계산·다운로드도 전체 로그를 한 문자열로 읽지 않고 스트리밍한다.

Production parser는 이벤트 하나씩 처리한다. 모든 응답 본문을 누적하지 않고 lifecycle,
권한 거절, 도구 호출 집계와 generated-file 출처 검사에 필요한 상태를 유지한다. 원본
바이트로 예산을 계산하므로 한글을 ASCII escape로 다시 직렬화한 크기로 거부하지 않는다.
이는 workspace filesystem quota나 전체 native 도구의 격리 완료를 의미하지 않는다.

## 2. 중단 진단과 성공 검증

- 한도 초과·timeout·취소·잘린 마지막 행·잘못된 이벤트·파일 기록 실패에도 앞서 확인한
  완료 호출을 보존한다. 서버·도구·이벤트 크기·전체 집계, 진행 중 호출, 중단 원인과
  해석된 범위를 남긴다. 공개 호출 목록은 최대 500건이며 전체 집계와 구분한다.
- 부분 진단을 근거로 terminal 성공이나 결과 반입을 인정하지 않는다. 진단 실패를
  “모델 호출 없음”으로 단정하지 않으며 UI는 완료 호출과 불완전 trace를 구분한다.
- 수집/해석 실패 뒤에는 실패한 parser나 파일 기록기를 정리 과정에서 다시 호출하지
  않고 pipe를 비운다. 최초 오류와 실제 process-group/pipe/reap/원격 종료 증거를 따로
  남긴다. Cleanup 결과를 무조건 성공으로 바꾸거나 원래 오류를 정리 오류로 덮지 않는다.
- Agy terminal 없는 실행의 원격 종료 미확인 기준은 유지한다. 뒤늦은 `denied_actions`,
  generated-file 출처 제한과 큰 MCP 응답의 `isError` 판정도 유지한다. 큰 로그를 허용해도
  권한 거절이나 잘못된 결과를 통과시키지 않는다.
- 공통 Research 지침은 실제 지원되는 인자로 작은 페이지·좁은 범위부터 조회하고 필요한
  근거를 추가 조회하도록 한다. 응답의 생략 표시는 경고로 보존한다. 앱은 이를 이유로
  읽을 수 있는 업무 record를 버리거나 불완전한 원문을 완전한 근거로 표시하지 않는다.

## 3. 이름 선택과 불변 ID 해석

`delivery.recipient_routing_mode`는 `catalog_name` 또는 `legacy_ids`다. 신규 UI는
`catalog_name`을 기본으로 사용하고 필수 단일 그룹 picker 대신 정확한 등록 이름 목록을
보여준다. 새 모드의 Task에는 고정 `allowed_recipient_group_ids`를 저장하지 않는다.

CompositionInput v3는 생성 당시 **Task 소유자에게 속하고 활성·유효한 수신자 매핑이 있는 모든 그룹**의
`{recipient_group_id, display_name}`을 `recipient_groups`에 저장한다. 내부 호환용
`allowed_recipient_group_ids`는 이 스냅샷의 ID에서 파생한다. 실제 주소·멤버십·자격증명은
입력이나 worker workspace로 전달하지 않는다. 발신 계정은 기존 Task의 opaque
`sender_profile_id`로 고정되며 모델이 선택하지 않는다.

Compose worker는 `task.md`의 규칙에 따라 `recipient_group_name` 문자열 하나와
`recipient_group_reason`을 반환한다. Composition schema는 이름을 요구하되 고정 enum이나
const로 등록 목록을 봉인하지 않는다. 앱은 앞뒤 공백을 제거한 이름이 스냅샷에서 **정확히
하나**와 일치할 때만 ID로 연결한다. 미등록·중복 이름·복수 선택은 명확한 구성 오류이며
유사 이름, 첫 그룹 또는 ID로 자동 대체하지 않는다. Research에는 수신자 선택을 넣지 않는다.

그룹 이름이 실행 도중 바뀌어도 저장된 스냅샷을 재작성하거나 다른 ID로 해석하지 않는다.
Worker가 만든 원본 `composition-result.json`, 제목·HTML·plain text는 그대로 보존한다.
내부 `CompositionResult`와 handoff/SMTP에는 해석한 기존 ID를 사용하고, 별도
`recipient_resolution` 증거에 반환 이름·확정 ID·run·Task 버전·composition revision 및
입력/원본 결과 SHA-256을 저장한다.

Handoff와 SMTP는 최신 결과를 임의 조회하지 않고 해당 run·Task 버전·revision의 저장된
입력·결과·hash를 검증한다. 현재 ID의 활성 상태와 주소 매핑을 다시 확인하며 기존 SMTP
설정 revision·본문 hash·취소·중복 방지·DATA 이후 불확실 메일 자동 재발송 금지를 유지한다.

## 4. 호환과 검증

기존 모드 생략 Task 및 보존 중인 과거 Draft 자료와 `legacy_ids`는 기존 ID enum, CompositionInput v2를 지원한다.
기존 단일 그룹 API 호출은 legacy를 유지하고, 편집/복제 저장에서 모드가 생략되면
현재 모드를 보존한다. Compose-only는 기존 v2/v3 입력과 원래 논리 날짜를 재사용한다.
모드 변경은 새 불변 Task 버전으로 처리하고 사용자 문서·다른 schema 제약·보조 파일을
유지한다. 수신자 필드를 참조하는 중첩 조건은 자동 삭제하지 않고 명시적 고급 schema
편집이 필요한 오류로 처리한다.

수용 기준은 다음과 같다. 실제 통과 여부는 [STATUS](STATUS.md)와 CI/Release 결과에서 확인한다.

| 검증 | 확인할 결과 |
|---|---|
| 완료 MCP 호출 뒤 잘린 이벤트 | 완료 호출 진단·불완전 호출·실제 종료 원인 보존, 결과 반입/handoff 없음 |
| 대형 이벤트 여러 건·이전 소규모 로그 예산 초과 | Research→Compose 성공, 원본 hash 일치, 로그 합계에 비례한 메모리 누적 없음 |
| 정확한 상한·1 byte/1 event 초과·큰 stderr | 독립 예산 경계, 최초 실패와 실제 cleanup 보존 |
| 한글/emoji 절단·잘못된 JSON·timeout·취소·쓰기 오류 | 원본 bytes와 부분 진단 보존, 불완전 성공 거부 |
| Codex wire와 Agy 큰 오류/denial/파일 출처 | 기존 엔진 호환 및 종료/권한 경계 유지 |
| Task별 조건·새 그룹·미등록/중복 이름 | 입력 생성 시점의 전체 목록과 유일한 이름 해석 |
| 이름/삭제/매핑 변경·다른 run/revision | 저장 스냅샷 ID 유지, 현재 발송 경계 재검증·교차 결과 거부 |
| 기존 Task/보존 자료/clone/compose-only·SMTP double | v2/legacy 호환, 전달 ID와 메일 내용 동일성 |

실제 모델 검증이 필요하면 독립 합성 runtime에서 지원되는 native HTTP/STDIO 경로를 선택하고 실제 서버 receipt·원본 bytes·terminal·cleanup을 구분해 확인한다. 특정 provider에서 신규 설정 반영이 확인되지 않으면 전역 설정이나 공유 daemon을 변경해 우회하지 않는다. 기본 회귀는 대형 이벤트 fixture·CLI double·SMTP double을 사용하고 기존 업무 Task와 실제 SMTP를 실행하지 않는다.

설치 시 `uv --link-mode copy`를 사용해 cache hardlink와 singly-linked file 검사의 충돌을 피한다. 검증 실패 파일을 허용하도록 링크 검사 자체를 완화하지 않는다. 업그레이드·원문 보존은 [백업과 업그레이드](BACKUP_AND_UPGRADE.md)를 따른다.
