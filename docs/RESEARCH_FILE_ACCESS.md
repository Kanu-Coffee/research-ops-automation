# Research 실행 중 원본 파일 활용

Research worker는 `task.md`의 지시에 따라 원본 PDF·공식 이미지를 확보하고 기존 로컬 도구로
읽거나 가공할 수 있다. 앱은 파일 취득·반입·출처와 전달 무결성을 담당하고, 그림의 선택과
업무상 적절성은 Task 문서와 worker가 판단한다. Research/Compose 두 단계는 유지한다.

## 호출과 파일의 구분

Production Research의 `public-research` 호출에는 결과 제출 helper와 함께 다음 함수를
제공한다. Codex와 Agy에 같은 계약을 적용하며 Compose와 offline 호출에는 제공하지 않는다.
호출별 안내가 지정한 `.researchops-submit`을 Python import 경로에 추가한다.

```python
from researchops.runners.file_acquisition import acquire_file

acquired = acquire_file(source)
if acquired["status"] == "available":
    original_path = acquired["path"]
    # Task가 요구하는 로컬 도구로 읽기/가공한다.
```

`source`는 [기존 미디어 계약](MEDIA_ARTIFACTS.md)의 `cardrag_pdf` 또는
`official_image` 형식이다. 등록된 연결과 허용 호스트만 사용하며 추가 인증 인자나 MCP
설정은 받지 않는다. 보호 취득 프로세스만 기존 자격증명을 사용한다. Worker에는 토큰,
인증 파일 또는 원본 감사 영역의 경로를 제공하지 않는다.

성공 응답은 `status`, `acquisition_id`, 읽을 수 있는 작업용 사본의 `path`, `mime_type`,
실제 `size_bytes`·`sha256`를 가진다. 실패 응답은 `status: failed`와 고정 `reason_code`를
반환한다. 필요 여부와 실패 후 업무 처리는 Task가 결정한다. 실패 상세에 외부 응답 본문이나
자격증명을 포함하지 않는다.

파일 채널은 현재 Research staging의 `.researchops-files/{requests,responses,files}`다.
앱은 Run·attempt·fencing 소유권에 결속한 원장과 검증된 원본을 worker staging 밖의
`research-acquisition/ledger.json`, `research-acquisition/originals/`에 보존한다.
Worker 사본·응답 파일·worker가 선언한 ID 자체는 취득 증거가 아니다. 같은 UID의 신뢰 운영
모드이며 이 경로 분리가 적대적 작업의 OS 강제 격리를 뜻하지는 않는다.

## 결과 반입과 출처

취득만으로 첨부가 결정되지 않는다. 원본을 첨부하려면 기존처럼 `result.json.artifacts`에
원래 `source`를 선언한다. 앱은 현재 원장의 source와 실제 원본 hash를 대조해 재사용한다.
원격 source의 최종 출력 경로에 worker가 파일을 먼저 만들어 두면 계속 거부한다.

추출한 이미지는 최종 output 아래 별도 경로에 만들고 기존 로컬 `inline_image`로 제출한다.
원본 작업용 사본을 덮어쓰지 않는다. 선택적 `derived_from: [acquisition_id, ...]`는 현재
세션에서 성공한 취득만 참조할 수 있다. 앱은 이를 원본 hash와 결속한 참조로 확정한다.
추출 페이지·영역·선택 근거는 Task가 요구하는 연구 evidence에 작성한다.

파일의 경로·링크·형식·용량·hash와 record/CID 연결은 기존 검증을 적용한다. 모든 파일을
검증한 뒤 Compose 입력을 봉인하며 Compose는 확정된 목록만 사용한다. PDF 추출기,
상품별 이미지 판별기, 강제 이미지 개수 또는 추가 AI 단계는 앱에 만들지 않는다.

원장이 있는 새 입력의 `artifact_report.acquisition_evidence`에는 생산 `run_id`, `attempt`,
정확한 원장 bytes의 `ledger_sha256`를 넣는다. Compose-only와 delivery-only는 DB에 고정된
입력 hash, 생산 Run의 동일 Task 버전·조상 관계, 원장 및 원본의 archive index/hash를 확인한
뒤 원본 bytes를 자식 archive에 보존한다. 모델 변경이나 재시도로 출처·날짜를 새 값으로
교체하지 않으며 네트워크를 재호출하지 않는다. 기존 출처 참조 없는 입력은 계속 지원한다.
새 업무용 DB 테이블이나 과거 archive backfill은 없다.

## 예산·수명·오류

요청은 직렬 처리한다. 동일 source의 성공과 실패를 해당 실행에서 재사용하며 실패를
자동 재다운로드하지 않는다. 같은 문서/URL에 서로 다른 descriptor가 들어오면 차단한다.
요청/응답은 각각 16 KiB, 요청 건수는 `4 × media.max_files` 이내다.

최종 결과에서 빠진 중간 취득도 기존 `media.max_files`, `media.max_total_bytes`에 포함한다.
원본을 최종 첨부로 재사용할 때는 이중 계산하지 않으며 파생 파일은 별도 파일로 계산한다.
다운로드 도중 timeout 등으로 수신량을 확정할 수 없으면 예약한 수신 상한을 보수적으로
차감한다. 원장에 실제 관찰 또는 예약 계산 여부를 보존한다.

`media.phase_timeout_seconds`는 다운로드 활성 시간의 합계에 적용한다. Worker가 PDF를
읽거나 가공하는 시간은 원래 Research 전체 timeout을 따른다. 종료·취소 시 요청 접수를
닫고 transport를 취소·회수한 뒤 원장을 확정한다. 완료 파일과 실패 근거는 보존한다.
정리를 확인하지 못하면 `needs_attention`과 기존 lock/lease를 유지하고 pending archive를
보존한다. 정리 미확인 상태를 성공 처리하거나 쓰기 가능한 archive를 확정하지 않는다.

## 검증

`tests/research_file_fixture.py`는 대상 도표와 무관한 그림을 함께 넣은 합성 PDF다.
`test_research_file_pipeline`은 CLI double을 사용한 양 provider adapter에서 helper → 보호 HTTP 취득 →
`pdfimages` 추출 → artifact → Compose CID → SMTP double의 동일 bytes를 검사한다.
Task의 지시만 바꾼 일반 문서 도표와 상품 그림 사례는 같은 앱 코드를 사용한다.

`test_research_acquisition`, `test_research_acquisition_runner`, `test_acquisition_reuse`,
`test_acquisition_evidence`는 중복·실패 재요청, 중간 파일 예산, 변조·위조·경로·소유권,
취소·timeout·정리 실패와 재작성/전송 전용 출처 보존을 검사한다.
`python -m tests.live_research_file_validation --live --provider ... --root ...`는 명시적
합성 native 검증이다. 기존 CardRAG 서버·업무 자료·실제 SMTP를 사용하지 않는다.
공개 릴리스의 검증 범위는 [STATUS](STATUS.md)와 해당 CI/Release 기록에서 확인한다.
