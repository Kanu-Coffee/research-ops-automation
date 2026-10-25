# Native 도구 개발 검증

`validate-runner-tools`는 신뢰된 합성 입력으로 CLI의 파일·코드·공개 웹·MCP 도구를
확인한다. [Research/Compose 검증](RUNNER_VALIDATION.md)과 별도 명령이며 일반 운영
Task를 실행하거나 SMTP를 발송하지 않는다. 운영 native runner의 신뢰 경계는
[보안 가이드](12_SECURITY.md)를 따른다.

## 실행과 시나리오

```bash
# 모델 미호출; 선택된 case는 명시적 opt-in 대기 상태
uv run --frozen researchctl validate-runner-tools --json

# 한 provider와 한 합성 시나리오 실행
uv run --frozen researchctl validate-runner-tools --live \
  --runner codex_exec --scenario file-code --timeout-seconds 180 --json
```

`--runner`는 `codex_exec`, `antigravity_exec` 또는 `opencode_exec`, `--scenario`는 아래
값이며 반복할 수 있다. 생략하면 전체를 선택한다. timeout은 1~300초, 기본 180초다.
`--model`은 `--live`와 함께 사용할 선택값이다. `--output-parent`에는 기존 증거 부모
디렉터리를 지정한다. 운영 `--config`는 받지 않으며 매 실행 새 private
workspace/control/evidence를 만든다.

OpenCode는 현재 `file-write`, `file-code`, `code-repair`, `web-search`를 같은 합성
workspace에서 검사한다. `mcp-research`와 `mcp-public-docs`는 test 전용 MCP를 호출별로
주입하는 공식 경로가 없어 `OPENCODE-MCP-PROVISIONING-UNSUPPORTED`로 차단한다. OpenCode
검증도 실제 파일 bytes·생성 코드 실행·단위 테스트·관찰된 tool 상태와 session cleanup을
확인하며, 모델의 성공 문구만으로 통과하지 않는다.

OpenCode의 `code-repair`는 실제 `python -m unittest` 명령 위치와 대상 테스트를 확인하고,
모델이 실행한 동일 테스트의 실패(`FAILED` marker) → 정확한 `metrics.py` 수정 → 같은
테스트의 성공(`OK`, exit 0, 실행 test count) 순서, 세 테스트 존재, 테스트 파일 write
hash와 실제 파일 일치를 함께 요구한다. `echo`/주석/문자열에 테스트 이름만 포함한 명령,
다른 테스트 대상, 0-test 실행, 실패한 write/edit, controller-only 실행은 인정하지 않는다.
또한 `;`, `&&`, pipe, redirection, comment 등 simple command 경계를 넘어 다른 명령의
파일 인자를 대상 테스트로 귀속하지 않으며, 대상 unittest가 정확히 한 번 실행된 경우만
실행 증거로 인정한다. 복수 실행·불확실한 shell 구문은 unverified로 남긴다. `file-code`는 모델이 프로그램과 테스트를
실제 실행한 shell 증거를 요구하며, `web-search`는 검색과 공식 URL 본문 조회를 각각
확인한다. controller의 독립 재실행은 보강 증거일 뿐 모델의 native 실행을 대신하지
않는다. live shell/PTY/persistent terminal이 남으면 해당 case는 cleanup 미확인으로
실패/보류한다.

| 시나리오 | 필요한 증거 |
|---|---|
| `file-write` | 전용 파일 도구의 쓰기·읽기와 정확한 파일 bytes |
| `file-code` | 합성 CSV 분석 코드·테스트·실행 결과 및 입력 보존 |
| `code-repair` | 같은 테스트의 수정 전 실패 → 실제 수정 → 수정 후 성공 |
| `web-search` | 실제 native 검색과 공식 문서 출처, provider별 본문 조회 증거 |
| `mcp-research` | 로컬 합성 stdio MCP 호출과 서버 marker·receipt 대조 |
| `mcp-public-docs` | 공개 문서 MCP의 실제 검색·조회와 출처 |
| `combined-research` | 합성 코드·공식 웹·로컬 MCP 각 요구사항의 실행 증거 |

구체적인 합성 입력 계약은 [시나리오 안내](../examples/runner-tool-validation/README.md)를
따른다. 공개 문서 MCP 시험은 사내 connector나 개인 계정 데이터 접근을 검증하지 않는다.

## Antigravity의 명시적 개발 옵션

기본 호출은 native sandbox를 요청한다. 다음 옵션은 해당 합성 세션의 **모든 도구 요청**을
자동 승인하므로 개별 도구 allowlist로 해석하면 안 된다.

```bash
uv run --frozen researchctl validate-runner-tools --live \
  --runner antigravity_exec --agy-dangerously-skip-permissions \
  --scenario mcp-research --json
```

`--agy-dangerously-skip-permissions`는 `--live`와 명시적 Antigravity 단독 선택을 요구한다.
Sandbox 해제 시험에는 아래 조건을 모두 추가해야 한다.

```bash
uv run --frozen researchctl validate-runner-tools --live \
  --runner antigravity_exec --agy-dangerously-skip-permissions --agy-no-sandbox \
  --scenario file-code --scenario code-repair --json
```

`--agy-no-sandbox`는 명시적으로 선택한 `file-code`, `code-repair`, `combined-research`에만
허용한다. 시나리오 생략·다른 시나리오·혼합 provider는 호출 전에 거부한다. 이 옵션은
호출별 `--sandbox=false`를 요청하며 전역 CLI 설정을 수정하지 않는다. requested 상태와
실제 적용이 확인되지 않은 `null` 상태를 구분한다. 실패 뒤 자동으로 sandbox를 해제하거나
다른 엔진으로 바꾸는 fallback은 없다.

이 개발 옵션은 output-only 검증이나 일반 운영 설정을 변경하지 않는다. 일반 운영의
Agy native 실행은 [운영 가이드](PRODUCTION_OPERATIONS.md)에 별도로 정의되어 있다.

## 판정과 한계

Exit 0, 도구 `DONE`, 모델 자기보고만으로 성공을 확정하지 않는다. terminal 상태·권한 거부·
실제 산출물·입력 hash·테스트 순서·MCP 서버 감사 증거를 함께 검사한다. Agy의 생성된
응답 파일은 현재 conversation의 선행 성공 호출과 연결된 정확한 경로만 인정한다.
인증·설정 파일이나 다른 conversation 접근을 허용하는 규칙이 아니다.

결과는 passed/failed/blocked와 세부 검사, 요청·관찰된 permission mode, cleanup 및 호출
집계를 보존한다. 종료 코드는 통과 `0`, 실패 `1`, 차단 `78`이다. 권한·sandbox·terminal·
cleanup 문제가 있으면 같은 기능의 관련 후속 case를 보류한다. 재개 조건을 확인한 뒤
해당 시나리오를 선택하여 다시 검사한다.

도구 사용 증거는 사실 검증이나 모든 URL의 본문 조회를 보장하지 않는다. 사후 경로 검사는
발생한 외부 효과를 되돌리지 않으며, process group 정리는 악성 코드·공유 daemon·전체
host 접근 격리를 대신하지 않는다. 실제 실행 결과는 각 report와 [STATUS](STATUS.md)를
기준으로 판단한다. 공개 배포본에는 운영자의 원본 증거와 업무 지시문을 포함하지 않는다.
