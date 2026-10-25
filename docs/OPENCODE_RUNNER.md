# OpenCode v2 runner

`opencode_exec`는 OpenCode v2의 공식 `opencode api` 세션 API를 사용하는 세 번째
Research/Compose 실행 엔진입니다. `codex_exec`, `antigravity_exec`와 같은 Task·Run·archive
계약을 사용하며, 기존 두 provider의 설정과 cache를 변경하지 않습니다.

## 전제와 설치

- OpenCode v2 바이너리가 필요합니다. 이 저장소의 구현·시험은 `v2.1.1` 설치본에서
  확인했습니다. 이 버전이 최신 릴리스라는 뜻은 아닙니다.
- OpenCode의 기존 background service와 로그인이 이미 정상이어야 합니다. ResearchOps는
  `opencode service start`, 로그인, upgrade를 자동 수행하지 않습니다.
- `command -v opencode`가 실패하는 서비스 환경에서는 절대경로를 지정합니다.

```yaml
runner:
  opencode_binary: /home/operator/.opencode/bin/opencode
  default_type: opencode_exec   # 기존 기본값 codex_exec는 유지 가능
```

Task/단계 선택:

```yaml
runner:
  type: opencode_exec
  model: openprovider/model-id   # OpenCode catalog의 canonical provider/model
  reasoning_effort: high         # 실제로 catalog에 있는 variant ID
```

모델 ID 안에 `/`가 있을 수 있으므로 첫 `/`만 provider 구분자로 사용하고 나머지는
model ID로 보존합니다. 예: `openrouter/moonshotai/kimi-k3.1-pro`. variant가 없으면
저장하지 않으며, catalog에 없는 variant는 저장 전에 거부합니다.

## 호출 구조

1. 인증된 control process에서 `opencode api`로 catalog와 실행 가능성을 먼저 확인합니다.
   background service가 없으면 새로 시작하지 않고 `RUNNER-SERVICE-UNAVAILABLE`로
   차단합니다.
2. service status와 version/model capability를 **모든 MCP/model/session 호출보다 먼저**
   확인합니다. 확인 직후 service가 사라지는 race는 CLI의 기본 server discovery에 맡기며,
   앱은 새 server 자동 시작을 완전히 차단했다고 주장하지 않습니다.
3. phase/attempt마다 **빈 native session**을 만들고 location을 해당 phase의 임시
   staging으로 지정합니다. 이전 session, `--continue`, `--fork`, `parentID`를 사용하지
   않습니다.
4. session ID·run/phase/attempt/fencing 연결을 prompt 제출 전에
   `logs/opencode-session-<stage>.json`에 기록합니다.
5. session environment는 task-local `HOME`·`TMPDIR`과 최소 `PATH`·locale·timezone만
   설정합니다. provider token, OpenCode server password, SMTP 설정, SSH agent, host
   credential은 worker session에 복사하지 않습니다.
6. 전체 지시문은 보호된 staging의 UTF-8 파일로 쓰고, prompt에는 짧은 bootstrap과 파일
   경로·크기·SHA-256만 전달합니다. 모델은 파일 전체를 읽어야 하며 잘린 발췌로 대체할 수
   없습니다.
7. 모델은 기존 `submit_result` helper로 `submission.json`을 만들고 file-reference
   envelope만 최종 응답으로 반환합니다. 앱은 hash·size·경로·phase schema를 다시 검증한
   뒤 고정 출력 파일만 반입합니다.
8. 완료는 session `outcome=succeeded`와 `idle`, 마지막 assistant message의 `finish=stop`,
   `time.completed`, message ID·model·variant·agent, prompt/session 결합, 관찰된 권한/도구를
   확인합니다. 최종 message에 `running`/`streaming` 도구가 남아 있으면 반입하지 않습니다.
9. `shell.list`(현재 실행 중 shell), `pty.list`와 session-scoped persistent terminal 목록을
   조회해 이번 location/session의 live resource가 0인지 확인합니다. API 오류·형식 오류·
   identity 불일치는 빈 목록으로 간주하지 않고 cleanup 미확인으로 남깁니다.
10. 각 control subprocess의 `cleanup_verified`와 remote resource 정리 결과를 함께
   결합합니다. session create 응답을 잃은 경우 remote session 부재를 증명할 수 없으므로
   `cleanup_verified=false`를 유지합니다.

`session.shell`은 모델 phase 실행이나 취소의 대체 수단으로 사용하지 않습니다.
timeout·취소·오류 시에는 **이번 호출이 소유권을 기록한 session에만**
`session.interrupt`를 요청합니다. 공유 server 중지·전역 reload·session 전체 삭제·
임의 process 종료를 수행하지 않습니다. interrupt가 `false`인 경우에도 session이 이미
idle인지와 해당 location의 live shell/PTY를 함께 확인하며, 정리를 확인하지 못하면
`needs_attention`으로 남깁니다.

side effect 전(prompt 제출 전 포함)에는 취소와 남은 deadline을 확인하고, API
subprocess에도 가능한 범위에서 cancellation callback과 남은 deadline을 전달합니다.
Trace 예산은 control 응답, polling, message page, tool metadata를 공유하며
`trace_max_bytes`/`trace_max_event_bytes`/`trace_max_events`를 실제로 집행합니다.
초과·timeout 시에는 부분 진단을 보존하되 성공으로 승격하지 않습니다.

## 권한과 네트워크

호출별 `permissions`는 `*` deny를 먼저 두고 필요한 `read`·`edit`·`glob`·`grep`·`shell`
및 명시한 staging/project 경계를 다시 허용합니다. `subagent`, `question`, `skill`은
명시적으로 deny하고, MCP 조회를 위해 Code Mode `execute`는 허용합니다. 별도
project/global agent 파일을 만들지 않고 기본 primary agent에 session-level rules를
적용합니다.

- Research와 Compose 모두 운영자가 등록한 MCP server의 tool/resource 조회를 허용합니다.
  Code Mode `execute`도 허용하며 nested tool은 각자의 permission을 다시 검사합니다.
  직접 `webfetch`/`websearch`는 `public-research` Research에서만 허용합니다.
- MCP 상태는 가능하면 구조화된 `mcp.list`/`mcp.resource.catalog` API를 사용하고,
  connected/pending/disabled/failed/needs_auth와 resource/template metadata를 구분합니다.
  연결 상태는 실제 tool 호출 성공 증거가 아닙니다.
- MVP는 신뢰된 운영자가 등록한 MCP 서버의 도구 이름을 넓게 허용하고,
  알려진 변경 동사와 명시적인 mutation metadata를 차단합니다. 이름이 임의인 조회
  도구도 사용할 수 있습니다. 이 정책이 모든 도구의 읽기 전용 동작을 보증하지는 않습니다.
  설치본에 없는 `mcp.tool.catalog`는 호출하지 않습니다. 완전한 tool capability 분류와
  nested 도구의 의미 검증은 후속 범위입니다.
- MCP tool 이름은 설치본의 정규화 규칙에 맞춰 server prefix action과 resource action을
  허용합니다. 원격 쓰기·삭제·메일 전송 권한을 별도로 승인하지 않으며, observed write
  semantics는 제한적으로만 검증합니다.
- `execute`는 MCP nested tool뿐 아니라 Code Mode의 일반 utility/fetch 표면도 엽니다.
  현재 앱은 flat trace의 direct web tool만 phase 위반으로 검사하며, Code Mode 내부
  fetch/utility 호출을 MCP 조회와 완전히 분리해 감사하지는 못합니다. 따라서 “MCP 조회
  허용”을 direct web/HTTP 허용이나 완전한 egress 격리로 해석하지 않습니다. MCP nested
  tool은 server/tool/outcome 단위의 구조화된 관찰이 아직 제한적입니다.
- shell은 host 사용자 권한으로 실행됩니다. native 파일 규칙과 관찰된 tool 검사는 적대적
  Task의 OS/egress 격리가 아닙니다.

`permissions`의 마지막 일치 규칙이 우선합니다. 설치/관리 정책이 특정 action을
hard-deny하면 앱이 이를 해제할 수 없으며, 해당 capability는 unavailable/blocked로
표시합니다.

Production에서 `read`/`write`/`edit`가 `external_directory` 거부로 실행되지 않은 경우는
`OPENCODE_BLOCKED_FILE_ACCESS` 경고로 보존합니다. 세션이 정상 종료하고 최종 제출물의
hash·구조·파일·cleanup 검증을 통과하면 이전의 차단 이력만으로 전체 Run을 실패시키지
않습니다. 거부된 도구 자체를 성공으로 바꾸거나 허용 디렉터리를 넓히지 않습니다.
다른 권한 거부와 output-only 검증의 엄격한 정책은 유지합니다. 임시 파일도 supplied
artifact_root 아래에 두며 공유 `/tmp/opencode`를 사용하지 않습니다.

공급자가 `provider.quota`로 종료하면 `OPENCODE_PROVIDER_QUOTA_EXHAUSTED`와 사용량
복구/모델 선택 안내를 표시합니다. 일시적인 HTTP 429는 `OPENCODE_PROVIDER_RATE_LIMITED`로
구분합니다. 원본 HTTP body/header를 일반 실행 로그에 복사하지 않고 분류·상태 코드만
보존하며, 앱이 자동으로 유료 모델 전환이나 업무 재실행을 수행하지 않습니다.

## 모델 catalog

`model.list`의 공개 projection에서 `providerID`, catalog `id`(선택 별칭), `name`,
variant ID만 정규화해 저장합니다. `modelID`는 upstream 이름이며 picker/session key로
대체하지 않습니다. `settings`, `headers`, endpoint, raw config는 저장하지 않습니다.
API 조회가 실패하면 `opencode models` text의 model ID와 기본 선택만 fallback으로
사용하며, variant를 추측하지 않습니다. 기존 Codex/Agy 두-provider cache는 유지하고
OpenCode 항목만 `unavailable`로 추가합니다.

## 검증

합성 Research/Compose:

```bash
uv run --frozen researchctl validate-runners --runner opencode_exec --live \
  --sample numeric-compare --model <provider/model> --timeout-seconds 240 --json
```

합성 native tool:

```bash
uv run --frozen researchctl validate-runner-tools --runner opencode_exec --live \
  --scenario file-code --model <provider/model> --timeout-seconds 240 --json
```

MCP 합성 scenario는 현재 OpenCode에 test 전용 MCP를 호출별로 주입하는 공식 경로를
구현하지 않았으므로 계속 `OPENCODE-MCP-PROVISIONING-UNSUPPORTED`로 차단합니다. 실제
운영 MCP는 OpenCode의 native 등록 설정을 사용하며, 이 앱은 인증 정보나 config 내용을
읽지 않습니다.

## 알려진 한계

- API control stdout은 private 임시 regular file에 직접 연결합니다. CLI가 큰 pipe
  응답을 비우기 전에 종료하는 경우를 피하며 child file-size limit과 합산 stdout/stderr
  제한, timeout/cancel 및 process-group cleanup을 유지합니다. 파일은 수집 후 닫고 제거합니다.
  message page는 trace 전체 예산으로 계산하고 개별 message에 event 크기 제한을 적용합니다.
  명시한 작업 경로 자체와 그 하위만 허용하며 상위 폴더 탐색은 허용하지 않습니다.
- durable session log/SSE 실시간 수집은 아직 사용하지 않습니다. 완료 후 bounded message
  projection을 사용하며 pagination/cursor 중복은 앱에서 제한합니다. timeout 시에는
  별도 2초/1 MiB 한도의 부분 projection만 보존합니다.
- native tool 검증은 shell `metadata.exit`·truncated·unittest 결과 marker와 파일 write
  path/hash를 확인합니다. 모델이 테스트를 실행하지 않거나 실패→수정→성공 순서를
  남기지 않으면 controller 재실행이 성공해도 해당 capability를 통과로 세지 않습니다.
- server restart, background tool/PTY, 전체 descendant 정리는 보장하지 않습니다.
- OpenCode native configuration/credential store는 앱이 검사하지 않습니다.
- binary가 설치되어 있어도 service·login·model 권한이 없으면 실행할 수 없습니다.
- 실제 모델·MCP·web·이미지 생성의 성공 범위는 해당 검증 report와 [STATUS](STATUS.md)를
  따릅니다. 단순 텍스트 응답 성공을 tool capability 성공으로 확대하지 않습니다.
