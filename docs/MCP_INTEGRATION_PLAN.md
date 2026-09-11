# Native MCP 연결과 실행 진단

ResearchOps는 각 CLI가 지원하는 기존 native MCP 설정을 사용한다. Codex와 Antigravity의
등록·인증은 별개이며 앱이 서로 복제하지 않는다. 연결은 Task의 조사에 필요한 읽기 전용
사용을 전제로 한다. 공개 [software-releases 예제](../examples/tasks/software-releases/README.md)는
별도의 사내 MCP 연결을 필요로 하지 않는다.

## 단계별 계약

| 단계 | Codex | Antigravity |
|---|---|---|
| Research·`public-research` | Native 사용자 설정의 직접 MCP·지원 Apps·설치 플러그인 사용 | 기존 native 설정·인증 사용 |
| 도구 요청 승인 | 호출별 `on-request`와 `auto_review` | 신뢰 운영 호출의 자동 승인·sandbox 해제 설정 |
| Compose·network `none` | 사용자 설정 제외, MCP·Apps·플러그인 비활성화와 관련 env 미전달 | 업무 지시 및 관찰된 원격 호출의 사후 거부 |
| 경로·결과 | Task workspace와 불변 결과 반입 계약 | 같은 결과 계약 및 생성 응답 파일의 출처 검사 |

Codex의 model·effort·출력 schema·timeout·workspace-write 설정은 호출별로 지정한다.
규칙 파일·hooks·multi-agent 등도 앱 계약에 맞게 override하므로 사용자 실행 설정 전체를
그대로 상속한다는 뜻은 아니다. 자동 심사는 거부하거나 시간 초과될 수 있다.

Agy에서 phase별 모든 MCP 원천을 예방적으로 비활성화하는 계약은 확인되지 않았다.
사후 거부는 이미 발생한 도구 효과를 되돌리지 않는다. 자동 승인과 sandbox 해제를 포함한
일반 native 운영은 신뢰된 내부 Task 작성자를 위한 모드이며 host 접근 격리의 보장이 아니다.
자세한 경계는 [보안 가이드](12_SECURITY.md)와 [운영 가이드](PRODUCTION_OPERATIONS.md)에 있다.

## 설정·환경 처리

CLI 디렉터리와 정해진 실행 경로를 사용하며 임의의 전체 환경을 그대로 전달하지 않는다.
Codex 직접 MCP 설정에서 명시한 로컬 env 참조를 필요한 범위로 전달한다. SMTP·앱 내부·
SSH·실행 환경 변조에 쓰일 보호 env는 제외하고 작업 셸은 최소 환경을 사용한다.
Agy에는 연결용 token env를 광범위하게 추가 주입하지 않는다. Native 서버의 inline env와
인증 처리는 해당 CLI의 계약을 따른다.

일반 진단은 설정 파일에서 서버명·전송 방식·활성 상태·도구 정책·필요한 env 이름·실행 파일
존재 여부만 정제해 반환한다. URL·token·header·env 값·실행 인자·설정 원문·인증파일을
진단 요약이나 worker 입력에 넣지 않는다. 등록 목록이 존재한다고 연결 성공으로 판정하지 않는다.

## Doctor와 Run 화면

Doctor/System Health의 목록은 현재 진단 프로세스가 확인한 직접 등록 설정이다.
`connections_checked=false`, `inventory_complete=false`를 유지한다. Apps·플러그인·
profile·시스템 설정의 모든 원천을 완전히 열거한 목록이 아니며 진단 과정에서 서버에 접속하지
않는다. 선택 연결의 문제만으로 전체 실행 readiness를 실패로 만들지 않는다.

Run 화면은 관찰된 provider·서버·도구·성공/실패/거부/응답 미확인 상태와 안전한 오류 코드를
보여준다. 일반 요약에는 도구 인자·응답 본문·원본 오류를 넣지 않으며 생략된 항목 수를
표시한다. 과거 Run에 새 필드가 없으면 연결 실패로 바꾸지 않는다. `DONE`, CLI exit 0,
전체 Run 성공은 개별 MCP 응답 검증과 같은 증거가 아니다.

Agy 생성 schema와 `instructions.md`는 활성 직접 등록 서버 또는 실제 호출에서 관찰한
서버에 연결된 경로만 인정한다. `output.txt`·`content.md`는 현재 conversation에서 앞서
성공한 해당 MCP/웹 호출의 정확한 응답 경로와 순서를 대조한다. 다른 conversation·미래
step·실패 호출·설정·인증 파일·임의 parent 디렉터리 접근을 허용하지 않는다.

## 연결 확인과 한계

기존 CLI에서 원하는 서버의 등록·인증 상태를 운영자가 준비한 뒤 앱의 등록 메타데이터와
선택한 Task의 실제 Run 증거를 비교한다. CLI별 신규 설정 반영 방식과 shared daemon의
캐시 동작은 다를 수 있다. 앱은 영구 MCP enable/disable이나 공유 daemon 재시작을 phase
전환 수단으로 사용하지 않는다.

독립 개발 검증에는 [native 도구 검증](RUNNER_TOOL_VALIDATION.md)의 합성 stdio MCP 또는
공개 문서 MCP 시나리오를 사용할 수 있다. 이 검증이 모든 기업 connector·OAuth·개인 계정의
접근을 보장하지 않는다. 실제 검증 결과는 [STATUS](STATUS.md)와 해당 실행의 report를 따른다.
