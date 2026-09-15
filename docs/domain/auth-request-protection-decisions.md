# 인증 요청 보호 구현 결정

대상: [#44](https://github.com/Yumetri/MDM_example/issues/44)
정본: [#22](https://github.com/Yumetri/MDM_example/issues/22)

이 문서는 #44 구현 검수에서 승인된 보완 사항을 기록한다.

## CSRF 회전 이전 값의 재사용

2026-09-15 검수에서 다음 해석을 확정했다.

- CSRF 값은 서버에 저장하지 않고, cookie와 header의 strict format 및 timing-safe equality를
  검사한다. 기존 목적별 256-bit CSPRNG와 canonical base64url 계약을 유지한다.
- 회전 후 cookie가 새 값 `B`이고 header가 이전 값 `A`이면 불일치로 거부한다.
- cookie와 header가 모두 이전 값 `A`이면 CSRF 일치 검사는 통과한다. 서버가 이전 CSRF 값을
  저장하거나 세션과 결합하지 않으므로 이 검사만으로 이전 값임을 판별하지 않는다.
- 이 결과가 요청 전체의 성공을 의미하지는 않는다. exact Origin 검사와 해당 endpoint의
  인증 요건은 별도로 적용하며, refresh token의 유효성·회전·재사용 처리는 #37이 담당한다.
- 회전 이전 값 재사용 테스트는 위 두 경우를 구분한다. 서버 상태나 암호학적 결합을 추가해
  이전 CSRF 값 쌍을 거부하는 정책은 이번 확정 범위에 포함하지 않는다.

## 운영 로그의 검증된 client IP

2026-09-15 검수에서 #22의 원본 IP 기록에 관한 상충 문구를 다음과 같이 구분했다.

- rate limit 저장소의 key는 서버 secret을 사용하는 HMAC-SHA-256 IP digest다.
  저장소에는 원본 IP를 보존하지 않는다.
- 인증 실패·보호 운영 이벤트에는 신뢰 경계에서 검증된 `client_ip`만 선택적으로 기록한다.
  IP를 확정할 수 없으면 필드를 생략하며 미검증 전달 header로 대체하지 않는다.
- 운영 로그의 보존기간은 14일이며 실제 수집·보존 정책 설정은 배포 티켓 #34가 담당한다.
- 기존 로그의 비밀값 금지, provider-neutral 출력 및 출력 실패가 요청 결과를 바꾸지 않는
  계약을 유지한다.

## #44 완료 범위와 후속 티켓

2026-09-15 검수에서 실제 인증 endpoint가 없는 단계의 완료 범위를 확정했다.

- #44는 재사용 가능한 Origin·CSRF 검사, cookie 처리, 공용 rate limit 및 API 연결 부품을
  제공한다. 테스트 전용 endpoint에 실제 부품을 연결해 인증 흐름별 보호 행렬을 검증한다.
- HTTP 테스트에서 거부 시 업무 처리·DB 접근이 실행되지 않고 cookie가 유지되는지 확인한다.
- 실제 인증 endpoint 연결과 세션 DB 검증은 아래 후속 티켓이 담당한다.

2026-09-15 GitHub에서 다음 티켓의 존재, OPEN 상태 및 구현 책임을 확인했다.

| 티켓 | 확인된 책임 |
| --- | --- |
| [#37](https://github.com/Yumetri/MDM_example/issues/37) | #44 직접 의존. login·refresh·logout 보호 연결, cookie 처리 및 refresh SQL 상태 검증 |
| [#38](https://github.com/Yumetri/MDM_example/issues/38) | 가입 신청·완료의 공용 제한, 완료 Origin 검사, 자동 로그인 cookie 발급 |
| [#39](https://github.com/Yumetri/MDM_example/issues/39) | 재설정 공용 제한, 로그인 상태 변경의 Origin·CSRF·rate limit 순서 및 cookie 처리 |
| [#34](https://github.com/Yumetri/MDM_example/issues/34) | Cloud Run client IP adapter·조작 header 검증, IP-HMAC secret 주입, 로그 14일 보존 및 단일 instance/worker 검증 |

#38의 자동 로그인 문구 중 CSRF digest를 저장한다는 부분은 검수 승인 후 GitHub 본문에서
수정했고 다시 읽어 반영을 확인했다. refresh digest만 사용자 생성 transaction에 저장하며,
CSRF 원본·digest는 서버에 저장하지 않고 두 raw 값 모두 commit 성공 후에만 응답한다.

## 보호 검사 순서와 quota 소비 범위

2026-09-15 검수에서 #22의 모든 성공·실패 요청 quota 문구와 #39의 검사 순서를 다음과 같이
조정했다.

- 모든 인증 흐름에서 필요한 검사를 `Origin → CSRF → rate limit` 순서로 실행한다.
- Origin·CSRF 실패는 403으로 먼저 거부하며 quota를 소비하지 않는다.
- 이 검사를 통과한 요청은 본문·비밀번호·token 검증의 성공·실패와 관계없이 quota를 소비한다.
- Origin·CSRF가 필요 없는 대상은 바로 공용 quota를 소비한다. `/auth/me`와 logout은
  기존 계약대로 rate limit에서 제외한다.

## 설정과 구성 실패

2026-09-15 검수에서 다음 설정 정책을 확정했다.

- `MDM_AUTH_IP_HMAC_SECRET`: 독립적인 32바이트 난수를 padding 없는 canonical base64url
  43자로 주입한다. 고정 기본값이나 자동 생성 대체값은 없다.
- 형식 오류는 typed settings 검증에서 실패하며 값은 오류 문자열에 표시하지 않는다.
  누락은 인증 보호 부품을 구성할 때 실패한다. 아직 인증 API를 연결하지 않은 앱에는
  secret을 필수로 강제하지 않는다.
- `MDM_AUTH_ALLOWED_ORIGINS`: JSON 배열로 설정한다. 기본 빈 배열은 Origin이 필요한
  요청을 모두 거부한다. HTTP(S) scheme·소문자 host·effective port로 비교하며, path,
  query, fragment, userinfo, 공백 및 복수 Origin은 거부한다. DNS host는 브라우저가
  직렬화한 ASCII 형식이며 국제화 host는 A-label 형식을 사용한다.
- 기본 cookie는 `Secure=true`이며 allowlist에는 HTTPS Origin만 허용한다.
- `MDM_AUTH_ALLOW_INSECURE_LOCAL_COOKIES=true`를 명시하고 allowlist가 비어 있지 않으며
  전체가 `http://localhost[:port]`, `http://127.0.0.1[:port]`, `http://[::1][:port]`일 때만
  `Secure=false`를 허용한다.
- `MDM_AUTH_RATE_LIMIT_CAPACITY` 기본값은 100,000이다. 제한은 최초 요청부터 고정된
  60초당 30회이며 monotonic clock을 사용한다. 만료 entry는 요청당 최대 256개씩 정리하고
  현재 조회 key의 만료도 확인한다. 전체 map·expiry bookkeeping은 capacity에 묶인다.

## 후속 구현 연결 방법

- composition root에서 `build_auth_protection(settings)`를 앱 생성 시 한 번 호출하고 반환된
  `AuthProtectionComponents`를 모든 인증 흐름에 공유한다. 요청마다 구성하면 공용 quota가
  초기화되므로 허용하지 않는다.
- 반환된 `components.lifespan`을 `FastAPI(lifespan=components.lifespan)`에 연결한다.
  기존 앱 lifespan이 있으면 그 안에서 `async with components.lifespan(app):`로 감싼다.
  기본 로그 스레드는 이 범위에서만 동작한다. 같은 components는 한 번의 앱 lifespan에만
  사용하고, 앱을 다시 시작할 때는 새로 구성한다. #37의 실제 API 연결 시 함께 적용한다.
  직접 주입한 `event_sink`는 호출자가 수명을 관리하며 `emit`이 출력 I/O를 기다리지 않아야 한다.
- API builder에 `components.router(AuthAction.LOGIN)` 등 해당 동작의 router를 주입한다.
  이 router는 본문 JSON 해석·FastAPI dependency 실행 전에 Origin·CSRF·quota를 검사한다.
  테스트 전용 probe는 production 앱에 등록하지 않는다.
- 공용 403·429 OpenAPI 응답은 router에 포함된다. 후속 API는 실제 성공·credential 오류와
  인증 요구사항을 별도로 문서화하고 해당 use case를 연결한다.
- `components.cookies.issue(response, refresh=..., expires_at=..., now=...)`는 DB commit 성공
  후에 호출한다. 기존 family의 절대 만료 시각을 전달하며 refresh 회전이 만료를 연장하지
  않도록 한다. CSRF 값은 호출할 때마다 새로 생성하고 cookie 응답 외 서버 상태에 남기지 않는다.
- logout 및 refresh credential 거부에는 `components.cookies.clear(response)`를 호출한다.
  Origin·CSRF 403 및 refresh conflict 409에서 cookie를 삭제하지 않는다.
- 기본 `UnresolvedClientIpResolver`는 peer나 전달 header를 신뢰하지 않고 `None`을 반환한다.
  이때 제한은 생략하고 `CLIENT_IP_UNRESOLVED` 경고를 출력한다. #34에서 검증된 adapter를
  주입한 뒤에만 실제 client IP에 기반한 제한과 IP 로그가 적용된다.
- Uvicorn의 `FORWARDED_ALLOW_IPS=*` 또는 임의 `X-Forwarded-For`를 신뢰 근거로 사용하지 않는다.
  resolver는 전달 header 원문을 반환하지 않으며 검증된 IP 주소 객체 또는 `None`만 반환한다.
- 메모리 제한은 instance 1·worker 1을 전제로 한다. restart·deploy·scale-to-zero 초기화는
  승인된 잔여 위험이다. scale-out 전 공유 원자 TTL 저장소로 교체한다.

## CSRF 오류 응답

2026-09-15 검수에서 외부 오류 응답을 다음과 같이 확정했다.

- HTTP 상태: `403`
- RFC 9457 `code`: `CSRF_VALIDATION_FAILED`
- `type`: `/problems/csrf-validation-failed`
- 제목: `CSRF 검증 실패`
- 설명: `유효한 CSRF cookie와 X-CSRF-Token header가 일치해야 합니다.`
- 누락·중복·형식 오류·불일치는 모두 같은 응답을 사용하며 기존 cookie를 유지한다.

## 운영 로그 출력 지연과 대기열

PR #70 리뷰 검수에서 운영 로그의 출력 지연이 async API 처리를 막지 않도록 다음을 확정했다.

- 인증 보호의 기본 운영 로그 출력은 크기가 제한된 메모리 대기열과 전용 출력 스레드 1개를
  사용한다. 요청 처리는 이벤트를 대기열에 넣기만 하며 JSON 직렬화·write·flush를 기다리지 않는다.
- `MDM_AUTH_LOG_QUEUE_CAPACITY`는 1 이상의 정수이며 기본값은 1,024건이다.
  현재 출력 중인 1건은 대기열 용량과 별도다.
- 대기열이 가득 차면 새 운영 로그를 버리고 API 처리를 계속한다. 출력 실패도 요청 결과를
  바꾸지 않으며, 실패를 같은 출력 경로로 재기록하지 않는다.
- 이 정책은 best-effort 운영 로그에만 적용한다. DB에 보존하는 감사 기록에는 적용하지 않는다.
- 기본 스레드는 앱 시작 시 시작한다. 앱 종료 시 새 로그 접수를 중단하고 최대 1초 동안
  대기 로그 출력을 기다린다. 종료 대기도 이벤트 루프를 막지 않는다. 기한 이후 남은 대기
  로그는 버리며, 이미 출력 중인 1건은 강제로 중단할 수 없다. daemon 스레드로 두어 프로세스
  종료 시 이 출력의 완료를 기다리지 않는다. 시작 전·종료 후 이벤트는 버린다.
- #37에서 lifespan 연결을 적용하고, #34에서 실제 로그 수집·보존과 함께 용량 설정 및
  출력 지연·종료 동작을 검증한다. 기존 두 후속 티켓이 OPEN임을 이번 리뷰 수정 시 재확인했다.
