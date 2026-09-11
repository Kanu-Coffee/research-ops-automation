# 고정 조사 기간과 검증된 전달 이력

`state.research_context`는 날짜·자료 범위·시리즈를 불변 실행 입력으로 전달하는 선택 기능이다.
현재 `active_issuers` 값은 CardRAG의 canonical issuer 코드에 맞춘 확장 계약이다. 일반
소프트웨어 조사 Task에 임의의 프로젝트 코드를 넣는 범용 필터로 사용할 수 없다.
[software-releases 예제](../examples/tasks/software-releases/README.md)는 이 확장을 요구하지 않는다.

일반 Task에서는 해당 설정을 생략하거나 비활성화한다.

```yaml
state:
  research_context:
    enabled: false
```

## 활성화할 때의 설정

| 필드 | 계약 |
|---|---|
| `enabled` | 명시적 boolean |
| `active_issuers` | 지원되는 canonical 코드의 중복 없는 목록 |
| `reference_date` | ISO 날짜 또는 `null`; null/생략은 원래 서울 논리 업무일 |
| `lookback_days` | 정수 1~3660; 기준일을 포함한 조회 일수 |
| `series_id` | 영문 소문자·숫자로 시작하는 1~64자의 안전한 시리즈 식별자 |
| `delivery_history` | 검증된 전달 이력을 함께 제공할지 지정하는 boolean |

시작일은 기준일에서 일수−1을 뺀 날이고 종료일은 기준일이며 양 끝을 포함한다.
잘못된 날짜·목록·알 수 없는 설정은 오류로 거부한다. 날짜 변경은 새 Task 버전으로 저장한다.
현재 수록 자료를 해당 기간으로 조회하는 계약이며 과거 시장의 완전한 snapshot을 보장하지 않는다.

`research-context.json`은 Task/version·origin/parent run·논리 날짜·기준일 출처·자료 범위·
포함 기간·일수를 담는다. Research와 Compose, archive 및 불변 composition input에 같은
값을 보존한다. Task package가 이 파일이나 `delivery-history.json`을 대체할 수 없다.
Retry는 실제 부모 체인과 원래 날짜를 대조하고, Compose-only는 부모의 저장된 hash와
입력 bytes를 확인하여 재사용한다.

## 전달 이력의 신뢰 근거

`delivery-history.json`은 같은 Task·시리즈에 대해 불변 package·run/version/revision,
composition input과 archive bytes, record 집합, delivery request·HTML/text hash,
파일 binding, 원본 receipt hash와 local SMTP attempt를 대조한 투영이다.

| 상태 | 의미 |
|---|---|
| `smtp_accepted` | 모든 RCPT와 최종 DATA 응답이 수락됨; 수신함 도착 보장 아님 |
| `failed_not_sent` | 앱이 미발송을 확인함 |
| `uncertain` | 전송 결과가 불확실하여 자동 재발송 금지 |
| `unresolved` | 필요한 증거가 없거나 결합·무결성을 확인하지 못함 |

주소·자격증명·MIME bytes·원문 메일·서버 응답은 worker용 투영에 포함하지 않는다.
다른 시리즈는 제외하고 시리즈를 확인하지 못한 과거 자료는 불완전 이력으로 표시한다.
최대 500건·전체 256KiB 제한에 진단도 포함한다. 누락이나 한도 초과를 성공·미발송으로
추정하지 않는다. SMTP 재시도 chain과 대기 중인 재시도도 원본 메시지 identity로 확인한다.

구형 package는 봉인된 `task.md`의 유일한 구조화 설정에서 시리즈가 확인된 경우에만
호환 투영할 수 있다. 모델 summary·project 원장·이름이나 시각의 유사성은 receipt의
신뢰 근거가 아니다. 특정 운영 Task를 변경하는 일회성 migration은 공개 배포본에 없다.

## 선택적 project 원장 대조

앱은 같은 시리즈의 `sent-products.json`을 최대 2MB로 읽고, 원장 hash와 정확한
run/revision·record/product·HTML/text hash가 맞을 때 `ledger_reconciliation` 제안을
제공한다. 동일 receipt를 중복 적용하지 않고 같은 ID의 다른 hash는 충돌로 보존한다.
앱은 project 원장을 직접 수정하지 않는다. Task가 worker에게 제안 적용을 지시한다면
적용 전에 원장 hash를 다시 확인해야 한다.

이 기능은 [content dedupe](HARDENING.md)나 [SMTP 재시도](EMAIL_RETRY.md)의 안전 계약을
대체하지 않는다. 개발 검증은 독립 runtime·합성 데이터·SMTP double로 수행하며, 실제
검증 범위는 [STATUS](STATUS.md)에서 확인한다. 운영 지시문과 운영 이력은 예제에 포함하지 않는다.
