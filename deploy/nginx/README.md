# TLS reverse proxy 설정

ResearchOps Web은 자체 관리자·사용자·조회자 로그인을 제공합니다. 기본 upstream은 같은 호스트의 `http://127.0.0.1:8765`이고 외부 브라우저는 TLS 도메인으로 접속합니다. 이 문서의 `researchops.example.org`, `192.168.50.20`, `192.168.50.10`은 설명용 대체값이며 운영 주소가 아닙니다.

## 같은 호스트의 NPM/Nginx

1. 운영자 도메인의 DNS와 유효 TLS 인증서를 준비합니다.
2. NPM Proxy Host에 도메인, HTTP upstream `127.0.0.1`, port `8765`를 설정합니다. NPM 컨테이너 내부의 loopback과 앱 호스트 loopback은 다르므로 실제 같은 네트워크의 경로인지 확인합니다.
3. Force SSL과 필요한 TLS 정책을 적용합니다. 앱 설정 `web.allowed_hosts`에도 같은 실제 도메인을 등록합니다.
4. [Advanced configuration](researchops-npm.conf)을 적용하여 Host·Origin과 앱의 CSP를 보존하고 forwarding header를 proxy에서 덮어씁니다.
5. 앱 서비스를 시작하고 TLS 도메인에서 초기 관리자 개설·로그인을 확인합니다. NPM Access List를 추가로 사용하는 경우 원하는 인증 정책을 유지할 수 있으며 Basic 인증정보를 앱으로 넘기지 않습니다.

Production 앱 쿠키는 Secure입니다. 초기 설치 upstream의 직접 HTTP 화면으로 로그인하려고 insecure auth를 켜지 말고 TLS proxy를 먼저 구성합니다. [관리자 개설](../../docs/WEB_UI_AUTH.md)의 일회용 토큰을 보호된 서버 터미널에서 발급받습니다.

## 별도 호스트 proxy

VPN 또는 신뢰할 수 있는 private 연결을 사용하는 원격 proxy라면 앱에 해당 인터페이스와 **실제 TCP socket peer**를 명시합니다. 아래는 예시입니다.

```yaml
web:
  enabled: true
  bind: 192.168.50.20
  port: 8765
  allow_remote_proxy: true
  allow_insecure_local_auth: false
  trusted_proxy_cidrs:
    - 192.168.50.10/32
    - 127.0.0.1/32
  allowed_hosts:
    - researchops.example.org
    - localhost
    - 127.0.0.1
  require_origin_check: true
  csrf_protection: true
```

NPM upstream도 `http://192.168.50.20:8765`로 바꿉니다. 이 예시는 사설 주소를 요구하는 parser 계약을 보여주기 위한 것이며 네트워크 연결·VPN을 생성하지 않습니다. Router SNAT가 있으면 앱에서 관찰한 정확한 peer를 확인합니다. 공인 proxy IP나 브라우저 IP를 무조건 peer로 등록하지 않습니다.

Remote mode는 literal RFC1918/ULA bind, 최대 8개의 정확한 `/32` 또는 `/128` peer만 허용합니다. Wildcard·공개 bind·광범위 subnet 신뢰는 거부합니다. Forwarded header는 실제 peer 검사를 대체하지 않습니다. 앱 필터가 호스트 방화벽이나 VPN의 안전성을 증명하는 것은 아닙니다.

## 요청과 응답 경계

Proxy의 `client_max_body_size`는 `2m`으로 설정합니다. 앱의 정확한 요청 상한은 기본 **2,000,000 bytes**이며 한글 두 지시문의 form encoding을 수용합니다. Proxy에서 더 작은 상한을 걸면 앱이 허용하는 Task 저장도 413으로 거부됩니다.

앱의 route별 CSP를 전역 proxy CSP로 덮어쓰지 않습니다. 이메일 preview와 HTML/SVG artifact는 강화된 격리·다운로드 정책을 유지합니다. Origin·CSRF·Host 검사를 끄지 않고 incoming X-Forwarded-* 헤더를 그대로 신뢰하지 않습니다.

## 확인

TLS 도메인에서 미인증 페이지는 로그인으로 이동하고 미인증 API·자료는 거부되는지 확인합니다. 잘못된 Host·Origin·CSRF 누락·직접 미허용 peer·위조 forwarding header를 거부해야 합니다. 로그인·로그아웃·Task 조회·허용된 변경을 왕복 검증합니다.

502는 upstream 경로·서비스를, 403은 실제 peer·Host·Origin/CSRF를 먼저 확인합니다. 문제 해결을 위해 allowlist를 넓히거나 보호 검사를 끄지 않습니다. 로컬 단위 테스트는 실제 TLS·routing·proxy 인증·방화벽 검증을 대신하지 않습니다.
