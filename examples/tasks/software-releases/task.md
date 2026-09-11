# 가상 소프트웨어 릴리스 조사

이 문서는 공개 배포를 위해 새로 작성한 합성 학습용 프로젝트다. Atlas Notes와
Cedar Board는 이 예제 안에서만 존재하는 가상 서비스이며 실제 운영 Task를 표현하지 않는다.
`example.org` 경로는 예약된 예시 주소다. 기본 `fake` runner는 동봉 fixture를 읽으므로
웹 접속, AI 호출, 이메일 발송 없이 흐름을 확인할 수 있다. 실제 조사에 적용할 때는
운영자가 소유한 별도 Task에서 주제와 검증할 공식 출처를 새로 지정한다.

## Research

대상은 가상 Atlas Notes와 Cedar Board 두 서비스다. Atlas Notes 2.4.0의 노트 내보내기
기능 추가 한 건과 Cedar Board의 변경 없음 상태를 fixture로 제공한다.

- 입력의 서울 업무 날짜를 사용한다. 현재 시각으로 실행 날짜를 다시 정하지 않는다.
- `output.schema.json`에 맞는 JSON을 반환한다. record의 최소 필드는 `publisher`,
  `product_name`, `release_channel`이며 `release_channel`은 `stable`, `preview`, `unknown`이다.
- `record_type: software_release`, `release_version`, `release_date`, `changes`, `sources`,
  `notes`로 근거와 내용을 표현한다. 모르는 선택 값은 꾸며내지 않는다.
- 각 대상의 coverage를 보고한다. 확인하지 못한 대상은 `blocked` 또는 `unavailable`로
  남기고 `partial`과 warnings를 사용한다. 두 대상이 모두 확인되고 변경이 없을 때만
  `no_updates`를 반환한다.
- 실제 이메일 주소, 자격증명, 수신자 선택을 Research 결과에 넣지 않는다.
- 기본 예제에는 이미지와 첨부가 없다. 자료가 없는 경우 파일을 생성했다고 주장하지 않는다.

## Compose

봉인된 `composition-input.json`만 업무 자료로 사용하고 다시 조사하지 않는다.
모든 `reportable_records`와 coverage warnings를 보존한다. content dedupe는 기본 off다.
활성화한 경우에도 앱이 verified-sent exact unchanged로 제외한 항목을 되살리지 않는다.

다음 opaque 그룹 ID 중 정확히 하나를 선택한다. 실제 membership을 추론하지 않는다.

- `release-stakeholders`: reportable record 중 `release_channel: preview`가 하나 이상일 때.
- `release-team`: 그 외 일반 릴리스, 변경 없음 또는 부분 확인 보고.

`email_spec.md`에 따라 완성된 `output/email.html`과 `output/email.txt`를 작성한다.
최종 composition JSON은 `subject`, `html_path: email.html`, `text_path: email.txt`,
단일 `recipient_group_id`, 선택 근거인 `recipient_group_reason`, 그리고 정확한
`included_record_ids`를 포함한다. 입력에 없는 record나 사실을 더하지 않는다.

## 실행 경계

현재 Task의 공용 project와 현재 phase staging에서만 작업한다. 다른 Task, 앱 DB,
run archive, 전달 설정과 CLI 인증 파일에 접근하지 않는다. worker는 발송하지 않는다.
앱이 결과의 구조·파일 안전성·전달 무결성을 검사하며, 검증한 메일 원문을 다시 작성하지 않는다.
