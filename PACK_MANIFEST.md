# ResearchOps stable 배포 구성

이 배포본은 현재 애플리케이션 소스·스키마·합성 테스트·가상 예제·배포 템플릿·정리된
문서로 구성한다. 이전 Git 이력, 개인별 운영 승인 기록, 실제 운영 Task와 메일 작성 규칙,
특정 Task용 일회성 마이그레이션, 실제 서비스에 연결하는 현장 검증 스크립트,
운영 설정·DB·로그·archive·계정·인증파일·백업은 포함하지 않는다.

- `researchops/`: Core, CLI, Web, runner, SMTP, storage 및 배포 도구.
- `schemas/`: Task·결과·메일·전달 계약.
- `tasks/software-releases/`, `examples/`: 가상 예제와 합성 검증 입력.
- `tests/`: 독립 runtime 회귀와 명시적 opt-in 검증 도구.
- `deploy/`, `templates/`, `bin/`: Linux/systemd·프록시·백업 운영 보조 도구.
- `docs/`: 사용·설계·운영·검증 안내.
- `scripts/`, `.github/`: 릴리스 구성 확인과 CI.

`FILE_TREE.txt`는 배포 소스의 정확한 목록이다. `CHECKSUMS.sha256`은 자신을 제외한
소스 파일들의 SHA-256이다. 릴리스 첨부의 `SHA256SUMS`는 wheel과 sdist를 검증한다.
Wheel에는 실행에 필요한 Python 코드·DB schema·브랜드 아이콘·JSON schema·예제가 포함되고,
sdist에는 문서·테스트·배포 템플릿도 포함된다. 개발은 checkout 또는 sdist에서 시작한다.

```bash
python3 scripts/release_check.py
sha256sum --check --strict CHECKSUMS.sha256
```

새 저장소는 이 검토된 사본에서 독립된 최초 커밋으로 시작한다. 커밋 작성자는
개인을 식별하지 않는 `ResearchOps Maintainers <maintainers@example.invalid>`를 사용한다.
GitHub 저장소·릴리스의 소유 계정은 GitHub 플랫폼의 소유권 표시이며 소스에 복사하지 않는다.
