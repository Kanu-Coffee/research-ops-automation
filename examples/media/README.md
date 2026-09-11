# 미디어 요청 예제

`research-result.example.json`은 schema 설명용 합성 입력이다. PDF ID·hash·크기·상품과 이미지 경로는 실제 리소스가 아니며 실행하거나 운영 Task에 복사하지 않는다. 실제 요청은 도구가 반환한 document descriptor와 확인한 공식 이미지 URL을 사용한다.

예제의 두 record는 하나의 PDF를 공유한다. Dedupe가 둘 중 하나만 실제 제외하면 남은 record 연결로 PDF를 한 번 전달하고, 둘 다 제외하면 PDF 조회와 전달을 생략한다. 이미지는 첫 record에만 연결된다. 실제 파일이 확보되면 Compose는 앱이 제공한 CID를 해당 `data-record-id` 영역에서 사용한다.

PDF의 `ready`는 원천 선언이며 다운로드 성공을 뜻하지 않는다. 기본 `on_failure: continue`와 `announce_missing: false`는 파일 실패를 Run 진단에 남기고 나머지 Compose를 계속한다. Task가 파일 없이는 발송하지 말라고 요구할 때만 `on_failure: hold`, 메일에 누락을 알려야 할 때만 `announce_missing: true`를 선언한다.

이미 만들어진 로컬 파일은 `source`를 생략한다. 실행 전체에 관한 공통 첨부는 `role: attachment`, `scope: run`, `record_ids: []`이며 외부 source를 지정하지 않는다. 요청 자체가 없으면 `artifacts`를 생략하거나 빈 배열로 둔다. 앱은 문서의 주제만 보고 파일을 자동 생성·첨부하지 않는다.

계약은 [MEDIA_ARTIFACTS.md](../../docs/MEDIA_ARTIFACTS.md), 선택적 authoring 정의는 [generic-result.schema.json](../../schemas/generic-result.schema.json)의 `$defs.artifactRequests`를 따른다. 이 정의는 기존 Research 결과의 선택적 metadata를 일괄 차단하는 envelope 제약으로 연결하지 않는다.
