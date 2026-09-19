# 이메일 인증 가입 계약

구현 티켓: [#38](https://github.com/Yumetri/MDM_example/issues/38).
인증 정본 [#22](https://github.com/Yumetri/MDM_example/issues/22),
입력·암호화 기반 #35, 세션 #37, 공통 보호 #44와 이메일 전달 #45를 따른다.

## API와 저장 책임

| 경로 | 성공 | 보호 |
| --- | --- | --- |
| `POST /api/v1/auth/registrations` | 202, 공통 안내 JSON | 공용 IP 제한 |
| `POST /api/v1/auth/registrations/complete` | 201, access JWT·refresh·CSRF cookie | exact Origin, 공용 IP 제한 |

두 경로는 login·refresh와 동일한 보호 객체와 IP quota를 공유하고 모든 응답에 `no-store`를
적용한다. 완료 시 기존 로그인·CSRF cookie는 필요하지 않다. 성공 응답은 로그인과 같은
`access_token`, `token_type: Bearer`, `expires_in: 900`이며 USER·ACTIVE 사용자를 만든다.

- `registration_challenges`는 정규화 이메일, ACTIVE·COMPLETED·EXPIRED 상태와 최초 발급·
  절대 만료·완료 시각을 저장한다. 최초 신청 후 24시간이며 재발송으로 연장하지 않는다.
- `registration_tokens`는 개별 token의 SHA-256 digest·발급·사용 시각만 저장한다.
  인증 전 표시 이름·비밀번호·hash를 저장하지 않는다.
- 정규화 이메일의 안정적인 hash를 사용한 PostgreSQL transaction advisory lock으로 신청을
  직렬화한다. 완료도 같은 이메일 잠금 후 challenge → token 순서로 잠근다.
  partial UNIQUE가 이메일당 ACTIVE challenge 하나를 추가로 보장한다.
- 판정에는 필요한 잠금을 잡은 뒤 읽은 DB 시각을 사용하며 `now >= expires_at`부터 거부한다.
  만료 뒤 재신청은 이전 challenge를 EXPIRED로 전환하고 새 challenge를 만든다.
- token digest UNIQUE 충돌은 savepoint만 rollback하고 최대 세 번 재생성한다. 계속 충돌하면
  challenge 생성·만료 전환 또는 사용자·세션 생성까지 전체 rollback한다.
- 완료한 token만 사용 시각을 기록하며, challenge의 COMPLETED 상태로 형제 token도 거부한다.
- 이메일은 commit·DB session 종료 후 await한다. 전달 실패·timeout은 202와 이미 commit된
  상태를 바꾸지 않으며 비밀값 없는 `EMAIL_DELIVERY_FAILED`를 한 번만 기록한다.
- 가입은 `user_security_events`의 영구 감사 6종에 해당하지 않으므로 해당 행을 추가하지 않는다.
- 가입 challenge·digest는 완료 또는 만료 시점부터 7일 보존한다. 실제 삭제 CLI는 #41,
  인증 링크를 처리하는 frontend는 #48, 실제 SMTP·배포 환경 검증은 #34의 책임이다.

## 구현 전 검수로 확정한 사항

### 비밀번호 해시 계산과 transaction

#22 가입 설명의 transaction 내부 해시 계산 문구와 공통 실행 규칙이 충돌하므로,
검수에서 #22 공통 규칙과 #35·#38의 DB 연결 없는 해시 계산을 적용하기로 확정했다.

- 비밀번호 hash는 bounded PasswordHasher에서 DB 연결·transaction 없이 먼저 계산한다.
- 이후 가입 완료 transaction에서 token의 digest·소속·상태·만료를 다시 검사한다.
- 사용자, 첫 refresh family·token 생성과 challenge 완료는 같은 transaction에서 처리한다.
- hash 계산이 끝났다는 이유로 그 사이 만료되거나 사용된 token을 허용하지 않는다.

### 가입 완료 token 오류

- 잘못된 형식, 미존재, 만료 및 이미 사용된 token은 동일한
  `400 INVALID_REGISTRATION_TOKEN`으로 거부한다.
- 안내: `유효하지 않거나 만료된 가입 인증 링크입니다. 가입을 다시 신청해 주세요.`
- 기존 로그인 cookie를 유지한다.
- JSON 필드 누락·타입 오류 및 이름·비밀번호 검증 실패는 `422 VALIDATION_ERROR`이며
  token을 소비하지 않는다.

### 기능 활성화와 구성 실패

- `MDM_AUTH_REGISTRATIONS_ENABLED` 기본값은 `false`다.
- 활성화할 때는 `MDM_AUTH_SESSIONS_ENABLED=true`와 #45 이메일 발송 설정이 모두 필요하다.
  누락되면 앱 시작을 실패시킨다. 로그인만 활성화할 때는 이메일 설정을 요구하지 않는다.
- 비활성 상태의 가입 신청·완료 API는 DB 접근 전에 `503 SERVICE_UNAVAILABLE`을 반환한다.

### 가입 완료 시 허용 도메인 재검사

- 가입 신청뿐 아니라 완료할 때도 현재 설정의 허용 도메인을 검사한다.
- 신청 이후 도메인이 제거됐으면 `422 EMAIL_DOMAIN_NOT_ALLOWED`로 거부한다.
- 이 경우 사용자·세션을 만들거나 token을 소비하지 않는다.

### 허용 도메인의 정확한 일치

- `MDM_AUTH_REGISTRATION_ALLOWED_DOMAINS`는 정확한 도메인의 JSON 배열이다.
- 이메일과 같은 IDNA ASCII·lowercase 정규화를 적용하며 중복은 제거한다.
- `example.com`과 `sub.example.com`은 각각 등록해야 한다. wildcard는 설정 오류로 거부한다.
- 기본 빈 배열은 공개 가입을 허용하지 않는다.

### 신청 시 도메인 정책의 우선순위

- 계정 조회 전에 허용 도메인을 검사한다. 허용되지 않은 도메인은 기존 계정도
  `422 EMAIL_DOMAIN_NOT_ALLOWED`다.
- 허용 도메인의 기존 계정은 신규 신청과 같은 `202`를 반환하며 challenge·token·메일을
  생성하지 않는다. 계정 존재 여부로 도메인 거부 응답이 달라지지 않는다.

### 가입 신청의 공통 응답 본문

신규 신청·기존 계정·이메일 발송 실패는 모두 `202`와 아래 JSON을 반환한다.

```json
{"message":"가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요."}
```

### 메일 발급 뒤 다른 경로로 생성된 계정

- 가입 완료 전에 bootstrap 등으로 같은 이메일 계정이 생성되면 기존 계정의 비밀번호·역할·
  세션을 변경하지 않는다.
- 사용자 이메일 UNIQUE 충돌은 가입 transaction 전체를 rollback하고
  `400 INVALID_REGISTRATION_TOKEN`으로 반환한다. 기존 로그인 cookie와 가입 token은 유지한다.
