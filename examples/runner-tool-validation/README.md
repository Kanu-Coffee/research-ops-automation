# Runner tool validation 시나리오

신뢰된 합성 task를 통해 실제 Codex·Antigravity의 파일·코드·웹·MCP 활용을 점검하는 개발용 시나리오 계약이다. 기존 `examples/runner-validation/`의 output-only Research/Compose 전체 엔진 검증과 구분한다. Fake 결과나 모델의 자기보고만으로 native tool 지원을 표시하지 않는다.

## 실행

기본 실행은 모델 프로세스를 시작하지 않는다. 실제 실행에는 `--live`가 필요하며, 지정 provider·시나리오는 지원 계약과 실제 권한에 따라 성공·실패·차단을 명시한다.

```bash
uv run --frozen researchctl validate-runner-tools --json
uv run --frozen researchctl validate-runner-tools --live --runner antigravity_exec --scenario file-write --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario file-code --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario code-repair --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario web-search --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario mcp-research --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario mcp-public-docs --timeout-seconds 180 --json
uv run --frozen researchctl validate-runner-tools --live --runner codex_exec --scenario combined-research --timeout-seconds 180 --json
```

`--runner antigravity_exec`도 provider 선택값이다. 명령을 받아들인다는 사실은 모든 시나리오가 그 provider에서 통과한다는 뜻이 아니다. native 명령·MCP 권한 거부가 발생하면 별도 증거로 보존하며, 미승인 권한 우회나 성공한 다른 실행자로 조용히 대체하지 않는다.

Agy 도구 요청 자동 승인을 명시적으로 선택하는 시험:

```bash
uv run --frozen researchctl validate-runner-tools --live --runner antigravity_exec --agy-dangerously-skip-permissions --scenario mcp-research --timeout-seconds 180 --json
```

`--agy-dangerously-skip-permissions`는 기본 off이며 `--live`와 명시적 Antigravity 단독 선택이 필요하다. 해당 Agy 세션의 모든 도구 요청이 자동 승인되므로 좁은 기술적 allowlist가 아니다. 요청·관찰된 permission mode와 실제 도구·산출물 증거를 확인한다. 기본값은 `--sandbox` 요청이며 실제 격리나 모든 sandbox 외부 실행 차단을 보장하지 않는다. 전역 CLI 설정·output-only 엔진·일반 운영 task·기업 connector·SMTP 승인에는 적용하지 않는다.

임시 합성 코드 시험에 한해 sandbox 해제를 명시적으로 선택하는 예:

```bash
uv run --frozen researchctl validate-runner-tools --live --runner antigravity_exec --agy-dangerously-skip-permissions --agy-no-sandbox --scenario file-code --scenario code-repair --scenario combined-research --timeout-seconds 180 --json
```

`--agy-no-sandbox`는 기본 off이다. `--live`·`--agy-dangerously-skip-permissions`·Antigravity 단독 선택과 `file-code`·`code-repair`·`combined-research` 중 하나 이상의 명시적 `--scenario`를 모두 요구한다. 시나리오 생략, 코드 외 시나리오, Codex·혼합 provider는 호출 전에 거절한다. 승인된 호출에만 `--sandbox=false`를 전달하고 지정 임시 workspace의 합성 코드에 대한 지원되는 native unsandboxed 실행을 요청할 수 있다. 요청 상태 false와 실제 적용 미확인(`null`)을 구분한다. 이 시험은 host 접근 격리의 검증이 아니며 전용 파일 도구 오류나 다른 미승인 동작의 포괄적 우회 경로가 아니다.

## 시나리오별 수용 기준

| 시나리오 | task 동작 | 핵심 검증 |
|---|---|---|
| `file-write` | 지정 workspace의 고정 한국어 파일 작성 → native 파일 도구로 읽기 | 정확한 파일 bytes와 실제 쓰기·읽기 이벤트. 셸·웹·MCP 실행 없이 파일 기능만 검증 |
| `file-code` | 합성 CSV 읽기 → Python 분석 코드·테스트 작성 → 실행 → JSON 산출물 | 실제 읽기·쓰기·명령 이벤트, 계산 값, 테스트 결과, 입력 보존 |
| `code-repair` | 의도적으로 잘못된 작은 코드와 테스트 확인 → 실패 재현 → 코드 수정 → 재실행 | 수정 전 실패와 수정 후 성공의 실행 증거, 실제 patch와 올바른 결과 |
| `web-search` | Native 웹 검색으로 공식 Python 문서 조사 → 출처 포함 결과 | 실제 검색 이벤트·공식 출처. Agy는 성공한 본문 조회의 실제 공식 URL과 인용 일치도 요구. Codex 검색만으로 모든 페이지 조회를 주장하지 않음 |
| `mcp-research` | 실제 stdio MCP `search_documents` → `fetch_document` → 구조화 조사 | 합성 자료의 값, 실행별 marker·receipt와 서버 감사 일치. 서버 코드를 읽거나 답을 추측한 경우 불합격 |
| `mcp-public-docs` | 공개 원격 OpenAI Docs MCP 검색 → 공식 문서 조회 → 출처 기반 요약 | 실제 원격 MCP 도구 성공, 조회 결과와 공식 URL. 별도 key·기업 connector를 사용하지 않음 |
| `combined-research` | 합성 로컬 자료·코드 계산·공식 웹·MCP 자료를 함께 활용 | 각 요구 도구의 실행 증거와 결과 반영. 일부 도구만 사용한 결과를 전체 통과로 표시하지 않음 |

시나리오 입력은 개발 검증에 한정된다. 실제 고객·회사·수신자 데이터나 운영 task를 사용하지 않는다. 값이 불명확한 경우 추측하지 않고 실패·부분 검증 사유를 결과에 보존한다.

## MCP 자료의 성격

로컬 서버는 `researchops.runners.research_mcp`의 read-only 합성 corpus를 제공한다. `numeric-ledger`는 합성 매출 비교, `korean-policy`는 한국어·서울 날짜·opaque 수신 그룹, `partial-survey`는 누락 정보를 유지하는 일부 조사 자료다. 현재 시나리오에서 요청한 문서만 조사하며 다른 문서를 모두 검증했다고 주장하지 않는다.

서버 실행마다 새로운 marker와 receipt를 생성한다. 애플리케이션은 모델 반환값을 서버의 감사 기록과 대조해 실제 MCP 도구 호출 여부를 확인한다. 이 marker는 인증정보가 아니라 검증용 증거다. MCP 서버에 URL fetch·셸·임의 파일 경로·외부 쓰기 기능을 추가하지 않는다.

공개 원격 시나리오는 공식 문서용 `https://developers.openai.com/mcp`를 별도 사용한다. 실제 인터넷상의 MCP 연결이지만 Google Drive·Slack·사내 MCP의 OAuth 승인이나 데이터 접근을 검증하는 것은 아니다. [공식 서버 안내](https://developers.openai.com/learn/docs-mcp)

## 실행·보고 경계

- 기존 CLI 로그인은 정상 사용하지만 인증정보를 prompt·workspace로 전달하지 않는다.
- 독립 임시 workspace/control/evidence를 사용한다. 기본적으로 native sandbox를 요청하고 timeout·출력 상한·안전한 산출물 검사·cleanup 기록을 적용한다. Agy 호출별 자동 승인과 별도 sandbox 해제 승인, 실제 격리 증거를 구분한다. 위 `--agy-no-sandbox` 범위 외에는 sandbox 외부 실행을 요청하지 않는다.
- SMTP·운영 DB·전역 CLI 설정·영구 서비스는 변경하지 않는다. 사용자 날짜·표시는 `Asia/Seoul`이다.
- 실행 실패 시 fake 답이나 다른 provider로 자동 대체하지 않는다. 권한·인증 부재가 확인되면 재개 조건이 바뀌기 전 동일 실패를 반복하지 않는다.
- 모델 terminal 응답, 도구 완료, 산출물 검증은 별도 증거다. Antigravity의 `DONE`·exit 0이라도 terminal denial·빈 서버 감사가 있으면 실제 MCP 조사 성공이 아니다.
- Native 개발 capability 검증과 일반 hostile-task 운영 준비를 분리한다. 모든 도구 사용이나 운영 RUNNER-BOUNDARY 전체 완료를 보장하지 않는다.

명령의 적용 범위와 결과 해석은 [RUNNER_TOOL_VALIDATION](../../docs/RUNNER_TOOL_VALIDATION.md)을 따른다. 각 실행은 자체 evidence 디렉터리에 결과와 검증 근거를 기록한다.
