# 생성 코드의 별도 격리 실행 검증

`validate-runner-boundary`는 모델의 output-only 응답으로 받은 작은 합성 Python 함수를
인증 control process 밖의 namespace+cgroup 실행기에서 검사한다. 모델 응답을
control process에서 `exec`하거나 `eval`하지 않는다. 일반 운영 Task의 native 도구
전체를 강제로 격리하는 기능과는 범위가 다르다.

## 실행 조건

```bash
# 모델 미호출: 현재 실행기의 준비 상태 확인
uv run --frozen researchctl validate-runner-boundary --json

# 운영자가 미리 준비한 전용 cgroup v2 경로가 있을 때만 실행
uv run --frozen researchctl validate-runner-boundary --live \
  --runner codex_exec \
  --cgroup-root /sys/fs/cgroup/researchops-validation --json
```

예시 cgroup 경로는 실제로 존재하고 실행 계정에 필요한 memory/pids 제어가 위임되어야
한다. 이 명령은 권한을 부여하거나 mount·전역 정책·서비스를 구성하지 않는다.
`--runner`는 `codex_exec` 또는 `antigravity_exec`이며 반복할 수 있다. 기본은 두 엔진이다.
`--timeout-seconds`는 모델 응답 기한 1~300초이며 기본 120초다. `--model`은 `--live`와
함께 사용하는 선택값이고, `--output-parent`는 기존 증거 부모 디렉터리다.

운영 `--config`는 거부한다. `--live`가 없거나 실행기 준비가 부족하면 모델 호출 없이
종료 코드 `78`을 반환한다. 준비 상태를 통과한 뒤에도 먼저 모델을 쓰지 않는 kernel
preflight를 수행한다. 이후 전체 통과는 `0`, 실행·검증 실패는 `1`이다.

## 검증 계약

Preflight는 합성 host marker·host loopback socket·환경·proc·서울 시간대와 cleanup을
검사한다. 통과한 경우에만 기존 CLI 로그인으로 합성 함수 구현을 요청한다. 반환 소스는
별도 실행기에 전달하여 고정 테스트·결과값·원본 hash·하위 프로세스 정리를 확인한다.
SMTP·운영 Task·운영 DB를 사용하지 않는다.

생성 코드와 테스트 harness는 같은 격리 인터프리터에서 실행된다. 기능 테스트 통과를
악의적인 코드가 시험을 조작할 수 없다는 증거로 확대하지 않는다. 보고서의
`production_ready`, `hostile_model_tool_prevention_verified`, `filesystem_quota_verified`는
이 시험만으로 true가 되지 않는다. terminal 또는 cleanup 미확인 시 후속 호출을 중단한다.

## 관련 backend

`PublicResearchFetcher`는 별도 신뢰 transport process에서 GET/HEAD를 처리한다. 기본 port,
매 DNS 응답·redirect의 공개 주소 여부, numeric peer와 원래 hostname의 TLS 검증,
header/body/redirect/chunk 상한과 전체 deadline을 검사한다. 임의 자격증명·요청 body·
환경 proxy·cookie를 전달하지 않는다. 반환 text는 비신뢰 조사 자료이며 안전한 HTML이나
명령이 아니다. 이 모듈은 forward proxy나 모든 native 도구의 egress 통제 장치가 아니다.

`DedicatedFilesystemQuota`는 운영자가 Task마다 준비한 전용 파일시스템의 전체 byte/inode
용량과 mount identity를 확인한다. 일반 디렉터리의 여유 공간 감시를 quota로 취급하지 않는다.
Persistent ext4와 명시적으로 선택한 volatile tmpfs를 구분하고, 실행 직전 같은 snapshot을
재검사한다. 파일시스템을 생성·포맷·확장하지 않으며 다른 writable mount까지 제한했다고
주장하지 않는다.

별도 kernel 회귀는 `RESEARCHOPS_TEST_CGROUP_ROOT`와 `RESEARCHOPS_TEST_QUOTA_VOLUME`의
명시적 준비를 요구한다. 일반 회귀에서 해당 시험이 skip되면 실제 kernel 검증으로
계산하지 않는다. 호스트별 실행 결과는 [STATUS](STATUS.md)와 실행별 report로 확인한다.

일반 신뢰 운영은 [PRODUCTION_OPERATIONS](PRODUCTION_OPERATIONS.md), native 도구 개발은
[RUNNER_TOOL_VALIDATION](RUNNER_TOOL_VALIDATION.md), 보안 한계는
[12_SECURITY](12_SECURITY.md)를 따른다.
