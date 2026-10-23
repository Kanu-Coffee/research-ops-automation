# 05. Task 작성

Task package는 `task.yaml`, `task.md`, `email_spec.md`와 참조 schema·보조 파일로 구성됩니다. [가상 software-releases 예제](../examples/tasks/software-releases/README.md)를 복사해 설정 계약을 확인하세요. 실제 운영 문서를 예제에 덮어써 커밋하지 않습니다.

`task.md`에는 조사 대상·기간·근거·수신자 그룹 선택 기준을 작성합니다. `email_spec.md`에는 독자·문체·제목 규칙·본문 구조·첨부 정책을 작성합니다. 실제 수신자 주소·SMTP 로그인·토큰을 두 문서에 넣지 않습니다.

`task.yaml`은 고정 ID, runner와 단계별 선택, schedule, state/dedupe, 입력·출력 경로, delivery 등을 정의합니다. 정확한 필수값과 허용 필드는 [task schema](../schemas/task.schema.json) 및 실행 가능한 예제를 따릅니다. 새 UI Task는 자동 ID와 등록 그룹명 선택을 기본으로 합니다.

Web의 새 Task는 저장만·즉시 실행·예약 중 마지막 행동을 선택합니다. 기존 Task 일반 설정 또는 고급 YAML 저장은 같은 ID의 새 불변 버전을 만들고 이력·workspace·예약 상태를 보존합니다. 고급 화면에서 소유권·Task ID를 바꿀 수 없습니다. Draft 기능은 제공하지 않습니다.

파일 패키지는 `researchctl task sync`로 검증된 후보 버전에 등록합니다. `task version list TASK_ID`, `task version dry-run TASK_ID VERSION_HASH`, `task version activate TASK_ID VERSION_HASH`로 확인·검증·활성화합니다. 개발 경로의 candidate 검사와 production UI의 검증·봉인·직접 발행을 구분합니다. 운영 설정에서는 활성화와 실행이 실제 발송에 연결될 수 있습니다.

## 선택적 HTTP와 식별자 별칭

`runner.allow_http_requests`는 boolean이며 기본값은 false다. true는 native Research의
`network_profile: public-research`와 함께 사용한다. 일반 폼의 HTTP 체크박스로 저장하며
Compose는 네트워크를 사용하지 않는다. `runner.http_proxy_profile`은 서버 설정의 profile
이름만 참조한다. 설정과 제한은 [HTTP 라우팅](HTTP_PROXY_ROUTING.md)을 따른다.

`state.dedupe.entity_aliases`는 `identities`와 `evidence`로 구성된 명시적 동등 식별자 그룹이다.
각 identity는 `key_fields` 전체를 문자열로 지정하며 그룹 간 중복은 금지한다. 그룹은 최대
128개, 그룹당 identity는 2~8개다. 앱은 이름 유사성으로 별칭을 추론하지 않으며 과거 발송
이력을 다시 쓰지 않는다. 대응 identity의 key와 content가 같은 검증된 발송 이력이 있어야
제외하며 내용 변경은 보존한다. Content dedupe의 기본값은 계속 꺼짐이다.
