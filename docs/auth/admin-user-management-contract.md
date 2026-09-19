# 사용자 역할·상태 관리 계약

구현 티켓: [#43](https://github.com/Yumetri/MDM_example/issues/43).
인증 정본 #22, 권한 검사 #27, 사용자 조회 #46과 비밀번호 수명주기 #39를 따른다.

## 구현 전 검수로 확정한 사항

### SUPER_ADMIN 간 역할 변경

2026-09-19 검수에서 #43의 마지막 SUPER_ADMIN 강등 허용 조건과 기존 #27의
하위 역할 대상 제한 사이의 충돌을 다음과 같이 해소하기로 승인했다.

- SUPER_ADMIN은 다른 USER·ADMIN·SUPER_ADMIN의 역할을 USER·ADMIN·SUPER_ADMIN으로
  변경할 수 있다. 기존 동급 SUPER_ADMIN 대상 거부 규칙을 변경한다.
- 자신의 역할 변경은 계속 금지한다.
- ADMIN은 다른 USER의 역할만 USER 또는 ADMIN으로 변경할 수 있다.
- ACTIVE SUPER_ADMIN 최소 인원 제한은 두지 않는다. 권한은 검증된 access JWT 역할로
  판정하므로, 서로 다른 SUPER_ADMIN이 상대를 강등하여 0명이 되는 결과도 허용한다.

### 동일 값 요청과 권한 검사 순서

2026-09-19 검수에서 대상의 현재 역할에 대한 권한 검사를 동일 값 판정보다 먼저
수행하기로 승인했다.

- 권한이 있는 동일 값 요청만 본문 없는 204를 반환하고 감사·변경 시각을 유지한다.
- ADMIN이 USER를 ADMIN으로 승격한 뒤 같은 요청을 반복하면 대상의 현재 역할이
  동급 ADMIN이므로 403 AUTHORIZATION_DENIED다.
- 자기 역할 변경도 동일 값 여부와 관계없이 403이다.

### 대상 사용자 잠금

2026-09-19 검수에서 #43에 명시된 `FOR UPDATE` 대신 `FOR NO KEY UPDATE`를
대상 사용자 행에 사용하기로 승인했다. 역할·상태 변경은 사용자 식별 키를 변경하지 않는다.
같은 사용자에 대한 변경은 직렬화하면서, 감사의 실행자 외래키 확인에 필요한
`KEY SHARE` 잠금은 허용한다. 따라서 두 SUPER_ADMIN이 서로를 변경해도 각자의
대상 잠금과 상대방에 대한 감사 외래키 확인이 교착하지 않는다.

## API와 원자성

| 경로 | 요청 | 성공 | 권한 |
| --- | --- | --- | --- |
| `PUT /api/v1/admin/users/{user_id}/role` | `role: USER\|ADMIN\|SUPER_ADMIN` | 본문 없는 204 | ADMIN·SUPER_ADMIN, 위 대상 규칙 적용 |
| `PUT /api/v1/admin/users/{user_id}/status` | `status: ACTIVE\|DISABLED` | 본문 없는 204 | SUPER_ADMIN, 다른 사용자만 |

- 호출자 권한과 감사 initiator는 검증된 Bearer JWT를 사용한다. body의 추가 필드는
  거부하며 query·header의 actor·role·SYSTEM 표시는 권한에 사용하지 않는다.
- 대상의 현재 역할·상태는 행 잠금 뒤 읽은 DB 값으로 판단한다.
- 상태 변경은 자신을 대상으로 할 수 없다. 같은 상태 요청도 이 제한을 적용한다.
- 미존재 대상은 `404 USER_NOT_FOUND`, 권한 위반은 `403 AUTHORIZATION_DENIED`,
  잘못된 UUID·enum·body는 `422 VALIDATION_ERROR`, 저장 실패는 내부 내용을 제외한
  `503 SERVICE_UNAVAILABLE`로 응답한다.
- 역할 변경은 role·updated_at과 `USER_ROLE_CHANGED`의 previous/new role을 함께 저장한다.
  역할 변경 자체는 refresh family나 reset challenge를 폐기하지 않는다.
- 비활성화는 status·updated_at, 모든 ACTIVE refresh family 폐기, 모든 ACTIVE
  reset challenge REVOKED 전환과 `USER_DISABLED` 감사를 함께 저장한다.
- 잠금 순서는 user → refresh family → refresh token → reset challenge → reset token이다.
  같은 종류의 여러 행은 UUID 순서로 잠근다. 필요한 잠금 후 읽은 DB 시각을 변경·감사에 사용한다.
- 활성화는 상태와 `USER_ENABLED` 감사만 저장한다. 폐기 인증수단이나 비활성화 중의
  재설정 신청·메일을 복원하지 않는다. 새 로그인·재설정 신청이 필요하다.
- 어떤 쓰기나 감사 저장이 실패하면 전체 transaction을 rollback한다.
- 역할·상태 변경 전 access JWT는 기존 역할로 `now < exp+30초` 동안 유효하다.
  사용자가 비활성화됐어도 이미 발급된 JWT를 별도 DB 조회로 차단하지 않는다.
  다음 login·refresh는 현재 DB 역할을 반영하고 DISABLED 사용자를 즉시 거부한다.
- 기존 관리자 조회와 같은 Bearer 인증 구성을 사용하며 별도 활성화 설정이나 이메일 설정을
  요구하지 않는다. Cookie 기반 인증을 사용하지 않는다.
- 활성 SUPER_ADMIN 집합 count, 최소 인원 제약, 전역 advisory lock은 두지 않는다.
  역할 목록, 이메일 변경, 관리자 UI와 관리자 부재 복구는 이 티켓 범위 밖이다.

## 검증 책임

#37·#39에서 #43으로 넘긴 비활성화 교차 테스트를 이 티켓에서 완성한다.
login·refresh·logout·reset 완료·로그인 상태 password change·reset 신청과 실제
비활성화 유스케이스를 교차 실행하여 부분 커밋·감사 누락·교착을 검증한다.
두 SUPER_ADMIN의 상호 강등·비활성화와 같은 대상의 동시 변경도 실제 DB에서 검증한다.
