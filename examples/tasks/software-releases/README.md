# Software releases: 합성 학습용 Task

Atlas Notes와 Cedar Board라는 가상 서비스의 릴리스 다이제스트다. 공개 릴리스를 위해
새로 작성했으며 운영 Task 문서, 실제 사용자, 고객, 이메일 또는 서비스 자료를 포함하지 않는다.
`example.org`의 경로는 문법 설명용이며 실제 릴리스 페이지가 아니다.

`task.yaml`은 예약 OFF, `fake` runner, network `none`, dry-run이다. fixture 한 건으로
Research → 검증 → Compose → archive 흐름을 로컬에서 확인한다. 실제 모델·발송을
사용할 때는 별도 Task를 만들고 주제·출처·수신자·발신 계정을 직접 설정한다.

- `task.md`: 조사 범위, coverage와 수신자 선택 규칙.
- `email_spec.md`: AI가 완성하는 HTML/plain text 규칙.
- JSON schemas: 최소 구조 계약. 추가 업무 필드는 원문대로 보존한다.
- `fixtures/`: 전부 합성인 Research/Compose/legacy delivery receipt 예제.

`release-team`, `release-stakeholders`, `researchops-admins`는 예시 opaque 그룹 ID다.
실제 주소는 앱의 보호된 delivery 설정에만 둔다. 기본 content dedupe는 OFF이며,
별도 버전에서 켰을 때에도 verified-sent와 내용이 정확히 같은 record만 제외한다.

`sample-compose-input.json`은 worker용 사본 형태다. sample delivery/receipt는
legacy filesystem gateway 계약을 설명하며 현재 SMTP 발송 성공 증거가 아니다.
동일 예제가 저장소의 `tasks/software-releases/`에도 있으며 배포 패키지에는
`examples/tasks/software-releases/`가 포함된다. 사용 절차는 루트 README를 따른다.
