# 14. 후속 개발 범위

v1.0.0에 포함된 기능은 [릴리스 범위](STATUS.md)에 정리합니다. 아래 항목은 현재 제공 기능이나 완료된 보장으로 표현하지 않습니다.

| 영역 | 후속 검토 내용 |
|---|---|
| 적대적 Task 격리 | credential/control 분리, 모든 egress 경로 제어, Task별 영구 bytes/inode quota |
| Provider 동등성 | Antigravity phase별 예방적 도구·MCP 차단, CLI 변화에 대한 실제 호환 검증 |
| 운영 자동화 | 검증된 upgrade controller, 배포·migration·복구 상태의 자동 관리 |
| 가용성 | 다중 호스트 실행·분산 scheduler·active-active DB 전략 |
| 계정 관리 | 명시적인 자원 소유권 이전, 필요한 조직 인증·복구 흐름 |
| Dedupe 정책 | 직전 상태 비교·시간 window·TTL·재알림과 기존 정확 일치 정책의 구분 |

후속 변경은 검증 가능한 작은 단위로 진행하며 사용자 지시·현재 권한·실제 소스 계약을 확인합니다. 미완성 적대적 Task 격리를 이유로 이미 제공하는 신뢰 운영 경로 전체가 실행 불가라고 설명하지 않습니다.
