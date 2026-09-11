# ResearchOps 시작 안내

처음 사용하는 사람은 [README](README.md)에서 지원 범위를 확인하고 다음 순서로 진행합니다.

1. [사용자 소유 설치](docs/USER_DEPLOYMENT.md): Python·uv 준비, 빈 설치 디렉터리, 서비스 등록.
2. [로그인과 권한](docs/WEB_UI_AUTH.md): 일회용 토큰으로 첫 관리자 개설, 사용자·조회자 등록.
3. [운영 가이드](docs/PRODUCTION_OPERATIONS.md): 발신 계정·수신자 그룹·Task·실행·예약.
4. [백업과 업그레이드](docs/BACKUP_AND_UPGRADE.md): DB·설정·archive 보존과 전체 서비스 전환.

코드를 살펴보려면 [아키텍처](docs/03_ARCHITECTURE.md), [Task 계약](docs/05_TASK_DEFINITION.md), [구현 불변조건](docs/HARDENING.md), [테스트 전략](docs/15_TESTING_STRATEGY.md)을 읽습니다. 전문 계약은 [문서 목차](docs/README.md)에서 찾을 수 있습니다.

기본 흐름은 Task 저장 → 수동 또는 예약 enqueue → Research → 구조·파일 검증 → 불변 Compose 입력 → Compose → 이메일 검증 → archive → SMTP 큐입니다. 새 Task 저장만으로 실행하지 않습니다. 실제 발송이 켜진 Task의 실행·예약은 별도 승인 화면 없이 발송으로 연결됩니다.

공개 예제는 가상 소프트웨어인 Atlas Notes와 Cedar Board의 변경 공지를 정리합니다. 원래 운영환경의 조사 주제·Task 지시문·메일 규격을 재현하지 않습니다. 주소와 인증정보는 예제를 수정해 저장소에 넣지 말고 보호된 운영 설정에서 입력합니다.

개발 검증은 독립 runtime을 사용합니다. 기존 DB를 여는 명령은 migration을 실행할 수 있으므로 테스트와 문서 검토에는 운영 설정을 연결하지 않습니다.
