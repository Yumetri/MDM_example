# 비밀번호 재설정·변경 계약

구현 티켓: [#39](https://github.com/Yumetri/MDM_example/issues/39).
인증 정본 [#22](https://github.com/Yumetri/MDM_example/issues/22),
세션 #37, 공통 요청 보호 #44와 이메일 전달 #45를 따른다.

## API와 저장 책임

| 경로 | 성공 | 인증·보호 |
| --- | --- | --- |
| `POST /api/v1/auth/password-resets` | 202, 공통 안내 JSON | 공용 IP 제한 |
| `POST /api/v1/auth/password-resets/complete` | 본문 없는 204, 두 cookie 만료 | 이메일 token, 공용 IP 제한 |
| `PUT /api/v1/auth/me/password` | 본문 없는 204, 새 refresh·CSRF cookie | exact Origin, double-submit CSRF, 공용 IP 제한, Bearer JWT, 현재 refresh·비밀번호 |

- 모든 인증 흐름은 같은 공용 IP quota를 사용하고 응답은 `Cache-Control: no-store`다.
- 재설정 신청은 ACTIVE 사용자에게만 challenge와 이메일을 만든다. 사용자 행 잠금과
  `(user_id) WHERE status = 'ACTIVE'` partial UNIQUE로 활성 challenge 하나를 보장한다.
- 재신청은 같은 challenge에 token만 추가하며 최초 30분 절대 만료를 연장하지 않는다.
  만료된 ACTIVE challenge는 새 신청 때 EXPIRED로 바꾸고 새 challenge를 만든다.
- DB에는 SHA-256 digest와 발급·사용 시각만 저장한다. token digest UNIQUE 충돌은
  savepoint rollback 뒤 최대 세 번 재생성하고, 계속 충돌하면 전체 transaction을 rollback한다.
- 이메일은 commit과 DB session 종료 후 await한다. 발송 실패·timeout은 202와 저장 결과를
  바꾸지 않으며 `EMAIL_DELIVERY_FAILED`를 한 번만 기록한다.
- 재설정 완료는 비밀번호 변경·모든 ACTIVE refresh family 폐기·challenge COMPLETED 전환·
  `PASSWORD_RESET` 감사를 한 transaction에 저장한다. 감사 initiator는 RESET_TOKEN이다.
  완료한 token만 사용 시각을 기록하고, challenge의 완료 상태로 형제 token도 거부한다.
- 로그인 상태 변경은 비밀번호 변경·기존 family 폐기·현재 브라우저의 새 7일 family 생성·
  ACTIVE reset challenge REVOKED 전환·`PASSWORD_CHANGED` 감사를 함께 저장한다.
  감사 initiator의 사용자와 역할은 검증된 JWT Principal에서 가져온다.
- 잠금 순서는 user → refresh family → refresh token → reset challenge → reset token이다.
  같은 종류의 여러 행은 UUID 순서로 잠그고, 필요한 잠금 뒤 DB 시각으로 만료를 판정한다.
- 비밀번호 hash·verify는 DB connection 없이 bounded adapter에서 수행한다. 쓰기 직전
  사용자 상태·기존 hash와 token의 digest·소속·상태·만료를 다시 검증한다.
- 로그인 상태 변경은 hash·verify 전에 JWT와 refresh 소유자의 결속 및 현재 세션을 검사한다.
  다른 사용자의 refresh를 제출하면 DB 변경 없이 `SESSION_PRINCIPAL_MISMATCH`만 한 번 기록한다.
- 성공·만료·폐기된 reset challenge와 token은 종료 시점부터 7일 보존한다. 삭제 CLI는 #41,
  fragment token을 읽고 즉시 주소에서 제거하는 화면은 #48, 실제 SMTP 배포 검증은 #34의 책임이다.
- 재설정 후 refresh 세션은 즉시 무효지만 기존 access JWT는 원래 만료와 30초 clock skew까지
  유효할 수 있다. 이번 변경은 access JWT denylist를 추가하지 않는다.

## 구현 전 검수로 확정한 사항

### 후속 비활성화 기능과의 교차 테스트

2026-09-19 검수에서 #39와 #43의 동시성 검증 책임을 다음과 같이 나누기로 승인했다.

- #39는 비밀번호 재설정·로그인 상태 비밀번호 변경과 기존 login·refresh·logout 사이의
  실제 DB 경합, 잠금 순서와 원자성을 검증한다.
- 사용자 비활성화는 #39를 선행 작업으로 요구하는
  [#43](https://github.com/Yumetri/MDM_example/issues/43)의 구현 범위다.
  비활성화와 위 인증 흐름 사이의 실제 교차 동시성 검증은 #43에서 완성한다.
- #39에서 비활성화 기능을 앞당겨 구현하거나, 직접 DB 상태를 바꾼 테스트를 실제
  비활성화 유스케이스와의 교차 검증으로 간주하지 않는다.

### 기능 활성화와 구성 실패

2026-09-19 검수에서 다음 활성화 정책을 승인했다.

- 이메일 재설정 신청·완료는 `MDM_AUTH_PASSWORD_RESETS_ENABLED`를 따르며 기본값은
  `false`다. 활성화에는 `MDM_AUTH_SESSIONS_ENABLED=true`와 이메일 발송 설정이 모두
  필요하다. 누락되면 앱 시작을 실패시킨다. 공개 가입 활성화는 요구하지 않는다.
- 로그인 상태 비밀번호 변경은 `MDM_AUTH_SESSIONS_ENABLED`를 따른다. 이메일 재설정이나
  공개 가입이 비활성이어도 사용할 수 있으며 이메일 발송 설정을 요구하지 않는다.
- 비활성 API는 기존 인증 경로처럼 OpenAPI에 표시하되, 본문 해석·DB 접근 전에
  `503 SERVICE_UNAVAILABLE`을 반환한다.

### 비밀번호 변경에 이미 회전된 refresh token 제출

2026-09-19 검수에서 로그인 상태 비밀번호 변경은 현재 유효한 미사용 refresh token만
허용하기로 확정했다.

- 이미 회전된 token은 사용 후 경과 시간과 관계없이 `401 INVALID_SESSION`으로 거부하고
  응답의 refresh·CSRF cookie를 만료시킨다. 비밀번호 변경에는 10초 회전 충돌 유예를
  적용하지 않는다.
- 거부 시 DB의 refresh family·비밀번호·재설정 challenge·감사는 변경하지 않는다.
- 다른 탭의 갱신과 비밀번호 변경이 겹치면 다시 로그인해야 할 수 있다.
  기존 refresh API의 `409 refresh_conflict`와 10초 이후 family 폐기 정책은 유지한다.

### 재설정 신청의 공통 응답

2026-09-19 검수에서 정상·미등록·비활성 계정 및 이메일 전달 실패의 공통 `202` 본문을
다음과 같이 확정했다.

```json
{"message":"비밀번호 재설정이 가능한 계정이면 안내 메일을 보냈습니다. 메일함을 확인해 주세요."}
```
