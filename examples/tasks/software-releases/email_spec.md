# 가상 릴리스 다이제스트 작성 규칙

공개 배포용으로 새로 작성한 예제다. 실제 업무 문서나 이메일을 바탕으로 하지 않는다.
Compose는 봉인된 입력으로 제목, HTML, plain text를 직접 작성한다.

- 제목: `[가상 소프트웨어 릴리스] YYYY-MM-DD 업데이트`.
- 상단에 서울 업무 날짜, reportable record 수와 합성 예제임을 표시한다.
- 서비스별로 제품명, 발행 조직, 버전, release channel, 출시일과 변경 내용을 정리한다.
- record마다 최상위 HTML 요소에 정확한 `data-record-id`를 넣는다.
- optional 값이 없으면 생략하고 입력에 없는 기능·수치·근거는 만들지 않는다.
- coverage가 미완료이면 확인하지 못한 범위와 warnings를 본문 위쪽에 명확히 쓴다.
- 두 대상의 확인이 완료되고 record가 비었으면 `확인된 새 릴리스 없음`을 표시한다.
- `included_record_ids`는 입력 reportable record ID 집합과 정확히 같아야 한다.

UTF-8 `output/email.html`은 최대 폭 720px의 읽기 쉬운 표와 inline CSS를 사용한다.
외부 URL, 링크, 원격 이미지, script, form, iframe, event handler는 넣지 않는다.
검증된 CID artifact가 제공된 경우에만 해당 CID를 사용한다. 이 예제에는 첨부가 없다.
미해결 template 변수 없이 완성된 문서를 반환한다.

`output/email.txt`에는 같은 날짜·건수·서비스·변경 내용과 warnings를 평문으로 쓴다.
HTML과 text에서 record 누락, 날짜 불일치, 실제 이메일 주소가 없는지 확인한다.
앱은 검증된 원문을 수정하거나 별도 template로 다시 만들지 않는다.
