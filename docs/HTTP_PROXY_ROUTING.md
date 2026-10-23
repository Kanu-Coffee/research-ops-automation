# 공개 HTTP의 선택적 프록시

Research의 공개 자료 조회와 앱의 `official_image` 취득에 같은 hostname 기반 proxy profile을
사용한다. 사이트별 API나 이미지 URL을 앱 코드에 넣지 않는다. 프로필은 서버 운영자가
설정하고 Task는 이름만 선택한다. 생략한 Task는 기존 직접 경로를 유지한다.

```yaml
# 서버 settings.yaml
http_proxy_profiles:
  public-docs:
    proxy_url: http://127.0.0.1:18888
    host_patterns:
      - docs.example.org
      - '*.docs.example.org'
      - assets.example.org
      - '*.assets.example.org'

# Task task.yaml의 runner 안
runner:
  network_profile: public-research
  allow_http_requests: true
  http_proxy_profile: public-docs
```

프로필 endpoint는 인증 없는 literal loopback HTTP origin만 지원한다. 별도 proxy·터널을
설치하거나 CardRAG를 변경하지 않는다. 외부 proxy 인증정보를 worker에 전달하지 않는다.
예시 endpoint는 가상 설정이며, 운영자가 준비한 loopback CONNECT proxy를 지정한다.

패턴은 정규화한 hostname 전체 또는 `*.` 접미 도메인에만 적용한다. wildcard는 apex를
포함하지 않으므로 필요하면 별도로 적는다. 모든 path/query에 적용하며 유사 문자열·다른
port·userinfo를 같은 도메인으로 취급하지 않는다. wildcard는 라우팅 선택이며 앱의 기존
정확한 image host 허용 목록을 확대하지 않는다.

## Research client

HTTP ON인 Research에만 `.researchops-submit` 안의 `public_http`와 self-contained fetch
transport를 제공한다. 호출 안내의 `submission_helper`를 `sys.path`에 추가한다.

```python
from researchops.runners.public_http import fetch_public

r = fetch_public(url, timeout_seconds=20, max_bytes=1_048_576)
html = r.text("utf-8")  # 실제 응답 charset이 EUC-KR 등이면 그 값을 사용
r = fetch_public(query_url, method="POST", json_body={"product": "actual-id"},
                 headers={"X-Requested-With": "XMLHttpRequest"})
```

client는 HTTPS, GET/HEAD 및 명시적 읽기 목적 JSON POST를 지원한다. JSON body·header·응답
크기·전체 timeout·redirect 횟수를 제한한다. 인증/Cookie/Host/transfer 제어 header는 거부한다.
POST의 업무상 읽기 여부는 Task와 신뢰 worker의 지시 계약이며 HTTP 메서드만으로 서버의
외부 변경 의미를 기술적으로 판정한 것은 아니다. 기본 앱 media fetcher는 계속 GET/HEAD만
허용한다. 주문·신청·로그인·설정 변경에는 사용하지 않는다.

매 redirect마다 새 목적지를 검사하고 라우트를 재선택한다. 다른 origin으로 POST body를
보존하는 307/308은 거부하며, GET으로 전환하는 301/302/303에는 기존 body를 보내지 않는다.
cross-origin redirect에는 요청자 header를 전달하지 않는다. 쿠키·세션을 공유하지 않는다.

## 연결과 검증

모든 목적지 DNS 응답을 공인 IP로 검증한 후, proxy에는 **숫자 IP:443**으로 CONNECT한다.
그 터널에서 원래 hostname을 SNI로 사용하고 인증서를 검증한다. localhost는 고정 proxy
transport만의 예외이며 target URL의 loopback/private/metadata 접근은 계속 거부한다.
목적지 IP와 proxy peer는 별도로 검사한다. proxy 원격 DNS에 목적지 검증을 맡기지 않는다.
proxy 실패 후 직접 연결로 fallback하지 않는다. audit는 hop별 direct/proxy·원래 origin·
URL hash·검증한 목적지 IP·응답 상태를 기록하며 proxy endpoint나 POST 내용을 로그에 넣지 않는다.

동일 snapshot의 profile이 worker client, `acquire_file`의 공식 이미지 취득, Research 뒤
`official_image` 취득에 전달된다. protected CardRAG PDF는 기존 인증 transport를 유지한다.
Compose/SMTP에는 proxy 능력을 전달하지 않는다. Compose-only는 기존 파일을 재사용한다.

원 URL·record 연결·취득 맥락은 기존 evidence에 남긴다. 최종 파일은 기존 MIME·형식·hash·
용량·record/CID·전달 무결성 검사를 거친다. 이미지 미확보는 기존 continue/announce 정책을
따르며, 신규 수동 검수·승인 단계는 없다.

## 강제 범위

공통 client와 앱 transport 내부에서는 위 검사를 적용한다. HTTP ON인 신뢰 native worker는
셸 네트워크 능력을 가지므로 client 사용 지시가 모든 도구·호스트 접근의 OS 강제 격리는
아니다. staging의 helper/profile은 인증정보가 없는 호출 편의 기능이며 앱의 신뢰 설정을
대체하지 않는다. provider 측 native 웹 검색/리더와 모델 API·MCP·SMTP는 이 proxy로
전역 라우팅하지 않는다. 별도 환경변수 HTTPS_PROXY 설정도 하지 않는다.

관련 검증은 `test_http_proxy`, `test_research_fetch`, `test_production_runner`,
`test_research_acquisition`, `test_media_pipeline`을 따른다. 실제 proxy 왕복과 native 모델 동작은 별도 명시적 검증 범위다.
