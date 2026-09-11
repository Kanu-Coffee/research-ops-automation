# 백업·업그레이드·복구

앱 코드, 설정, 상태 DB, Task package, workspace, archive와 SMTP 원장을 함께 관리합니다. 아래 명령의 `RESEARCHOPS_INSTALL_DIR`은 [설치](USER_DEPLOYMENT.md)에서 정한 private 루트입니다. 백업에는 주소·계정 정보·메일·업무 결과가 포함될 수 있으므로 공개 저장소나 이슈에 올리지 않습니다.

## 백업

```bash
"$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/python" -m researchops.operations backup "$RESEARCHOPS_INSTALL_DIR/data/researchops.db" "$RESEARCHOPS_INSTALL_DIR/data/backups"
```

DB 도구는 SQLite online backup과 integrity check를 수행합니다. 실행 중인 DB 파일을 단순 `cp`한 사본으로 대신하지 않습니다. DB 백업에는 인증·소유권·예약·SMTP 상태도 들어가지만 다른 파일은 포함되지 않습니다.

같은 복구 시점에 `config/`, 보호된 delivery 설정, 봉인 Task 버전, run archive/outbox·receipt, 필요한 Task project와 코드 release도 보관해야 합니다. 파일들까지 일관된 snapshot을 만들려면 신규 실행을 보류하고 실행·SMTP가 유휴인지 확인한 뒤 전체 서비스를 중지하여 호스트의 승인된 백업 방식으로 보관합니다. Python interpreter가 설치 루트 밖에 있다면 그 의존성도 복구 계획에 포함합니다.

## 보관 기간 정리

```bash
"$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/python" -m researchops.operations cleanup 90 --database "$RESEARCHOPS_INSTALL_DIR/data/researchops.db"
```

기본은 dry-run입니다. 결과를 검토하고 같은 명령에 `--apply`를 주면 허용된 archive를 복구 가능한 retired 영역으로 이동합니다. Task workspace와 모든 개인정보가 이 명령 하나로 삭제되는 것은 아닙니다. 불변 증거·SMTP 원장을 임의 삭제하지 않습니다.

## 업그레이드

v1.0.0 초기 설치기는 기존 설치 덮어쓰기나 자동 업그레이드를 제공하지 않습니다. 다음 절차는 운영자가 수행하는 유지보수 과정입니다.

1. 새 릴리스 source와 checksum을 확인하고 별도 checkout/venv에서 frozen 설치·기본 회귀·build를 검증합니다. 필요한 경우 `deployment stage`로 새 private prefix의 설치 리허설을 수행합니다.
2. 운영 DB·설정의 private 복사본으로 새 코드의 migration·무결성·권한·기존 행 보존을 확인합니다. 실제 운영 설정으로 테스트를 실행하지 않습니다.
3. 신규 예약/실행 접수를 보류하고 기존 모델·SMTP 시도가 자연 종료되었는지 확인합니다. DATA 처리 중 강제 종료하면 발송이 불확실해질 수 있으므로 상태를 먼저 확인합니다.
4. Web·worker·SMTP·scheduler timer/oneshot을 함께 중지하고 DB와 관련 파일의 검증된 백업을 만듭니다.
5. 새 불변 release 디렉터리에 검증한 코드·schema·non-editable venv를 준비합니다. 기존 release와 설정·DB를 덮어쓰지 않고 `current` 선택만 전환합니다. 제공 unit 또는 실행 경로 변경이 있으면 별도로 검토·검증합니다.
6. 새 버전으로 DB를 초기화/열어 필요한 migration을 수행하고 `doctor`, integrity·자료 보존·HTTP 권한을 확인합니다.
7. 중지 전 운영 상태에 맞춰 모든 서비스를 **같은 릴리스**로 재개하고 실제 proxy·예약 timer·큐 상태를 확인합니다. 기능 검증을 위해 업무 Task를 임의 재실행하지 않습니다.

DB schema v3에는 사용자 역할·자원 소유권이 포함됩니다. 구버전과 새 worker를 혼합 실행하지 않습니다. `current` 링크를 바꾸는 것은 DB migration을 되돌리지 않습니다. 신규 쓰기가 발생한 뒤에는 과거 DB를 자동 복원하지 않고 현재 schema와 호환되는 수정 릴리스로 복구합니다.

## 오프라인 DB 복원

복원은 선택한 backup 이후의 상태를 잃을 수 있는 운영 작업입니다. 특히 이미 전송된 SMTP 상태를 과거로 되돌리면 중복 발송 위험이 있으므로 자동 장애 복구 용도로 실행하지 않습니다.

```bash
systemctl --user stop researchops-scheduler.timer researchops-scheduler.service researchops-worker.service researchops-smtp.service researchops-web.service
"$RESEARCHOPS_INSTALL_DIR/current/.venv/bin/python" -m researchops.operations restore "$RESEARCHOPS_INSTALL_DIR/data/backups/selected.db" "$RESEARCHOPS_INSTALL_DIR/data/researchops.db" --offline
```

`selected.db`는 실제로 검증해 선택한 백업 파일명으로 바꿉니다. 직접 시작한 CLI/SQLite 프로세스·모델 child도 종료되어야 합니다. 도구는 system/user unit·timer 상태, maintenance lock과 DB open-file 상태를 확인하며 불명확하면 거부합니다. `--offline`은 살아 있는 프로세스를 우회하는 옵션이 아닙니다.

원본 DB/WAL/SHM은 pre-restore backup으로 보존합니다. 중단 marker가 남았으면 원인을 해결하기 전 앱 시작을 계속하지 않습니다. 복원 DB·Task 버전·archive·delivery 설정의 일치와 schema 호환을 확인하고 발송 불확실 상태를 별도로 검토한 뒤 이전 운영 상태에 맞춰 서비스를 재개합니다. 기존 백업과 release는 검토 없이 삭제하지 않습니다.
