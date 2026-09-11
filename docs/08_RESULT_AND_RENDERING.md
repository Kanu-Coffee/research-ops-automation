# 08. 결과와 이메일 검증

Research 결과는 구조화 record와 출처·warning·artifact 요청을 담습니다. 구조와 파일 안전성은 필수 검증이며 선택 정보·coverage 부족·의심 중복만으로 읽을 수 있는 record를 삭제하지 않습니다. Content dedupe는 기본 OFF이고 명시적으로 켠 경우에만 검증된 과거 발송의 정확한 key/content와 비교합니다.

Compose는 불변 입력의 record·논리 날짜·허용 그룹·검증 파일을 사용해 제목·HTML·plain text를 작성합니다. 앱은 HTML/CSS·URI·record/date/CID 대응, UTF-8·크기·파일 hash와 수신자 선택을 검사합니다. 안전하지 않은 본문을 자동 sanitize해 통과시키지 않습니다.

통과한 제목·HTML·plain text는 archive에 원문으로 남기고 같은 내용과 hash로 handoff·SMTP에 전달합니다. MIME 직렬화에 필요한 encoding과 header는 SMTP 영역이 처리하지만 본문을 다른 템플릿으로 다시 만들지 않습니다. 미리보기는 제한된 정책으로 표시하며 실행 가능한 HTML/SVG artifact는 다운로드로 제공합니다.

세부 계약: [파일 참조 제출](RESULT_SUBMISSION.md), [미디어 v4](MEDIA_ARTIFACTS.md), [그룹 이름 해석](LARGE_TRACE_AND_RECIPIENT_ROUTING.md), [SMTP](09_EMAIL_AND_CREDENTIALS.md).
