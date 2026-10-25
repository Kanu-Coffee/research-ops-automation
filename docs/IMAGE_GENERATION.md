# 이미지 생성 설정과 엔진

Task는 Research/Compose의 LLM 선택과 독립된 이미지 생성 설정을 가질 수 있습니다.

```yaml
image_generation:
  enabled: true
  engine: codex        # codex | opencode | bailian | openrouter
  model: null          # 엔진별 모델 ID, null이면 엔진 기본값
```

- `enabled: false`는 이미지 생성 호출을 만들지 않습니다.
- `enabled: true`는 생성 권한입니다. Task 저장/시작 시 자동 생성하지 않으며,
  Research 또는 Compose가 staged helper를 호출할 때만 실행합니다.
- 기존 Task에 `image_generation`이 없으면 기존 Codex native imagegen helper
  동작을 보존합니다. 이 필드는 Task의 불변 버전에 포함되며 이후 실행의 선택을
  바꾸지 않습니다.
- Task 일반 편집·복제·고급 YAML·Web 생성/수정은 이 값을 함께 보존합니다.
  `researchctl task image TASK_ID [--enabled on|off] [--engine ...] [--model ...]`으로
  조회·변경할 수 있습니다.

## 엔진

| 엔진 | 실행 방식 | 자격증명 |
|---|---|---|
| `codex` | 기존 Codex native imagegen helper | Codex CLI 정상 로그인 |
| `opencode` | controller에서 `opencode run`으로 이미지 도구/스킬 요청 | OpenCode 정상 로그인 |
| `bailian` | `bl image generate` | `bl` 정상 로그인/API Key 설정 |
| `openrouter` | `POST {base_url}/images` 공식 Image API | 보호된 `image_services.openrouter_api_key_file` |

공통 구현은 `researchops/services/image_generation.py`에 있습니다. 엔진별 결과는
PNG/JPEG bytes·MIME·크기·SHA-256으로 검증하며, 파일이 없는 텍스트 완료는 성공이
아닙니다. 다른 엔진으로 자동 fallback하지 않습니다.

```yaml
image_services:
  openrouter_api_key_file: /protected/openrouter.key
  openrouter_base_url: https://openrouter.ai/api/v1
  openrouter_timeout_seconds: 120
  max_image_bytes: 4194304
```

OpenRouter key는 Task package·prompt·worker 환경·일반 로그·HTML에 넣지 않습니다.
`image_services.openrouter_api_key_file`은 Task/archive 경계 밖의 절대 보호 경로여야
합니다. 현재 Web 관리자 화면의 OpenRouter key 등록 UI는 아직 제공하지 않으며,
보호 파일을 운영자가 준비합니다.

## 실행과 제한

- CLI 예:

```bash
uv run --frozen researchctl task image TASK_ID --enabled on --engine openrouter \
  --model <image-model> --json
uv run --frozen researchctl image models --engine bailian --json
uv run --frozen researchctl image generate --engine codex \
  --prompt "Synthetic diagram" --filename first.png --out-dir /tmp/images --json
```

- Research 및 Compose worker 모두 `ImageBroker` IPC를 통해 staged helper(`generate_images()`)를
  호출하며, Controller의 `ImageBroker`가 Task에 지정된 이미지 엔진(`codex`, `opencode`,
  `bailian`, `openrouter`)으로 생성을 안전하게 위임하고 결과 이미지를 검증하여 반환합니다.
  자격증명은 worker에 전달되지 않고 Controller가 관리합니다.
  Task의 `enabled: false` 시 브로커는 생성 호출을 수행하지 않습니다.
- 이미지 생성 결과는 기존 artifact hash/size/MIME/CID/Compose 검증을 통과해야 하며
  실패한 이미지 때문에 읽을 수 있는 업무 record를 삭제하지 않습니다.
- Compose의 새 이미지는 controller 메모리의 완료 job snapshot을 runner 결과로 전달해
  검증합니다. worker가 만든 ledger JSON은 승인 근거로 읽지 않습니다. 검증된 snapshot과
  bytes는 앱 archive에 보존하여 모델을 다시 호출하지 않는 재전송에도 사용합니다.
- 모델 목록은 후보 목록이며 계정별 사용 권한을 실시간 조회하지 않습니다. Codex/OpenCode는
  native 도구에 모델을 지시하며 실제 모델 영수증이 없으면 `model_verified=false`입니다.
  요청/관찰 모델을 구분해 보존하고, 명시적인 모델 불일치는 `image_model_mismatch`로
  실패 처리합니다. 이 미검증 상태만으로 정상 이미지 bytes를 차단하지 않는 것이 MVP 범위입니다.
- OpenRouter 401/403/429·timeout·응답 크기·잘못된 base64/MIME은 고정 reason code로
  실패 처리합니다. 유료 생성의 불확실한 재요청은 자동 수행하지 않습니다.
- 실제 유료 생성은 기본 회귀에 포함하지 않으며, `--live`/명시적 `image generate`에서만
  검증합니다. 엔진별 자격증명·모델이 없으면 `image_engine_unavailable`/검증되지 않음으로
  남깁니다.

## MCP와의 구분

이미지 생성은 MCP 조회와 별개입니다. Task의 이미지 엔진이 OpenCode여도 OpenCode
세션의 MCP 조회 권한을 이미지 생성 권한으로 확대하지 않으며, 그 반대도 아닙니다.
