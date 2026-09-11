# 17. 배포 개요

권장 경로는 Linux 단일 호스트의 사용자 소유 설치입니다. Python 3.12 이상, uv, local SQLite를 사용하며 자동 서비스 예제는 systemd 254 이상과 cgroup v2 위임을 요구합니다. [설치 가이드](USER_DEPLOYMENT.md)에 실제 지원 명령을 정리했습니다.

`python -m researchops.deployment user-preflight`는 빈 설치 루트의 메타데이터를 확인하고, `user-install`은 계획을 출력하며 `--apply`를 주면 새 release·venv·설정·unit을 만듭니다. 설치기는 DB 초기화·서비스 등록/시작·CLI 로그인·SMTP 설정을 대신하지 않습니다. 기존 루트에는 덮어쓰지 않습니다.

`stage` 명령은 외부 영향 없는 별도 설치 리허설입니다. `deploy/systemd/`는 전용 계정을 사용하는 관리형 서비스 예제이며 자동 root 설치기는 아닙니다. `deploy/systemd-user/`가 사용자 설치기의 실제 렌더링 입력입니다.

운영에서는 Web·research worker·SMTP worker·scheduler를 같은 코드 버전으로 사용합니다. 외부 Web은 TLS reverse proxy와 실제 peer·Host·Origin·CSRF 검사를 유지합니다. 초기 SMTP·예약 비활성값과 사용자가 이후 켠 production 상태를 구분합니다.

업그레이드·DB schema 호환·복원은 [백업과 업그레이드](BACKUP_AND_UPGRADE.md)를 따릅니다. 설치 호스트와 인증·외부 SMTP·네트워크 상태는 이 저장소의 릴리스만으로 검증되지 않습니다.
