# 05. Task 작성

Task package는 `task.yaml`, `task.md`, `email_spec.md`와 참조 schema·보조 파일로 구성됩니다. [가상 software-releases 예제](../examples/tasks/software-releases/README.md)를 복사해 설정 계약을 확인하세요. 실제 운영 문서를 예제에 덮어써 커밋하지 않습니다.

`task.md`에는 조사 대상·기간·근거·수신자 그룹 선택 기준을 작성합니다. `email_spec.md`에는 독자·문체·제목 규칙·본문 구조·첨부 정책을 작성합니다. 실제 수신자 주소·SMTP 로그인·토큰을 두 문서에 넣지 않습니다.

`task.yaml`은 고정 ID, runner와 단계별 선택, schedule, state/dedupe, 입력·출력 경로, delivery 등을 정의합니다. 정확한 필수값과 허용 필드는 [task schema](../schemas/task.schema.json) 및 실행 가능한 예제를 따릅니다. 새 UI Task는 자동 ID와 등록 그룹명 선택을 기본으로 합니다.

Web의 새 Task는 저장만·즉시 실행·예약 중 마지막 행동을 선택합니다. 기존 Task 일반 설정 또는 고급 YAML 저장은 같은 ID의 새 불변 버전을 만들고 이력·workspace·예약 상태를 보존합니다. 고급 화면에서 소유권·Task ID를 바꿀 수 없습니다. Draft 기능은 제공하지 않습니다.

파일 패키지는 `researchctl task sync`로 검증된 후보 버전에 등록합니다. `task version list TASK_ID`, `task version dry-run TASK_ID VERSION_HASH`, `task version activate TASK_ID VERSION_HASH`로 확인·검증·활성화합니다. 개발 경로의 candidate 검사와 production UI의 검증·봉인·직접 발행을 구분합니다. 운영 설정에서는 활성화와 실행이 실제 발송에 연결될 수 있습니다.
