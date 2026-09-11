# Linux 사용자 소유 설치

단일 Linux 호스트에 운영 사용자 소유의 private 디렉터리를 만들고 `systemd --user`로 실행하는 절차입니다. 예시 경로·도메인은 설명용이며 특정 운영환경을 가리키지 않습니다. 비어 있지 않은 디렉터리에 설치기를 다시 실행하지 않습니다.

## 준비 조건

- Python 3.12 이상과 `uv`, 소스 checkout, lock에 지정된 패키지/build 의존성 cache.
- Production에 사용할 `codex` 또는 `agy` CLI의 설치와 해당 OS 계정의 정상 로그인. 계정 인증은 CLI의 공식 절차로 직접 완료합니다.
- 사용자 systemd manager. 제공 worker unit의 `DelegateSubgroup`는 systemd 254 이상과 cgroup v2 `memory`/`pids` 위임이 필요합니다.
- 해당 사용자가 소유하고 권한이 `0700`인 빈 설치 루트. 소스 checkout과 설치 루트는 서로 포함하지 않아야 하고 경로에 symlink를 사용하지 않습니다.
- Web에 사용할 TLS reverse proxy와 운영자 소유 도메인. 초기 Web은 loopback HTTP upstream이며 브라우저 로그인은 TLS proxy를 통해 진행합니다.

```bash
python3 --version
uv --version
systemctl --version
systemctl --user show-environment > /dev/null
```

먼저 checkout에서 `uv sync --frozen`과 `uv build --no-sources`로 잠긴 의존성 및 빌드 준비를 완료합니다. 설치기는 `--offline --no-dev --no-editable --link-mode copy`와 현재 Python을 사용하고 새 Python을 다운로드하지 않습니다. Offline 의존성이 없으면 설치 증거를 확인하고 cache를 준비한 뒤 새 빈 대상에서 다시 계획합니다.

## 초기 설치

아래는 현재 사용자 홈 아래의 예시입니다. `/opt/researchops`처럼 관리자가 미리 소유권·권한을 준비한 전용 경로도 사용할 수 있습니다.

```bash
export RESEARCHOPS_INSTALL_DIR="$HOME/.local/share/researchops"
install -d -m 700 "$RESEARCHOPS_INSTALL_DIR"
uv run --frozen python -m researchops.deployment user-preflight --root "$RESEARCHOPS_INSTALL_DIR"
uv run --frozen python -m researchops.deployment user-install --source "$PWD" --root "$RESEARCHOPS_INSTALL_DIR" --web-port 8765
uv run --frozen python -m researchops.deployment user-install --source "$PWD" --root "$RESEARCHOPS_INSTALL_DIR" --web-port 8765 --apply
```

`--apply` 전에는 계획만 출력합니다. 적용 시 읽은 소스 hash, 설치 version/help, unit 검사와 로그를 `evidence/`에 보존합니다. 설치기는 서비스 등록·시작이나 DB 초기화를 하지 않습니다. 실패 시 남은 claim·로그를 삭제해 같은 디렉터리를 억지로 재사용하지 말고 원인을 확인합니다.

```text
<install>/
  releases/<release-id>/        소스 snapshot, schema, examples, .venv
  current -> releases/...      현재 코드 선택 링크
  config/settings.yaml         앱 설정
  config/tasks/                사용자 작성 canonical package
  data/delivery_config.yaml    보호된 발신 계정·수신자 설정
  data/researchops.db          최초 init에서 생성되는 상태·계정·SMTP 원장
  data/task-workspaces/        Task별 project와 phase 작업 공간
  data/run-archive/            불변 실행 증거
  data/task-versions/          봉인된 Task package
  data/delivery-outbox/        전달 package·receipt
  data/backups/                DB 백업
  units/                      렌더링된 사용자 서비스
  evidence/                   설치 증거
```

기존 버전의 Draft 보존 디렉터리가 있다면 삭제하지 않습니다. 새 UI에는 Draft 기능이 없습니다. venv의 Python은 기존 관리 interpreter에 의존할 수 있으므로 `readlink -f "$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/python"`으로 위치를 확인합니다. 설치 루트 백업만으로 그 interpreter까지 보존되지 않을 수 있습니다.

## 설정과 TLS proxy

```bash
export RESEARCHOPS_ROOT="$RESEARCHOPS_INSTALL_DIR/current"
export RESEARCHOPS_CONFIG="$RESEARCHOPS_INSTALL_DIR/config/settings.yaml"
"$RESEARCHOPS_ROOT/.venv/bin/researchctl" --config "$RESEARCHOPS_CONFIG" init
"$RESEARCHOPS_ROOT/.venv/bin/researchctl" --config "$RESEARCHOPS_CONFIG" doctor --json
```

Doctor의 실행 파일·등록 설정 진단은 CLI 인증·실제 추론·SMTP 수락 검증과 별개입니다. 초기 미설정 항목의 진단을 확인하고 필요한 설정을 준비합니다.

설치 설정의 기존 항목을 보존하면서 Web hostname을 실제 사용할 도메인으로 등록합니다. 아래 `researchops.example.org`는 예약 예시이며 실제 인증서를 발급받아 사용할 도메인으로 바꿔야 합니다.

```yaml
web:
  enabled: true
  bind: 127.0.0.1
  port: 8765
  allow_remote_proxy: false
  allow_insecure_local_auth: false
  trusted_proxy_cidrs: [127.0.0.1/32, '::1/128']
  allowed_hosts: [localhost, 127.0.0.1, '::1', researchops.example.org]
  require_origin_check: true
  csrf_protection: true
```

같은 호스트의 Nginx/NPM이 HTTPS를 종료하고 `http://127.0.0.1:8765`로 전달하도록 설정합니다. 정상 인증서, Host, 원래 Origin, proxy가 덮어쓴 forwarding header를 유지합니다. NPM을 별도 network namespace/컨테이너에서 실행하면 그 컨테이너의 `127.0.0.1`은 앱 호스트가 아니므로 [원격 proxy 설정](../deploy/nginx/README.md)을 적용하거나 실제 같은 host network 경로를 준비해야 합니다.

Production 세션은 Secure 쿠키이므로 브라우저에서 `http://127.0.0.1:8765`에 직접 접속하는 것으로 개설·로그인을 완료할 수 없습니다. 독립 loopback 시험용 `allow_insecure_local_auth`를 운영 접속 해결책으로 켜지 않습니다. TLS proxy를 먼저 준비합니다.

## 서비스 등록과 관리자 개설

기존에 같은 이름의 unit이 있으면 다른 설치를 덮어쓰지 말고 경로·소유자를 확인합니다.

```bash
systemd-analyze --user verify "$RESEARCHOPS_INSTALL_DIR"/units/researchops-*.service "$RESEARCHOPS_INSTALL_DIR"/units/researchops-scheduler.timer
systemctl --user link "$RESEARCHOPS_INSTALL_DIR"/units/researchops-*.service "$RESEARCHOPS_INSTALL_DIR"/units/researchops-scheduler.timer
systemctl --user daemon-reload
systemctl --user enable --now researchops-web.service researchops-worker.service
"$RESEARCHOPS_ROOT/.venv/bin/researchctl" --config "$RESEARCHOPS_CONFIG" auth setup-token
```

마지막 명령의 30분 유효 일회용 토큰을 보호된 터미널에서 확인하고 **TLS 도메인**의 관리자 개설 화면에 입력합니다. 로그인 이름·비밀번호는 운영자가 직접 정합니다. 토큰을 이슈·채팅·문서·명령 로그에 복사하지 않습니다. 이후 [로그인과 권한](WEB_UI_AUTH.md)을 따라 사용자·조회자를 등록합니다.

Worker가 cgroup 경로·위임 오류로 시작하지 못하면 해당 unit의 실제 `ControlGroup`, 사용자 manager와 `memory`/`pids` 위임을 확인합니다. 템플릿은 표준 `user.slice/.../app.slice/<unit>` 계층을 전제로 하므로 비표준 환경에서는 운영자가 해당 서비스 경로에 맞춰 검토해야 합니다. 부모 slice·다른 서비스·전역 보안 정책을 자동 변경하지 않습니다.

## 실제 운영 시작

초기 설치는 `environment: production`이지만 발송 차단 ON, 전달 dry-run, SMTP·scheduler OFF입니다. [운영 가이드](PRODUCTION_OPERATIONS.md)에 따라 발신 계정과 유효 그룹을 만들고, 전역 발송 설정과 Task의 handoff를 명시적으로 켭니다. 준비된 이후에만 다음 서비스를 시작합니다.

```bash
systemctl --user enable --now researchops-smtp.service researchops-scheduler.timer
systemctl --user status researchops-web.service researchops-worker.service researchops-smtp.service --no-pager
systemctl --user list-timers researchops-scheduler.timer
journalctl --user -u researchops-worker.service -u researchops-smtp.service -n 80 --no-pager
```

Task별 예약이 OFF이면 timer가 있어도 그 Task를 자동 실행하지 않습니다. 로그인 세션 종료 후에도 사용자 서비스를 유지하려면 호스트 관리자가 사용자 manager의 linger/부팅 정책을 준비해야 합니다. `enabled` 또는 linger 설정만으로 실제 재부팅 복구가 검증됐다고 간주하지 않습니다.

[백업과 업그레이드](BACKUP_AND_UPGRADE.md), [보안 경계](12_SECURITY.md)를 확인하고 DB·설정·archive를 함께 보호합니다.
