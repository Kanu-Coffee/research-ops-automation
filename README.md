# ResearchOps

**v1.0.0** · 신뢰할 수 있는 내부 사용자를 위한 조사·이메일 자동화 도구

ResearchOps는 Codex CLI 또는 Antigravity CLI로 자료를 조사하고, 조사 결과를 바탕으로 이메일을 작성한 뒤 검증된 원문과 첨부를 SMTP로 전달하는 Python 애플리케이션입니다. Web과 CLI가 같은 실행 큐·Task 버전·감사 이력을 사용하며, 예약과 업무 날짜는 `Asia/Seoul`을 기준으로 합니다.

## 제공 기능

- Research → 결과 검증 → Compose → 이메일 검증 → SMTP 전송의 두 단계 AI 실행
- 단계별 엔진·모델·사고 수준, 전체 재실행·메일 재작성·작성된 이메일만 전송
- Task별 고정 workspace, 불변 버전·실행 archive, 취소·timeout·중복 처리 방지
- 관리자·사용자·조회자 로그인, 자원 소유권과 Task별 조회 권한
- 여러 발신 계정과 수신자 그룹, 서울 시간 예약, 발송 상태·파일·로그 진단
- PDF·이미지 취득 및 첨부 무결성, 선택적 정확한 내용 dedupe와 검증된 전달 이력
- 사용자 소유 Linux 설치, systemd 서비스 예제, SQLite 백업·오프라인 복원

AI 입력에는 실제 수신자 주소나 SMTP 비밀번호를 제공하지 않습니다. AI가 선택한 수신자 그룹을 앱이 해석하고, 통과한 제목·HTML·plain text를 다시 작성하지 않고 전달합니다. SMTP 성공은 서버의 수락이며 수신함 도착을 보장하지 않습니다. 전송 여부가 불확실한 메시지는 자동 재발송하지 않습니다.

## 빠른 확인

Python 3.12 이상과 `uv`가 필요합니다. 아래 명령은 저장소 checkout에서 실행합니다.

```bash
uv sync --frozen
uv run --frozen researchctl version
uv run --frozen researchctl validate-runners --runner fake --json
uv run --frozen python -m unittest discover -s tests -v
uv build --no-sources
```

`fake` 검증은 새 임시 runtime에서 합성 입력으로 전체 파이프라인을 확인합니다. 실제 AI 호출이나 이메일 발송은 하지 않습니다. 소프트웨어 릴리스 모니터링을 가정한 [software-releases 예제](examples/tasks/software-releases/README.md)는 가상의 Atlas Notes·Cedar Board와 예약 도메인만 사용합니다.

실제 사용은 [설치 가이드](docs/USER_DEPLOYMENT.md) → [계정 개설](docs/WEB_UI_AUTH.md) → [운영 가이드](docs/PRODUCTION_OPERATIONS.md)를 따르세요. 설치기는 빈 디렉터리에 실행 파일·기본 설정을 만들며, SMTP와 예약은 처음에 꺼져 있습니다. 운영자가 발신 계정·그룹을 등록하고 실제 발송 설정을 켠 이후의 실행·예약은 외부 이메일 전송으로 이어집니다.

## 지원 범위

Linux 단일 호스트, 로컬 SQLite, Python 3.12 이상을 기준으로 합니다. 사용자 서비스 예제는 systemd 254 이상과 cgroup v2의 해당 사용자 서비스 위임을 요구합니다. Web 외부 접속은 TLS reverse proxy를 사용합니다.

Production native runner는 **신뢰된 내부 사용자의 작업 실행 모드**입니다. Codex의 workspace-write와 Antigravity의 호출별 자동 승인·sandbox 해제는 적대적 Task의 강제 격리를 보장하지 않습니다. 동일 OS 계정의 파일 접근, 모든 외부 통신, 영구 디스크 quota에 대한 완전한 격리는 제공하지 않습니다. [보안 경계](docs/12_SECURITY.md)를 확인한 뒤 사용할 계정과 호스트를 선택하세요.

이 릴리스에는 실제 운영 계정·주소·비밀번호·DB·로그·Task 문서를 포함하지 않습니다. 사용 가이드의 계정·호스트·프로젝트는 익명 예시이며, 특정 운영 환경의 구축 또는 실발송 성공을 보장하는 자료가 아닙니다.

## 문서와 개발

[시작 안내](START_HERE.md) · [전체 문서 목차](docs/README.md) · [릴리스 범위](docs/STATUS.md) · [설계 결정](docs/DECISIONS.md) · [개발 지침](AGENTS.md) · [기여 안내](CONTRIBUTING.md) · [보안 보고](SECURITY.md)

기본 회귀는 fake runner·합성 자료·SMTP double로 실행합니다. 실제 모델이 필요한 별도 개발 검증은 명시적인 `--live` 옵션을 사용하며, 비용이 발생할 수 있습니다. [검증 가이드](docs/15_TESTING_STRATEGY.md)에 실행 명령과 결과 해석을 정리했습니다.
