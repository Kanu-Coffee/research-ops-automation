# 기여·개발 안내

Python 3.12 이상과 기존 `uv` 설치를 사용한다. 개발은 별도 checkout과 임시 runtime에서
진행하고, 운영 설정을 읽거나 업무 Task·SMTP·예약을 회귀 검증에 사용하지 않는다.

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen python -m compileall -q researchops
shellcheck bin/*.sh
actionlint
uv run --frozen python scripts/release_check.py
uv build --no-sources
```

소스 변경 뒤 릴리스 파일 목록과 SHA-256을 갱신하려면
`uv run --frozen python scripts/release_check.py --write`를 실행한다.
검사는 DB·비밀 설정·운영 프롬프트를 포함하지 않는 소스 목록을 사용한다.
변경 파일을 먼저 검토하고, 새 파일이 배포 대상인지 확인한다.

코드는 CLI와 Web이 공유하는 application service에 업무 규칙을 두고, 모델이 만든
이메일 원문과 검증된 첨부의 무결성을 유지한다. 구조·권한·중복 방지·전달 경계에 영향을
주는 변경에는 합성 회귀 검증을 추가한다. 단순 문서 수정에는 별도 단위 테스트가 필요 없다.
설계·운영 동작이 바뀌면 해당 가이드와 [변경 이력](CHANGELOG.md)을 함께 갱신한다.

기본 테스트는 실제 AI 호출과 SMTP 발송을 하지 않는다. 이름에 `live_`가 있는 독립 도구와
CLI의 `--live`는 자동 회귀 대상이 아니며, 별도 격리 환경에서 명시적으로 실행한다.
브라우저 검증은 기존 `tests/browser_ui_qa.py`를 사용하며 설치된 Firefox·geckodriver·Node가 필요하다.
새 의존성을 이 검증만을 위해 전역 설치하지 않는다.

보안 문제는 공개 이슈에 실제 값·취약한 운영 주소를 기록하지 않고
[보안 안내](SECURITY.md)를 따른다. 운영 설정·Task 원문·실제 로그·스크린샷을 커밋하지 않는다.
이 저장소에는 오픈소스 라이선스가 부여되어 있지 않다. 외부 재배포 허용 범위는 소유자가
별도로 정한다.
