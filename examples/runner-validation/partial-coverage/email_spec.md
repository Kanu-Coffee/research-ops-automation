# 합성 검증 메일 계약

- UTF-8의 완성된 HTML document와 plain text를 생성한다. 템플릿 placeholder를 남기지 않는다.
- HTML은 정확히 하나의 html/head/body로 구성한다. head에 `<meta name="researchops-local-date" content="run.local_date의 실제 값">` 하나를 넣는다.
- 각 record는 body의 section 한 곳에 `data-record-id="실제 record_id"`를 정확히 한 번 넣는다. 빈 records이면 record marker 없이 변경 사항 없음이라고 쓴다.
- 한국어와 수치·날짜·부호를 그대로 보존한다. partial일 때는 경고와 확인 불가 범위를 HTML/text에 모두 넣는다.
- script, iframe, form, event handler, remote image, CSS url/import, 외부 stylesheet, data URI를 사용하지 않는다.
- 이 task에는 inline/attachment artifact가 없으므로 이미지·CID 참조를 추가하지 않는다.
- 제목은 합성 검증 자료임과 canonical 서울 날짜를 명시한다. 개행·제어문자를 넣지 않는다.
- composition-result.json은 recipient_group_id, recipient_group_reason, subject, html_path=email.html, text_path=email.txt, included_record_ids만 가진다.
- 결과를 메일로 보내지 않는다. 애플리케이션의 보호된 내장 SMTP 영역도 이 샘플에서는 dry-run이다.
