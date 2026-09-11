# 요청한 PDF·이미지 확보와 전달 계약

Research가 명시적으로 요청한 PDF·이미지를 앱이 확보하고, 같은 파일과 record 연결을 Compose·handoff·SMTP까지 검증하는 계약이다. CardRAG PDF는 선택적 source protocol이며 일반 소프트웨어 릴리스 예제에는 해당 서비스나 인증이 필요하지 않다.

## 요청과 책임

Research worker는 `task.md`에 따라 `result.json.artifacts`에 필요한 파일만 요청한다. 앱은 업무 record를 재판정하거나 문서 주제만으로 이미지·PDF를 자동 첨부하지 않는다. 파일 요청이 없거나 배열이 비어 있으면 최종 첨부를 위한 조회를 하지 않고 Run에도 누락 파일 섹션을 만들지 않는다.

Research 실행 중 `acquire_file(source)`는 원본을 읽고 가공하기 위한
선택적 경로다. 중간 취득도 예산과 원장에 보존하며 취득만으로 첨부하지 않는다. 최종 원본
요청은 같은 source를 유지하고 검증된 원본을 재사용한다. 로컬 파생 파일의 선택적
`derived_from`과 원장 hash 결속, 재작성에서의 원본 보존은
[RESEARCH_FILE_ACCESS](RESEARCH_FILE_ACCESS.md)를 따른다. 기존 후취득 경로는 유지한다.

요청에는 안전한 상대 `path`, `role`, 선택적인 `artifact_id`, 상품을 연결하는 `record_ids`, 원천 상태인 `declared_status` 또는 기존 `status`를 넣는다. `role`은 `attachment`, `inline_image`, `evidence`다. `ready`는 원천에서 준비됐다는 선언이며 실제 파일이 확보됐다는 증거가 아니다. 파일 이름과 주변 record 순서로 상품 관계를 추측하지 않는다.

`source`가 없으면 현재 Research output 안에 worker가 이미 만든 로컬 파일이다. 외부 source는 다음 두 형식만 지원하며 worker가 임의 헤더·token·인증 파일 경로를 전달할 수 없다.

| source 종류 | Research 요청 필드 | 앱의 검증 |
|---|---|---|
| `cardrag_pdf` | `connection_id`, `document_id`, `issuer`, `product_code`, `sha256`, `size_bytes` | 등록된 보호 설정, 원천 metadata와 descriptor의 일치, 실제 PDF MIME·내용·hash·크기 |
| `official_image` | 공개 공식 이미지의 HTTPS `url` | 정확한 허용 host, 목적지·redirect·응답 상한, 실제 PNG/JPEG/GIF 형식 |

CardRAG 요청은 `role: attachment`, `mime_type: application/pdf`와 실제 도구가 반환한 descriptor를 사용한다. document ID는 `doc_`와 64자리 소문자 hex이며 hash·크기·상품 식별자를 임의 생성하지 않는다. 공식 이미지의 request URL과 달리 확정 report/Compose source에는 query·userinfo·fragment를 제거한 URL과 원 요청 URL의 `url_sha256`을 넣는다. 일반 Run 화면에는 source metadata를 출력하지 않는다.

[generic-result.schema.json](../schemas/generic-result.schema.json)의 `$defs.artifactRequests`는 새 요청의 선택적 authoring/lint 계약이다. 기존 Research envelope의 `artifacts`는 계속 비차단 metadata다. 형식이 다른 과거 선택 정보 때문에 읽을 수 있는 record를 조용히 삭제하지 않는다. 정상화된 파일 요청이 확보 단계에 도달하면 source·경로·연결·실제 byte 안전성은 별도의 필수 검증을 받는다. [합성 예제](../examples/media/research-result.example.json)는 source 요청과 공유 record 연결을 보여준다.

## 보호된 PDF 연결

`media.providers.<connection_id>`는 앱 설정의 고정 `base_url`과 `bearer_token_file` 참조로 구성한다. Base URL은 DNS 이름이나 임의 원격 주소가 아닌 literal loopback HTTP origin이다. 앱은 사전에 등록된 origin에 다음 순서로만 인증 GET을 수행한다.

1. `/resources/documents/{document_id}`를 조회하고 `document_id`, `issuer`, `product_code`, `pdf_sha256`, `pdf_size_bytes`가 요청 descriptor와 모두 같은지 확인한다.
2. 일치한 경우 `/sources/{document_id}/pdf`를 조회하고 PDF 형식·hash·크기를 다시 확인한다.

두 GET은 하나의 파일 timeout을 공유하며 재시도하지 않는다. PDF endpoint의 redirect도 허용하지 않는다. HTTP 인증은 고정 loopback 경계 안에서만 사용한다. worker 입력·Task package·Compose에는 origin, 실제 token, token 파일 경로를 넣지 않는다. 전역 CLI 로그인이나 인증 helper를 읽어 연결을 자동 구성하지 않는다.

현재 CardRAG API는 검색 당시 generation/revision을 고정하는 요청 계약을 제공하지 않는다.
문서·상품 ID와 원본 hash/크기의 일치를 검사하며 검색 세대 고정 검증을 제공하지 않는다.
CardRAG 서버나 CLI의 인증 설정을 변경하지 않는다.

Token 참조는 task/output 경계 밖의 절대 경로이며 앱 transport만 읽는다. 파일과 상위 경로의 symlink를 거부하고 singly linked regular file, 소유권·권한·읽는 동안의 변경을 검사한다. 일반적인 지원 권한은 앱 사용자 소유 `0600` 또는 root 소유 읽기 전용 `0440`이다. group 쓰기와 world 접근, token의 개행 헤더 주입은 허용하지 않는다. 이 보호된 transport 프로세스는 Research/Compose AI worker와 구분된다.

공식 이미지 조회는 기존 public fetch transport를 사용한다. Task의 허용 network profile과 앱에 등록된 정확한 공식 host가 모두 필요하다. 임의 외부 인증이나 사용자가 지정한 header는 지원하지 않는다.

공식 이미지의 기본 호스트 목록은 `researchops/config.py`의 `OFFICIAL_IMAGE_HOSTS`에 정의되어 있다. 운영 설정에 `media.official_image_hosts`를 명시하면 기본값과 병합하지 않고 그 목록을 사용한다. 사용할 공식 source의 정확한 HTTPS 호스트만 등록하고 임의 하위 도메인·검증되지 않은 redirect를 허용하지 않는다. 기본 목록이 모든 분야의 이미지 호스트를 제공하는 것은 아니다.

## 예산과 실패

예산은 전체 요청에 공유하며 설정 상한을 임의로 넘기지 않는다. `MiB` 외의 byte 표기는 십진 byte다.

| 설정 | 기본값 | 설정 가능한 범위 |
|---|---:|---|
| `media.max_total_bytes` | 20,000,000 bytes | 양수, 최대 20,000,000 |
| `media.max_files` | 64 | 1–64 |
| `media.max_image_bytes` | 4 MiB | 양수, 최대 4 MiB |
| `media.file_timeout_seconds` | 20초 | 양의 정수, 최대 30초 |
| `media.phase_timeout_seconds` | 120초 | 양의 정수, 최대 600초 |
| `media.retries` | 0 | 0만 지원 |
| `media.mime_reserve_bytes` | 2,000,000 bytes | 기본값 이상 |
| `media.mime_part_header_bytes` | 파일당 4,096 bytes | 기본값 이상 |
| `delivery.max_message_bytes` | 20,000,000 bytes | 실제 SMTP MIME에도 적용 |

MIME 사전 계산은 파일별 base64의 `4 × ceil(bytes / 3)`, 76열마다 붙는 CRLF, 파일별 header 여유와 본문 reserve를 합산한다. 이 계산은 Compose 이전의 보수적 선택 예산이다. 완성된 SMTP MIME 직렬화 후 실제 byte 크기를 enqueue 전과 전송 전에 다시 검사한다. 본문과 header가 reserve보다 큰 경우에도 실제 상한을 넘긴 메일은 발송하지 않는다. 최종 `composition-result.json`의 기존 1,000,000-byte 상한은 별도로 유지한다.

파일을 확보하지 못하면 기본 `on_failure: continue`로 해당 파일을 Compose 입력에서 제외하고 나머지 record·파일 처리를 계속한다. 예를 들어 원천 401/403/404, timeout, metadata/hash/크기 불일치, 형식 오류, 예산 초과는 고정 `reason_code`를 남긴다. Task가 해당 파일을 필수로 요구할 때만 Research 요청에 `on_failure: hold`를 넣는다. 이 경우 확보 단계 뒤 `needs_attention`으로 중단하여 Compose와 handoff를 진행하지 않는다.

기본 `announce_missing: false`는 실패 진단을 메일 본문에 강제로 넣지 않는 계약이다. Task가 수신자에게 누락을 알리라고 요구하는 경우만 `true`를 반환한다. Compose worker가 그 지침에 따라 본문을 작성하며 앱이 문장을 삽입하지 않는다. 운영 파일 경고는 Run 진단에 남기고 원본 Research warnings나 worker가 작성한 제목·HTML·text를 수정하지 않는다.

경로 탈출·예약 파일 덮어쓰기·symlink/hardlink·중복 식별자·미등록 record 연결·확정 로컬 파일 변조는 안전성 실패로 중단한다. `continue`가 이 검증을 완화하지 않는다. 취소와 transport cleanup 실패도 종료 원인과 완료된 진단을 보존한다. 네트워크에서 처음 받은 파일의 불일치와 이미 확정한 파일의 변경을 구분한다.

## Dedupe와 파일의 범위

Content dedupe 기본 OFF와 verified-sent에 대한 정확한 내용 일치 판정은 그대로다. 파일 선택은 실제 제외 결과를 사용한다.

| 범위 | 동작 |
|---|---|
| `scope: record` | 요청 `record_ids`와 이번 reportable record의 교집합을 남긴다. 하나 이상 남으면 같은 파일을 한 번 포함하며, 모두 제외되면 원격 조회·Compose 포함을 생략한다. |
| `scope: run` | 명시적인 실행 공통 로컬 `attachment` 또는 `evidence`다. `record_ids`는 생략하거나 `[]`, `source`는 생략하거나 `null`이어야 한다. 상품 dedupe와 별개로 보존하며 `evidence`는 메일 첨부·inline 목록에 넣지 않는다. |
| 기존 연결 없는 `legacy` | 기존 로컬 파일을 지원한다. Dedupe ON만으로 제외하지 않고, 실제 record 제외가 생긴 경우 연결을 알 수 없는 전달용 파일을 제외한다. 연구 evidence는 보관한다. |

공유 PDF의 일부 상품만 제외되면 남은 상품 연결을 유지한다. 업무 record 자체를 파일 실패 때문에 삭제하지 않는다. 이미 검증한 연구 파일은 Compose에서 제외되어도 연구 보관 영역에 남길 수 있다. 파일 취득·전달 제외와 연구 증거 보존은 별도 판정이다.

`no_updates`, `records: []`인 실행도 로컬 조회 이력 JSON을 `scope: run`, `role: evidence`로 선언할 수 있다. 앱은 파일 형식·경로·hash·크기를 검증해 `research-artifacts/`에 보존하고, Compose에는 진단 원장과 빈 첨부 목록을 전달한다. `run` 범위의 inline 이미지·외부 source·상품 record 연결은 계속 거부한다.

## Compose와 전달 무결성

요청 진단이 있는 입력은 `CompositionInput v4`를 사용한다. `catalog_name`과 `legacy_ids` 모두 v4에 같은 artifact 계약을 적용한다. 파일 요청이 없는 기존 입력의 v2/v3와 두 수신자 라우팅 방식의 호환을 유지한다.

v4 입력에는 reportable records, 선택된 `attachments`/`inline_artifacts`, `artifact_report`가 함께 봉인된다. 선택 파일은 `artifact_id`, `path`, `mime_type`, 실제 `sha256`·`size_bytes`, 남은 `record_ids`, 확정 source를 유지한다. 인라인 이미지는 검증된 CID를 제공하며 Compose HTML의 해당 `data-record-id` 영역에서 참조해야 한다. 원격 이미지 URL을 HTML resource로 직접 넣지 않는다.

MessageValidator는 report와 선택 파일의 일치, record 연결·CID 위치, 모든 파일의 hash·크기를 검사한다. 원본 `composition-result.json`과 worker 제목·HTML·text는 보존하고 `composition-binding.json`에 해당 run·Task version·composition revision과 입력·원본 결과·report·파일의 hash를 결속한다. Handoff와 SMTP는 동일 저장 revision과 원본 archive를 다시 대조하고 승인된 파일 목록을 그대로 MIME으로 만든다. 다른 run/revision의 결과 대체, 검증 후 파일 변경, 임의 첨부 추가·제거를 허용하지 않는다. 기존 수신자 해석·취소·중복 방지·DATA 후 불확실 발송의 자동 재발송 금지는 유지한다.

Compose-only는 부모 입력·report·파일 hash를 검증한 뒤 기존 byte와 파일 선택을 재사용한다. 원천을 다시 조회하거나 현 시점 이름·파일로 바꾸지 않는다. 자식 report는 새 run/revision identity와 `derived_from`의 부모 run/revision/report hash를 보존한다. 변조된 부모 입력이나 파일은 worker 호출 전에 중단한다. 기존 v2/v3 Compose-only 경로도 지원한다.

## Run 진단과 보관

`artifact-report.json` schema 1은 run·Task·Task version hash·composition revision과 최대 64개 요청 entry를 가진다. 각 entry는 원천 `declared_status`, 실제 `status: available|failed|excluded`, 고정 `reason_code`, 원래 `requested_record_ids`와 남은 `record_ids`, `include_in_compose`, `announce_missing`, 확보한 파일 hash·크기를 보존한다. 미확보 hash·크기는 `null`이다.

새 원장의 선택적 `requested_scope`는 요청한 범위를 보존하며, 생략된 범위는 `null`이다. 기존 원장에는 이 필드가 없어도 된다. 역할·범위 오류는 `invalid_artifact_role`, `invalid_artifact_scope`, `invalid_artifact_role_scope`로 구분한다. 실패 진단의 요청 범위를 `legacy`로 재해석하지 않으며, 원본 실패 Run과 archive는 수정하지 않는다.

새 report는 요청의 `on_failure`도 보존하며 과거 이 필드가 없는 report와 호환한다.

Run 화면은 원천의 `ready`, 당시 확보 결과, 현재 archive의 다운로드 가능 여부를 구분한다. `available` 기록이라도 이후 파일이 없어지거나 hash가 바뀌면 현재 다운로드 검증 실패로 표시한다. 반대로 전달에서 제외된 파일도 실제 연구 보관 파일의 hash가 report 및 archive index와 일치하면 다운로드할 수 있다. source URL·token·임의 응답 metadata는 일반 진단에 표시하지 않는다.

Report가 없거나 요청이 0개면 누락을 추측하지 않는다. Report 형식·identity·hash를 검증할 수 없으면 “진단 확인 불가”와 원본 확인 안내를 표시한다. 안전한 상대 경로의 한글·공백 파일명은 URL encoding과 RFC5987 `filename*`로 다운로드하며 traversal·잘못된 UTF-8·중복 encoding·header 제어문자는 거부한다.

## 구현 위치와 검증

`engine/artifact_acquirer.py`가 요청 준비·dedupe 연결·확보·진단을 담당한다. `runners/protected_media_fetch.py`가 보호된 PDF metadata/원본 GET을, `runners/research_fetch.py`가 공식 이미지의 공개 조회를 담당한다. `engine/composition_input.py`, `engine/message_validator.py`, `delivery/artifact_integrity.py`와 SMTP dispatcher가 저장된 v4 입력과 실제 전달 byte를 검증한다. `services/run_service.py`와 `web/artifact_views.py`는 안전한 Run 진단만 제공한다.

자동 회귀는 합성 HTTP/PDF/PNG와 SMTP double로 수행한다. 관련 묶음은 `test_artifact_acquirer`, `test_artifact_composition`, `test_media_pipeline`, `test_artifact_report_web`이며 실제 모델·운영 Task·실제 메일 발송은 이 fixture 검증에 포함하지 않는다. Schema 예제와 legacy envelope 호환도 별도로 검사한다. 공개 릴리스의 검증 범위는 [STATUS](STATUS.md)와 해당 CI/Release 기록에서 확인한다.
