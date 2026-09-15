# 로그인·refresh 세션 계약

구현 티켓: [#37](https://github.com/Yumetri/MDM_example/issues/37).
인증 정본 [#22](https://github.com/Yumetri/MDM_example/issues/22)와
공통 요청 보호 [#44](https://github.com/Yumetri/MDM_example/issues/44)를 따른다.

## API

| 경로 | 성공 | 인증·보호 |
| --- | --- | --- |
| `POST /api/v1/auth/login` | 200, access JWT와 두 cookie | exact Origin, 공용 IP 제한 |
| `GET /api/v1/auth/me` | 200, 현재 프로필 | Bearer JWT |
| `POST /api/v1/auth/session/refresh` | 200, access JWT와 회전된 두 cookie | exact Origin, double-submit CSRF, 공용 IP 제한 |
| `DELETE /api/v1/auth/session` | 본문 없는 204 | exact Origin, double-submit CSRF |

로그인·refresh 본문은 `access_token`, `token_type: Bearer`, `expires_in: 900`이다.
프로필의 `id`, `email`, `name`은 DB 현재 값이고 `effective_role`은 현재 JWT 역할이다.
역할 변경은 다음 로그인·refresh에서 새 JWT에 반영된다.
기존 JWT는 발급 후 명목 15분과 30초 clock skew까지 기존 역할을 사용할 수 있다.

refresh·CSRF cookie는 host-only, `Path=/`, `SameSite=Strict`이며 운영에서는 `Secure`다.
refresh cookie만 `HttpOnly`다. 명시적으로 허용한 localhost HTTP 개발 환경에서만
`Secure=false`를 사용할 수 있다. 로그인·refresh·logout의 모든 응답은 `no-store`다.

## 저장·동시성

- 사용자당 `ACTIVE` refresh family는 partial UNIQUE로 하나만 허용한다.
- 재로그인은 기존 family를 폐기하고 7일 절대 만료의 새 family를 만든다.
- 만료는 `now >= expires_at`으로 판정하며 refresh가 만료 시각을 연장하지 않는다.
- family의 `ACTIVE` 상태 자체가 만료되지 않았음을 뜻하지 않는다. 만료 시각을 함께 검사한다.
- DB에는 refresh token 원본을 보관하지 않고 32-byte SHA-256 digest만 저장한다.
- token digest UNIQUE 충돌은 savepoint를 rollback하고 최대 세 번 재생성한다.
  계속 충돌하면 새 family 생성과 기존 family 폐기까지 전체 rollback한다.
- 비밀번호 검증은 DB session을 닫은 뒤 bounded Argon2id adapter에서 수행한다.
  미등록 이메일도 프로세스 시작 시 같은 설정으로 만든 고정 fake hash를 검증한다.
- 쓰기 transaction은 사용자 행부터 잠근 뒤 password hash와 ACTIVE 상태를 재확인한다.
- 잠금 순서는 user → refresh family → refresh token이다. 필요한 행만 잠근다.
  token digest로 대상을 찾은 후 잠금을 잡고 소속·digest·상태·만료를 다시 검사한다.
- 판정 시각은 필요한 잠금을 얻은 뒤 읽은 PostgreSQL 시각이다.
- 사용된 token은 사용 시각부터 10초 이내이면 `409 refresh_conflict`, `Retry-After: 1`이다.
  cookie와 family를 유지하며 클라이언트는 다른 탭이 반영한 새 cookie로 한 번만 재시도한다.
  유예시간 이후에는 소속 family 폐기를 commit한 뒤 `401 INVALID_SESSION`을 반환한다.
- 서명 실패나 DB 쓰기 실패에서는 token 소비·교체·family 변경을 함께 rollback한다.
- login 실패와 refresh 충돌은 각각 `LOGIN_FAILED`, `REFRESH_CONFLICT` 운영 이벤트만 한 번 기록한다.
  세션 행위는 `user_security_events`에 중복 저장하지 않는다.
- 종료된 family와 token은 정본의 30일 보존 정책을 따른다. 실제 삭제 CLI는 #41의 책임이다.

## 구현 전 검수로 확정한 사항

### 후속 기능과의 교차 테스트

이번 #37은 login·refresh·logout 사이의 실제 DB 경합과 비밀번호 검증 도중 사용자 hash·상태가
변경되는 경우를 검증한다. 아직 없는 비밀번호 재설정·변경 및 비활성화 유스케이스와의 실제
교차 테스트는 해당 기능을 구현하는 #39·#43에서 완성한다.

### refresh 인증 실패

누락·위조·만료·폐기 등의 원인을 구분하지 않고 다음 응답을 반환한다.

- HTTP `401`, `code: INVALID_SESSION`
- `detail: 유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.`
- refresh·CSRF cookie를 모두 만료

Origin·CSRF 실패 403과 요청 제한 429, 회전 충돌 409에서는 cookie를 유지한다.

### 이미 사용한 token으로 로그아웃

이미 회전된 이전 token은 현재 family를 폐기하지 않는다. 요청한 브라우저의 두 cookie만
삭제하고 204를 반환한다. 현재 유효 token으로 로그아웃할 때는 소속 family도 폐기한다.
Origin·CSRF 검사 자체는 모든 로그아웃 요청에 적용한다.

### 명시적인 세션 활성화

`MDM_AUTH_SESSIONS_ENABLED` 기본값은 `false`다.
비활성 상태에도 인증 경로는 OpenAPI에 표시되며 login·refresh·logout은 요청 본문 해석이나
DB 작업 전에 503을 반환한다. `/auth/me`는 기존 Bearer 검증과 현재 프로필 조회로 동작한다.

활성화할 때는 아래 설정이 모두 필요하며 누락되면 시작을 실패시킨다.

- `MDM_AUTH_JWT_ACTIVE_KID`
- `MDM_AUTH_JWT_PRIVATE_KEY_PATH`
- `MDM_AUTH_JWT_JWKS_PATH`
- 비어 있지 않은 `MDM_AUTH_ALLOWED_ORIGINS`
- `MDM_AUTH_IP_HMAC_SECRET`: 독립적으로 생성한 256-bit secret의 canonical base64url

로컬 키는 `make jwt-key-local`로 생성한다. 비밀값은 `.env` 또는 배포 secret store로 주입하고
저장소에 넣지 않는다. HTTP 개발에서는 Origin allowlist와
`MDM_AUTH_ALLOW_INSECURE_LOCAL_COOKIES=true`를 함께 명시해야 한다.
Cloud Run 실제 client IP adapter와 배포 검증은 #34의 책임이다. 현재 기본 resolver는
client IP를 확정하지 못하므로 전달 header를 신뢰하지 않고 IP 제한을 생략하며 경고를 기록한다.
