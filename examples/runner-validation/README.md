# AI runner 검증용 합성 task

운영 task·계정·메일 주소와 분리한 재현 가능한 입력 4종이다. 기본 template discovery(`examples/tasks`)에는 등록하지 않는다.

| 디렉터리 | Task ID | 검증 목적 | Research status / record 수 |
|---|---|---|---|
| document-extract | sample-document-extract | 한국어 문서 추출, 날짜·담당 보존 | success / 2 |
| numeric-compare | sample-numeric-compare | 양수·음수 변화량과 비율 | success / 2 |
| partial-coverage | sample-partial-coverage | 부분 조사, null·경고·기존 record 보존 | partial / 1 |
| no-updates | sample-no-updates | 변경 없음 및 빈 결과 Compose | no_updates / 0 |

모두 `enabled=false`, `runner.type=fake`, `delivery.mode=dry_run`, `Asia/Seoul`이다.
합성 source는 canonical package에 포함되고 Research 지시 파일에 명시된다. Compose는 source 대신 immutable composition input만 사용한다.
실제 주소·recipient membership·외부 조사용 네트워크·실제 SMTP는 필요하지 않다. Live 검증에서는 보호된 CLI control process가 기존 로그인과 provider 네트워크를 정상 사용한다.

각 `fixtures/expected.json`은 테스트 기대값이다. `fixtures/`는 version hash와 worker 입력에서 제외되므로 AI에 정답을 주지 않는다.
`sample-result.json`은 FakeRunner Research 입력, `sample-composition-result.json`은 FakeRunner의 group/subject 입력이다.
HTML/text preview는 **2026-09-06**으로 고정한 기대 출력 예시다. FakeRunner는 실제 run의 서울 날짜와 record ID로 HTML을 생성한다.
Fake 통과는 pipeline/schema/보존 계약을 검증할 뿐 실제 AI의 추출·산술·문장 품질을 증명하지 않는다.

검증:

```sh
uv run --frozen python -m unittest tests.test_validation_samples -v
uv run --frozen researchctl validate-runners --runner fake --json
# 실제 모델 사용을 명시적으로 선택할 때만 실행:
uv run --frozen researchctl validate-runners --live --json
```

모든 검증은 임시 설정·DB·workspace에서 실행한다. 기본 unittest는 모델을 호출하지 않는다.
명시적 `--live`는 이 합성 package의 임시 사본을 선택 provider로 봉인하고 실제 Research/Compose를 호출한다. 정답 fixture는 prompt에 넣지 않는다.
일반 운영의 hostile-task 격리 readiness와 개발 검증은 구분한다. Host credential을 task workspace로 복사·연결하지 않는다. 결과와 재현은 `docs/RUNNER_VALIDATION.md`를 따른다.
