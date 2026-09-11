# 단계별 AI 설정과 재실행

메일 작성·검증이 끝난 뒤 발송 전에 실패했다면 Run의 **작성된 이메일 보내기**를
사용한다. Research/Compose를 호출하지 않는 `delivery_only` 실행이며, 전송 전용
재시도도 같은 범위를 유지한다. 과거 Task 버전의 발송 호환성 검사와 원문 보존 계약은
[EMAIL_RETRY](EMAIL_RETRY.md)를 따른다.

Task의 Research와 Compose는 각각 AI 엔진·모델·사고 수준을 선택한다. Web 생성·편집·복제는 같은 두 카드와 카탈로그를 사용한다. Research 설정 복사는 명시적이며 이후 두
단계는 독립적이다. Task 저장은 새 불변 버전을 만들고 기존 실행의 설정을 바꾸지 않는다.

## 모델 선택

Codex는 읽기 전용 app-server model/list의 모델별 effort를 사용한다. Agy는 models 목록의
실제 변형 ID를 묶어 표시하며 family/effort 선택을 정확한 ID와 null effort로 정규화한다.
따라서 지원 여부가 확인되지 않은 --effort 조합을 만들지 않는다. Singleton은 기본값만
표시하며 family에 기본 수준 근거가 없으면 사용자가 수준을 선택해야 한다.

캐시는 보호된 data/model-catalog에 저장한다. GET은 캐시만 읽고, CSRF를 통과한 명시적
새로고침이 metadata CLI를 호출한다. 15분은 목록 freshness 기준이며 오래됨·조회 실패는
마지막 정상 목록과 기존 선택을 지우지 않는다. Codex app-server 조회 실패 시 CLI의
models_cache.json에서 허용된 모델 metadata만 읽을 수 있다. 인증 파일을 읽거나 worker에
복사하지 않으며 thread/turn/model 호출은 하지 않는다. 목록 노출은 계정 quota나 모델
추론 성공의 보증이 아니다.

새 선택은 카탈로그에서 검증한다. 같은 원본에 저장된 미등재 선택은 경고와 함께 보존할
수 있지만 임의 새 모델을 legacy라고 제출해서 승인받을 수 없다. CLI 기본값과 실제 CLI가
보고한 모델은 구분하며, 관찰 근거가 없으면 실제 모델/effort는 미확인으로 둔다.

## Task와 Run 계약

기존 runner 운영 옵션은 유지한다. 선택적 runner.stages.research/compose는 각각
type/model/reasoning_effort만 보유하며 생략 시 기존 공통 runner 설정을 상속한다.
Web의 새 저장은 두 단계를 명시하고 공통 type/model/effort는 Research 값에 맞춘다.
기존 Task 버전과 보존 중인 과거 Draft 자료는 일괄 변환하지 않는다.

run_execution_plans는 Run 등록과 같은 transaction에서 scope·stages·selection_source·
source_composition 및 계획 hash를 고정한다. 실행 중 설정 변경 API는 없다. 기존 Run은
원본 Task 버전으로 해석하고 과거 archive를 수정하지 않는다. 원본 Task 버전의 timeout,
네트워크/파일/수신자/전달 계약은 그대로이며 override는 AI 선택 세 값에 한정한다.

Run 상세의 과거 설정은 읽기 전용이다. 재실행 패널은 이전 실행 설정에서 시작하고
현재 Task 설정은 명시적으로 불러온다. 이때 폼에 복사한 값만 적용하며 POST 시 최신
Task 설정으로 조용히 대체하지 않는다. 메일 재작성에서는 Compose만 편집·전송한다.

full은 Research부터 재실행한다. compose_only는 확정된 입력을 재사용하고 Compose만
실행한다. 실패한 recompose의 기본 재시도도 compose_only이며, 전체 실행은 명시적
선택이 필요하다. 메일 전송만 재시도하는 SMTP 기능은 AI를 호출하지 않는 별도 경로다.

입력 출처는 직접 부모 이력과 분리한 source run ID/revision/input_sha256으로 고정한다.
Compose 준비 중 실패해 자기 입력을 저장하지 못한 경우 이전 계획의 source를 승계한다.
조상 관계·Task 버전·입력/hash·원본 archive 및 첨부 검증을 유지한다. 재작성 revision은
부모/source revision보다 증가하며 서울 논리 날짜는 바꾸지 않는다.

같은 request key와 같은 선택 요청은 카탈로그가 나중에 바뀌어도 기존 Run을 반환한다.
설정이 다른 요청은 충돌이며 Run/계획/요청 저장 실패는 함께 rollback한다. 기존 SMTP
불확실·진행 중 발송·실행 claim에 대한 중복 방지 정책을 유지한다.

## API·CLI

retry_run은 optional scope/execution_settings/selection_source를, compose_only는 optional
execution_settings/selection_source를 받는다. execution_settings는 단계별 완전한 세 값이며
선택하지 않은 단계는 이전 Run 설정을 상속한다. Web과 CLI가 동일 서비스를 사용한다.

CLI retry에는 --scope full 또는 compose_only를 사용할 수 있다. retry/compose-only 모두
--research-provider/model/effort, --compose-provider/model/effort를 받는다. model/effort에
default를 지정하면 CLI 기본값이며 provider 변경 시 이전 provider의 모델/effort를 초기화한다.
compose-only에서 Research 설정 변경은 거부한다.

## 한도 오류

Agy Individual quota reached는 quota_exhausted로 분류하고 안전하게 추출한 reset 초수를
보조 진단으로 남긴다. 완료된 ERROR와 로컬 cleanup이 확인된 경우 quota 오류를 exit 1,
response_missing보다 우선 표시한다. Timeout/cleanup 미확인과 입력 반입 차단 정책은
완화하지 않는다. 다른 엔진으로의 전환·업무 재실행·메일 재발송은 자동으로 수행하지 않는다.

## 검증 경계

회귀는 독립 합성 runtime, native adapter의 CLI double, SMTP double/dry-run을 사용한다. Provider quota 실패 후 다른 AI 설정을 선택한 Compose-only에서 Research를 다시 호출하지 않는지, source 입력·날짜·원문·권한·요청키가 보존되는지 검사한다. 실제 업무 모델 호출이나 기존 Run의 실제 이메일 발송을 회귀 검증에 사용하지 않는다. 최종 검증 범위는 [STATUS](STATUS.md)와 해당 CI/Release 기록에서 확인한다.
