# 합성 Research/Compose 파이프라인 검증

`validate-runners`는 배포본에 포함된 합성 입력으로 Research → Compose → 기술 검증 →
archive 흐름을 점검하는 개발 명령이다. 일반 운영 Task의 실행 선행조건은 아니다.
운영 실행은 [운영 가이드](PRODUCTION_OPERATIONS.md)의 신뢰 운영자 모드를 따른다.

## 실행

```bash
# 모델과 SMTP를 호출하지 않는 파이프라인 검증
uv run --frozen researchctl validate-runners --runner fake --json

# 실제 CLI는 사전 진단만 수행하며, 미실행 case는 blocked로 표시
uv run --frozen researchctl validate-runners --json

# 선택한 엔진으로 한 합성 입력의 실제 Research/Compose 호출
uv run --frozen researchctl validate-runners --live \
  --runner codex_exec --sample numeric-compare --json
```

`--runner`는 `fake`, `codex_exec`, `antigravity_exec` 중 선택하며 반복할 수 있다.
`--sample`도 반복할 수 있고, 생략하면 다음 네 입력을 사용한다.

| 입력 | 확인하는 동작 |
|---|---|
| `document-extract` | 합성 문서의 구조화 결과와 원문 값 보존 |
| `numeric-compare` | 합성 수치 비교와 결과값 보존 |
| `partial-coverage` | 일부 조사 결과 및 coverage·warning 보존 |
| `no-updates` | 기록이 없는 결과의 Compose·메일 계약 |

`--model`은 선택한 provider가 지원하는 모델을 명시할 때만 사용한다. 생략하면 CLI의
기본 선택을 따른다. `--timeout-seconds`는 phase별 1~300초이며 기본 120초다.
모델 지정과 기본값이 아닌 timeout은 `--live`가 필요하다. `--output-parent`는 이미
존재하는 증거 부모 디렉터리이며 그 아래 새 private 디렉터리를 만든다.

## 입력·실행 경계

명령은 운영 `--config`를 거부하고 독립 runtime을 생성한다. 합성 Task는 비활성 상태이며
전달은 dry-run이다. 기존 운영 Task·DB·예약·발신 설정을 검증 대상으로 사용하지 않는다.
실제 호출은 기존 CLI 로그인을 보호된 control process에서 사용한다. 인증파일을 Task
workspace에 복사하거나 symlink하지 않는다.

실제 모델 검증은 output-only adapter를 사용한다. Codex는 read-only 설정과 도구 비활성화,
Agy는 sandbox 요청 및 관찰된 동작 검사를 적용한다. 두 엔진의 도구 계약은 같지 않다.
반환 JSON·고정 파일명·입력/결과 identity·출력 상한·HTML/text 계약·archive hash와
process 종료·lease 해제를 검사한다. 반환한 본문은 다시 렌더링하지 않는다.

## 결과 해석

종료 코드는 전체 통과 `0`, 검증 실패 `1`, 미실행·선행조건 미충족 case가 있으면 `78`이다.
기본 전체 matrix에는 실제 모델을 호출하지 않은 blocked case가 포함되므로, 무모델 CI에는
`--runner fake`를 명시한다. 각 실행의 `report.json`에서 case별 검사와 원본 증거 경로를
확인한다. CLI 기동 수, 확인된 terminal 응답 수, 알 수 없는 provider 내부 요청 수를 구분한다.

Fake 통과는 실제 모델 품질의 증거가 아니다. 실제 합성 호출 통과 역시 임의 Task의 host
파일·자격증명·네트워크·quota 격리를 입증하지 않는다. terminal 또는 cleanup이 확인되지
않으면 관련 후속 호출을 보류한다. 이 릴리스의 실제 검증 결과는 [STATUS](STATUS.md)에
기록하며, 이 문서는 과거 운영 실행이나 특정 호스트의 성공 기록을 포함하지 않는다.

Native 도구 검증은 [RUNNER_TOOL_VALIDATION](RUNNER_TOOL_VALIDATION.md), 생성 코드의
별도 격리 실행은 [RUNNER_BOUNDARY_VALIDATION](RUNNER_BOUNDARY_VALIDATION.md)을 따른다.
