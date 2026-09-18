# 관리자 사용자 조회 계약

구현 티켓: [#46](https://github.com/Yumetri/MDM_example/issues/46).
인증·권한 정본 #22·#27과 사용자 모델 #35를 따른다.

## 조회 범위

- `GET /api/v1/admin/users`: SUPER_ADMIN은 모든 사용자, ADMIN은 현재 DB 역할이
  USER인 사용자만 조회한다. USER 호출은 `403 AUTHORIZATION_DENIED`다.
- `GET /api/v1/admin/users/{user_id}`: 같은 가시 범위를 적용한다.
  미존재 사용자와 비가시 사용자는 같은 404 응답으로 처리한다.
- 호출자 권한은 검증된 Bearer JWT에서 얻고, 조회 대상의 역할·상태는 DB 현재 값을 반환한다.
- 반환 필드는 `id`, `email`, `name`, `role`, `status`, `created_at`, `updated_at`이다.
- SQL 조회에 가시 범위를 적용한 뒤 필터와 페이지 경계를 적용한다.
- 조회는 보안 감사 이벤트를 생성하지 않는다. 역할·상태 변경은 #43이 담당한다.

## 필터와 페이지

- `email`은 기존 `normalize_email`을 적용한 exact match다.
- `role`: USER, ADMIN, SUPER_ADMIN. `status`: ACTIVE, DISABLED.
- 생략한 필터에는 제한이 없다. 제공한 필터는 모두 만족해야 한다.
- ADMIN이 ADMIN·SUPER_ADMIN 역할 필터를 지정하면 빈 목록이다.
- `(created_at DESC, id DESC)` 순서이며 `limit`은 기본 50, 범위 1~100이다.
- 응답은 `items`, `next_cursor`이며 마지막 페이지의 cursor는 null이다. 전체 건수는 없다.
- 같은 필터·호출자 조회 범위와 변경 없는 데이터에서는 페이지 간 누락·중복이 없다.

## 구현 전 검수로 확정한 사항

### 검색 조건 변경 시 cursor 처리

- email·role·status 필터가 바뀐 요청에 이전 cursor를 보내면 422로 거부한다.
- 검색 조건을 변경할 때는 cursor를 생략하고 첫 페이지부터 다시 조회한다.
- email 필터는 정규화한 값을 비교하므로 같은 이메일을 나타내는 표기는 같은 조건이다.
- `limit`은 검색 조건에 포함하지 않아 페이지 이동 중 변경할 수 있다.

### 호출자 조회 범위 변경 시 cursor 처리

- cursor는 검색 조건과 함께 서버가 판정한 호출자 조회 범위에 연결한다.
- ADMIN의 일반 사용자만 조회와 SUPER_ADMIN의 전체 사용자 조회 사이에 범위가 바뀌면
  이전 cursor는 `422 VALIDATION_ERROR`로 거부한다. cursor를 생략하고 첫 페이지부터 조회한다.
- cursor를 JWT 문자열이나 호출자 ID에 연결하지 않는다. 토큰 갱신이나 호출자 변경 후에도
  조회 범위와 검색 조건이 같으면 이어서 조회할 수 있다.
- cursor 유무와 관계없이 매 요청의 인증·인가와 SQL 가시 범위 제한을 적용한다.
- 조회 범위를 포함하지 않았던 이전 구현의 cursor도 422로 거부하고 재조회를 안내한다.

### 목록 조회 인덱스

- 전체 조회용 `(created_at DESC, id DESC)`와 역할별 조회용
  `(role, created_at DESC, id DESC)` B-tree 인덱스를 사용한다.
- 역할별 인덱스는 ADMIN의 USER 조회와 SUPER_ADMIN의 역할 필터 조회를 함께 지원한다.
- 두 인덱스는 저장 공간과 사용자 생성·변경 시 유지 비용을 추가한다.
- 상태 등 나머지 필터 조합은 실행 계획을 측정하여 추가 인덱스 필요성을 판단한다.
- `MDM_DATABASE_URL=<mdm_test URL> uv run python scripts/explain_admin_users.py`로
  실제 저장소 쿼리를 10만 건의 임시 데이터에서 측정한다. 전체·역할별 조회의 첫 페이지와
  뒤쪽 페이지를 인덱스 전후 및 custom/generic 실행 계획으로 비교한다.
  기존 데이터는 바꾸지 않으며 임시 테이블 작업은 rollback한다.
  결과는 `.artifacts/admin-user-query-plans.json`에 저장한다.

### 상세 조회 404 응답

미존재 사용자와 비가시 사용자는 아래 공통 응답을 사용한다.

- status: `404`, code: `USER_NOT_FOUND`
- type: `/problems/user-not-found`
- title: `사용자를 찾을 수 없음`
- detail: `조회할 수 있는 사용자가 없습니다.`

### 미지원 목록 query 파라미터

- 목록 조회는 `email`, `role`, `status`, `cursor`, `limit`만 허용한다.
- `name`, `actor_role` 등 미지원 파라미터는 무시하지 않고 `422 VALIDATION_ERROR`로 거부한다.
- 호출자 권한은 query·header의 역할 주장을 신뢰하지 않고 검증된 JWT로만 판정한다.
