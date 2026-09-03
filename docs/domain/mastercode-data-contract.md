# MasterCode 데이터 계약과 코드 합성 규칙

## 1. 문서 상태와 범위

이 문서는 GitHub 이슈 #4에서 합의한 MasterCode의 구현 정본이다. 후속 구현 티켓은 이 계약을
참조하며 서로 다른 규칙을 다시 정의하지 않는다.

이 문서가 정의하는 범위는 다음과 같다.

- MasterCode 필드와 고정 Dimension 참조 구조
- 결정론적인 합성 코드 생성 규칙
- 유일성, 충돌과 참조 무결성 정책
- Dimension 참조 수정과 MasterCode 삭제·복원 규칙
- MasterCode 생성과 Dimension code 변경이 공유하는 잠금 순서와 트랜잭션 경계
- 정상, 경계, 충돌과 동시성 수용 테스트 벡터

SQLAlchemy 모델, Alembic 마이그레이션, CRUD API와 SCD Type 2 조회 모델의 구현은 이 티켓의
범위가 아니다.

## 2. Dimension 계약에서 상속하는 제약

MasterCode는 `docs/domain/dimension-data-contract.md`에서 확정한 다음 제약을 변경하지 않는다.

- Dimension은 Year, Memory, Company, Model, Brand, Country, Network, Category의 고정된 8개
  타입별 테이블로 분리한다.
- 각 Dimension code는 N으로만 이루어진 예약 패턴을 제외한 ASCII `A-Z0-9` 1~32자이며 해당
  타입 테이블 안에서 유일하다.
- 삭제된 Dimension은 새 MasterCode에서 참조할 수 없다.
- Dimension code 변경과 영향받는 모든 MasterCode 재합성은 하나의 동기 DB 트랜잭션이다.
- Dimension FK는 `ON DELETE RESTRICT`, `ON UPDATE RESTRICT`를 사용한다.
- concurrent task 사이에서 SQLAlchemy 세션을 공유하지 않는다.

Dimension 삭제를 차단하는 참조는 활성 MasterCode의 참조만을 뜻한다. 논리 삭제된 MasterCode가
보존한 참조까지 삭제를 차단한다는 이전 이슈 초안의 표현은 이 최종 계약으로 대체한다.

## 3. Dimension 참여 규칙

MasterCode 하나는 다음 8개 타입의 자리를 항상 하나씩 가진다.

1. Company
2. Brand
3. Model
4. Category
5. Year
6. Memory
7. Network
8. Country

각 자리는 해당 타입의 활성 Dimension 한 행을 참조하거나 명시적인 `NULL`로 "해당 없음"을
나타낸다. `NULL`은 아직 값을 모르거나 입력하지 않았다는 뜻으로 사용하지 않는다. 저장된
MasterCode가 실제 Dimension을 참조하면 해당 타입의 UUID를 저장하고 "해당 없음"이면 `NULL`을
저장한다. 실제 Dimension 참조의 최소 개수는 0, 최대 개수는 8이다.

사용자의 MasterCode 생성 요청은 8개 자리를 모두 제공한다. 각 자리는 기존 Dimension 참조,
승인과 함께 수행할 신규 Dimension 생성 제안 또는 "해당 없음" 중 하나이다. 필드 누락은
불완전한 입력이므로 거부한다.

요청의 각 자리는 `mode` 필드로 구분하는 다음 tagged union을 사용한다.

- `REFERENCE`: 해당 타입의 기존 Dimension `id`를 함께 제공한다.
- `CREATE`: 신규 Dimension의 `code`와 타입별 `value`를 함께 제공한다.
- `NOT_APPLICABLE`: 추가 필드 없이 "해당 없음"을 나타낸다.

UUID, 객체와 JSON `null`을 직접 혼합하지 않는다. mode별 허용 필드 외의 추가 입력은 거부하고,
저장 시 `REFERENCE`와 승인된 `CREATE`는 해당 Dimension UUID로, `NOT_APPLICABLE`은 SQL
`NULL`로 변환한다.

생성 후 잘못 지정한 Dimension 참조는 조건부 수정으로 교체하거나 `NOT_APPLICABLE`로 해제할 수
있다. PATCH에서 제공하지 않은 참조는 현재 값을 유지한다. 합성 code는 클라이언트가 따로
지정하지 않고 변경 후 각 참조의 현재 code와 `NULL` 자리의 기본 코드를 사용해 다시 합성한다.

사용자의 `REFERENCE_UPDATE` 요청도 변경할 각 자리에 `REFERENCE`, `CREATE`,
`NOT_APPLICABLE` tagged union을 사용한다. 제공하지 않은 자리는 현재 참조를 유지하고,
`CREATE`는 관리자 승인과 같은 트랜잭션에서 신규 Dimension을 생성해 그 UUID로 교체한다.

`NULL` 자리의 합성 기본 코드는 모든 타입에서 `NNN`이다. N으로만 이루어진 code는 길이와
관계없이 실제 Dimension code로 사용할 수 없는 예약 영역이며, 합성 코드에서 "해당 없음"을
표현하기 위해서만 사용한다.

각 타입의 자리는 하나뿐이고 각 FK는 자기 타입 테이블만 참조하므로 동일한 Dimension 행을 한
MasterCode 안에서 두 번 참조하는 상태는 표현할 수 없다. 서로 다른 타입 테이블에 우연히 같은
UUID 바이트가 있더라도 서로 다른 Dimension 행이므로 중복 참조가 아니다.

## 4. 합성 코드 규칙

합성 코드는 다음 순서로 각 Dimension의 정규화된 code를 연결한다.

```text
COMPANY-BRAND-MODEL-CATEGORY-YEAR-MEMORY-NETWORK-COUNTRY
```

구분자는 하이픈(`-`) 하나이다. Dimension code에는 하이픈이 허용되지 않으므로 escaping 규칙은
필요하지 않으며, 합성 결과를 구분자로 나누면 항상 정확히 8개 부분이 나와야 한다. 각 code의
최대 길이는 32자이고 구분자가 7개이므로 합성 코드의 최대 길이는 263자이다.

동일한 8개 code 입력은 항상 바이트 단위로 동일한 합성 코드를 만든다. 합성 과정에서 code를
다시 정규화하거나 추정하지 않고, 이미 검증된 `DimensionCode` 값만 사용한다.

Dimension 참조가 `NULL`이면 해당 위치에는 `NNN`을 사용한다. 예를 들어 country 참조만
`NULL`이라면 마지막 부분이 `NNN`인 다음 형태가 된다.

```text
COMPANY-BRAND-MODEL-CATEGORY-YEAR-MEMORY-NETWORK-NNN
```

## 5. 도메인 책임

합성 알고리즘은 도메인의 `MasterCode` 객체가 소유한다. `MasterCode` 생성은 공개 생성 팩터리를
통해서만 수행하며, 이 팩터리가 객체 자신의 합성 메서드를 호출해 합성 코드를 만든다. Dimension
code 변경으로 기존 MasterCode를 재합성할 때도 반드시 같은 메서드를 사용한다.

`MasterCode`는 합성 순서를 나타내는 불변 순서 목록을 한 곳에만 선언한다. 합성 메서드는 8개
타입별 code를 이 목록의 순서대로 읽어 연결하며, 생성과 재합성을 위한 별도의 순서 목록을 두지
않는다. 순서 변경 시 이 선언과 그 결과를 고정하는 테스트 벡터만 함께 변경할 수 있어야 한다.

`MasterCode`는 `NOT_APPLICABLE_CODE = "NNN"`을 도메인 상수로 소유한다. 이 값은 실제
Dimension을 나타내는 `DimensionCode`가 아니라 합성 코드에서 `NULL` 참조를 표현하는 전용
부분이다. 환경 변수나 요청 값으로 덮어쓸 수 없다.

합성 메서드는 8개 타입의 검증된 code를 명시적으로 입력받아 합성 코드만 반환하는 순수 로직이다.
데이터베이스 조회, 행 잠금과 트랜잭션 제어는 수행하지 않는다. 필요한 Dimension을 조회하고
잠그는 일은 애플리케이션 유스케이스와 repository의 책임이며, SQLAlchemy 모델이나 DB 트리거에
동일한 합성 알고리즘을 중복 구현하지 않는다.

합성 순서와 구분자는 런타임 설정이나 요청 입력으로 바꿀 수 없다. 규칙 변경은 이 계약과
`MasterCode`의 합성 메서드 및 테스트 벡터를 함께 변경하는 명시적인 도메인 변경으로 수행한다.
`NNN`을 다른 값으로 변경할 때는 `NULL` 참조가 있는 기존 MasterCode를 모두 재합성하는 데이터
마이그레이션도 함께 수행한다.

## 6. 필드와 변경 정책

| 필드 | PostgreSQL 타입 | nullable | 기본값 | 변경 정책 |
| --- | --- | --- | --- | --- |
| `id` | `UUID` | 아니요 | `uuidv7()` | 변경 금지 |
| `company_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `brand_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `model_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `category_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `year_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `memory_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `network_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `country_id` | `UUID` | 예 | 없음 | 조건부 수정·해제 가능 |
| `code` | `VARCHAR(263)` | 아니요 | 없음 | Dimension 참조 또는 Dimension code 변경 시 자동 재합성 |
| `version` | `INTEGER` | 아니요 | `1` | 실제 상태 변경마다 정확히 1 증가 |
| `created_at` | `TIMESTAMPTZ` | 아니요 | `statement_timestamp()` | 변경 금지 |
| `updated_at` | `TIMESTAMPTZ` | 아니요 | `statement_timestamp()` | 실제 상태 변경 시 갱신 |
| `deleted_at` | `TIMESTAMPTZ` | 예 | `NULL` | 삭제·복원 명령만 변경 가능 |

클라이언트는 `code`와 서버 생성 필드를 입력하거나 직접 수정할 수 없다. MasterCode 참조 수정은
8개 참조 중 하나 이상을 교체할 수 있으며, 제공하지 않은 참조는 유지한다. 여러 참조를 한 요청에
제공하면 참조 교체와 code 재합성을 하나의 트랜잭션에서 원자적으로 수행한다.

## 7. 조회 표현과 ETag

MasterCode 단건 조회는 id, 합성 code, version, 생성·수정 시각과 8개 Dimension의 현재 상태를
반환한다. 실제 참조가 있으면 해당 Dimension의 id, code와 타입별 value를 중첩하고
`NOT_APPLICABLE` 자리는 JSON `null`로 반환한다. 과거 Dimension 값은 섞지 않는다.

일반 단건 조회와 tombstone 조회는 MasterCode와 8개 nullable Dimension을 `LEFT JOIN`한 하나의
SQL 문으로 읽는다. PostgreSQL `READ COMMITTED`의 동일 statement snapshot에서 얻은 행만으로
응답 본문과 ETag를 함께 계산한다. 여러 SELECT를 사용하는 lazy loading이나 `selectinload`로
MasterCode 상태와 Dimension 상태를 조합하지 않는다.

중첩 Dimension의 개별 ETag와 version은 MasterCode 응답에 넣지 않는다. 해당 Dimension을
독립적으로 조건부 변경해야 하는 클라이언트는 Dimension 단건 API에서 그 자원의 ETag를 새로
얻어야 한다.

MasterCode 단건 응답은 전체 표현을 검증하는 불투명한 강한 ETag 하나를 헤더에 제공한다. ETag는
DB 컬럼으로 저장하지 않고 다음 JSON 배열을 SHA-256으로 해시해 계산한다.

```text
[algorithm_version,master_code_version,[[type,id,version],...8개]]
```

바깥 배열과 각 Dimension 배열의 원소 순서는 고정한다. Dimension 배열은 Company, Brand, Model,
Category, Year, Memory, Network, Country 순서이고 type 문자열은 각각 `COMPANY`, `BRAND`,
`MODEL`, `CATEGORY`, `YEAR`, `MEMORY`, `NETWORK`, `COUNTRY`이다. UUID는 표준 소문자 하이픈
문자열이고 `NOT_APPLICABLE` 자리는 id와 version을 모두 JSON `null`로 쓴다. 정수는 선행 0 없는
JSON 10진수로 쓰며 구분자 앞뒤를 포함해 공백과 줄바꿈을 넣지 않는다. 이 한 줄의 UTF-8 바이트를
SHA-256 입력으로 사용한다.

ETag 알고리즘 버전의 초기값은 `1`이다. 응답 스키마나 직렬화 규칙 변경으로 같은 DB version
조합에서도 표현이 달라질 수 있으면 알고리즘 버전을 증가시킨다. 헤더는 알고리즘 버전과 64자리
소문자 16진수 해시를 포함한 quoted opaque-tag를 사용한다.

```text
ETag: "mc-1-<sha256-hex>"
```

고정 벡터로 알고리즘 버전 1, MasterCode version 1, 모든 자리가 `NOT_APPLICABLE`이면 해시 입력과
ETag는 다음과 같다.

```text
[1,1,[["COMPANY",null,null],["BRAND",null,null],["MODEL",null,null],["CATEGORY",null,null],["YEAR",null,null],["MEMORY",null,null],["NETWORK",null,null],["COUNTRY",null,null]]]
ETag: "mc-1-659320934c6987cb1e86f80832fe7248649203e6a3a1b12bb3517d97ff40642e"
```

MasterCode version은 MasterCode 자체의 상태 변경 횟수이고 ETag와 같은 값이 아니다. 참조한
Dimension의 value만 바뀌면 MasterCode version은 유지되지만 Dimension version과 전체 응답
ETag는 바뀐다. Dimension code 변경 또는 MasterCode 참조 변경처럼 MasterCode 자체가 바뀌면
MasterCode version과 ETag가 모두 바뀐다.

## 8. 조건부 참조 수정

MasterCode 참조 수정은 Dimension 수정과 같은 강한 ETag 계약을 사용한다.

- 요청은 직전 단건 응답에서 받은 정확히 하나의 강한 `If-Match` opaque-tag를 제공한다.
- `If-Match: *`, 약한 ETag와 여러 ETag 목록은 거부한다.
- 헤더 누락은 `428 PRECONDITION_REQUIRED`이다.
- 문법 오류는 `400 INVALID_IF_MATCH`이다.
- 현재 전체 표현의 ETag와 불일치는 `412 PRECONDITION_FAILED`이다.
- 현재 참조 Dimension과 MasterCode 행을 잠근 뒤 ETag를 다시 계산해 검사한다.

제공한 모든 참조가 현재 참조와 같으면 no-op이다. 이때 code 재합성, UPDATE, version 증가와
updated_at 변경을 수행하지 않는다. 하나 이상의 참조가 실제로 바뀌면 변경된 전체 참조로 code를
한 번 다시 합성하고 version을 정확히 1 증가시키며 updated_at을 갱신한다.

## 9. 유일성과 충돌

MasterCode는 다음 두 유일성 제약을 함께 사용한다.

- `UNIQUE(code)`
- `UNIQUE NULLS NOT DISTINCT (company_id, brand_id, model_id, category_id, year_id, memory_id,
  network_id, country_id)`

`NULLS NOT DISTINCT`는 같은 위치의 `NULL`을 동일한 값으로 취급한다. 따라서 실제 참조와
"해당 없음" 위치가 모두 같은 MasterCode를 중복 생성할 수 없다. 합성 code가 우연히 같거나 같은
참조 조합을 다시 사용한 경우 모두 충돌이며 기존 행의 성공 응답으로 바꾸지 않는다.

논리 삭제된 MasterCode도 두 유일성 제약의 범위에 계속 포함한다. 삭제된 행과 같은 참조 조합이나
code로 새 행을 생성할 수 없으며 기존 행을 복원해야 한다. 생성, 참조 수정 또는 재합성 중 하나의
유일성 제약이라도 위반하면 해당 트랜잭션 전체를 롤백한다.

## 10. 논리 삭제와 복원

MasterCode는 물리 삭제하지 않고 `deleted_at`을 사용하는 논리 삭제와 명시적인 복원을 지원한다.
삭제와 복원은 참조 수정과 같은 강한 `If-Match` 계약을 사용한다.

삭제할 때 현재 참조 Dimension과 MasterCode 행을 전역 순서로 잠그고 전체 ETag를 검사한 뒤 다음
상태를 한 트랜잭션에서 반영한다.

- 8개 Dimension 참조와 code는 그대로 보존한다.
- deleted_at과 updated_at을 같은 DB 시각으로 설정한다.
- version을 정확히 1 증가시킨다.

삭제된 MasterCode는 일반 목록과 단건 조회 및 참조 수정 대상에서 제외한다. 삭제된 MasterCode의
Dimension 참조는 해당 Dimension의 논리 삭제를 차단하지 않는다.

복원 권한이 있는 `ADMIN`과 `SUPER_ADMIN`은
`GET /master-codes/{id}/tombstone`에서 삭제된 MasterCode를 조회한다. 응답은 일반 단건 조회와
같은 필드에 `deleted_at`을 포함하고, 삭제된 Dimension을 포함한 8개 참조의 현재 상태로 계산한
강한 ETag를 제공한다. 활성 행이면 `409 MASTER_CODE_NOT_DELETED`, 존재하지 않으면 `404
MASTER_CODE_NOT_FOUND`이다. 이 경로는 복원 대상을 확인하고 현재 `If-Match` 값을 얻기 위한
읽기 전용 경로이며 목록 API에는 삭제 행 포함 옵션을 두지 않는다.

복원할 때 MasterCode 행과 모든 non-null Dimension 참조를 잠근다. 참조한 Dimension이 모두
활성 상태일 때만 복원할 수 있으며 하나라도 삭제 상태이면
`409 MASTER_CODE_REFERENCE_INACTIVE`로 거부한다. 복원 시 각 참조의 현재 code로 합성 code를
다시 계산하고 deleted_at을 `NULL`로 바꾸며 version을 정확히 1 증가시킨다. code가 달라진
경우에도 복원이라는 한 번의 상태 변경으로 처리하므로 version은 한 번만 증가하고 updated_at은
같은 DB 시각을 사용한다.

## 11. 잠금 순서와 트랜잭션 경계

승인 요청을 처리할 때는 먼저 해당 변경 요청 행을 `FOR UPDATE`로 잠그고 `PENDING` 상태인지
검사한다. 두 관리자의 중복 승인을 직렬화한 뒤 다음 도메인 행 잠금 순서를 따른다. 관리자 직접
작업에는 변경 요청 행 잠금이 없다.

MasterCode 생성·참조 수정·삭제·복원, Dimension 삭제와 Dimension code 변경은 다음 전역 잠금
순서를 공유한다.

1. Dimension 행
2. MasterCode 행

여러 Dimension을 잠글 때는 합성 순서인 Company, Brand, Model, Category, Year, Memory,
Network, Country 순서를 사용한다. 같은 타입에서 여러 행을 잠글 때는 UUID 오름차순으로
잠근다. 여러 MasterCode를 잠글 때도 UUID 오름차순을 사용한다. 트랜잭션 안에서 MasterCode를
먼저 잠근 뒤 Dimension 잠금을 추가로 획득하는 역순 경로를 만들지 않는다.

MasterCode 생성은 각 자리를 위 타입 순서대로 처리한다. `REFERENCE`는 대상 Dimension을
`FOR SHARE` 잠그고 활성 상태와 현재 code를 확인하며, `CREATE`는 신규 Dimension을 INSERT하고,
`NOT_APPLICABLE`은 잠글 행 없이 `NULL`로 확정한다. 8개 자리를 모두 해결한 뒤 합성하고
MasterCode를 INSERT한다.

MasterCode 참조 수정·삭제·복원은 잠금 대상과 현재 ETag 계산을 위해 현재 MasterCode를 먼저
잠금 없이 읽을 수 있다. 현재 참조와 참조 수정 후 사용할 `REFERENCE`의 모든 non-null
Dimension을 합쳐 위 순서대로 `FOR SHARE` 잠근다. 같은 타입의 현재 행과 교체할 행이 다르면
UUID 오름차순으로 둘 다 잠근다. 참조 수정의 `CREATE`는 같은 타입 순서에서 신규 Dimension을
INSERT하고 `NOT_APPLICABLE`에는 잠글 행이 없다.

그 뒤 MasterCode를 `FOR UPDATE`로 잠그고 상태를 다시 읽는다. 처음 읽은 뒤 참조나 MasterCode
version이 바뀌었다면 변경을 적용하지 않고 신규 Dimension INSERT까지 모두 롤백한다. 잠긴 현재
Dimension version들과 MasterCode version으로 ETag를 다시 계산해 `If-Match`와 비교한다. 검사를
통과한 경우에만 확정된 Dimension의 현재 code로 합성하고 같은 트랜잭션에서 MasterCode를
변경한다.

Dimension code 변경은 해당 Dimension을 `FOR UPDATE`로 잠근 뒤 활성·삭제 상태를 가리지 않고
그 Dimension을 참조하는 모든 MasterCode를 UUID 오름차순으로 `FOR UPDATE` 잠근다. 각
MasterCode의 다른 Dimension code는 MasterCode 잠금을 얻은 뒤 읽고 같은 합성 메서드로 code를
갱신한다. 변경된 MasterCode마다 version을 정확히 1 증가시키고 updated_at을 같은 mutation의
DB 시각으로 갱신한다. deleted_at은 기존 값을 유지한다.

모든 잠금, 검증, 합성, Dimension 변경과 MasterCode 변경은 하나의 DB 트랜잭션에서 수행한다.
하나라도 참조 상태, version, 유일성 또는 DB 검증에 실패하면 전체 트랜잭션을 롤백하며 부분
재합성이나 보상 트랜잭션을 허용하지 않는다. concurrent task 사이에는 SQLAlchemy 세션을
공유하지 않는다.

## 12. 변경 이력

모든 MasterCode 상태 변경은 append-only `MasterCodeLog`에 감사 이력을 남긴다. 다음 변경이
로그 대상이다.

- MasterCode 생성
- 하나 이상의 Dimension 참조 변경 또는 `NULL` 전환
- Dimension code 변경으로 인한 자동 재합성
- MasterCode 논리 삭제
- MasterCode 복원

로그에는 변경 전·후 상태, 변경 작업, 변경 완료 후 MasterCode version, 인증된 actor, 선택적
reason과 DB 변경 시각을 기록한다. no-op에는 로그를 만들지 않는다. 클라이언트는 actor를 요청
본문으로 지정할 수 없으며 DimensionLog와 같은 신뢰된 실행 컨텍스트를 사용한다.

MasterCode 상태 변경과 해당 로그 생성은 항상 같은 DB 트랜잭션에 포함한다. 로그 생성에
실패하면 MasterCode 변경과 이를 유발한 Dimension 변경까지 모두 롤백한다. 기존 로그의 UPDATE와
DELETE는 허용하지 않으며 로그 자체에는 `deleted_at`을 두지 않는다.

Dimension code 변경으로 여러 MasterCode가 재합성되면 해당 요청의 DimensionLog와 모든
MasterCodeLog가 같은 change set ID, actor, reason과 mutation timestamp를 사용한다. 각 로그에는
각 MasterCode의 변경 완료 후 version을 기록한다.

### 12.1 로그 필드

| 필드 | PostgreSQL 타입 | nullable | 기본값·제약 |
| --- | --- | --- | --- |
| `id` | `UUID` | 아니요 | PK, `uuidv7()` |
| `master_code_id` | `UUID` | 아니요 | MasterCode FK |
| `change_set_id` | `UUID` | 아니요 | 변경 요청 단위 UUIDv7 |
| `master_code_version` | `INTEGER` | 아니요 | 변경 완료 후 version, `>= 1` |
| `operation` | `VARCHAR(16)` | 아니요 | 아래 operation 중 하나 |
| `old_state` | `JSONB` | 조건부 | 생성은 SQL `NULL`, 나머지는 변경 전 스냅샷 |
| `new_state` | `JSONB` | 아니요 | 변경 후 스냅샷 |
| `reason` | `VARCHAR(500)` | 예 | 앞뒤 공백 제거, 빈 값은 `NULL` |
| `actor_kind` | `VARCHAR(6)` | 아니요 | `HUMAN`, `SYSTEM` |
| `actor_role` | `VARCHAR(11)` | 조건부 | `HUMAN`이면 `USER`, `ADMIN`, `SUPER_ADMIN`; `SYSTEM`이면 SQL `NULL` |
| `actor_id` | `VARCHAR(255)` | 아니요 | 1~255자, 앞뒤 공백 없음 |
| `changed_at` | `TIMESTAMPTZ` | 아니요 | 해당 mutation의 DB 시각 |

`(master_code_id, master_code_version)`은 UNIQUE이다. 따라서 MasterCode 한 건의 한 번 상태
변경에는 정확히 로그 한 행만 대응한다. FK는 `ON DELETE RESTRICT`, `ON UPDATE RESTRICT`를
사용한다.

`actor_kind`는 행위 주체의 종류이고 `actor_role`은 사람 actor가 변경 당시 보유한 권한 역할의
스냅샷이다. 사람의 역할이 나중에 바뀌어도 기존 로그의 `actor_role`은 바꾸지 않는다. 익명 쓰기는
허용하지 않으며 `HUMAN`과 `SYSTEM` 모두 안정적인 `actor_id`가 필수이다. DB CHECK는
`HUMAN`일 때 세 역할 중 하나를 요구하고 `SYSTEM`일 때 `actor_role IS NULL`을 요구한다.

### 12.2 operation

| operation | 발생 조건 | 스냅샷의 핵심 차이 |
| --- | --- | --- |
| `CREATE` | MasterCode 최초 생성 | old_state는 SQL `NULL`, new_state는 version 1 상태 |
| `REFERENCE_UPDATE` | 사용자가 하나 이상의 Dimension 참조를 변경하거나 해제 | 하나 이상의 참조가 다르고 code도 재합성 |
| `RECOMPOSE` | 참조한 Dimension의 code 변경 | 참조는 같고 합성 code가 다름 |
| `DELETE` | MasterCode 논리 삭제 | 참조와 code는 같고 deleted가 `false`에서 `true` |
| `RESTORE` | MasterCode 복원 | deleted가 `true`에서 `false`, 필요하면 code도 현재값으로 재합성 |

### 12.3 상태 스냅샷

old_state와 new_state는 추가 키 없이 다음 필드를 가진 JSON 객체이다.

```json
{
  "company_id": "UUID 또는 null",
  "brand_id": "UUID 또는 null",
  "model_id": "UUID 또는 null",
  "category_id": "UUID 또는 null",
  "year_id": "UUID 또는 null",
  "memory_id": "UUID 또는 null",
  "network_id": "UUID 또는 null",
  "country_id": "UUID 또는 null",
  "code": "합성 code",
  "deleted": false
}
```

UUID는 표준 소문자 하이픈 문자열로 기록하고 참조가 "해당 없음"이면 JSON `null`을 사용한다.
version과 변경 시각은 로그의 별도 컬럼에 있으므로 스냅샷에 중복 저장하지 않는다.

### 12.4 로그 생성과 변조 방지

애플리케이션 유스케이스는 mutation 트랜잭션을 시작할 때 UUIDv7 change set ID를 정확히 한 번
생성한다. repository는 MasterCode를 변경하기 전에 actor kind·ID와 조건부 actor role, reason,
change set ID, MasterCode operation과 mutation timestamp를 transaction-local 신뢰 컨텍스트에
설정한다. Dimension code 변경으로 재합성할 때는 actor, reason, change set ID와 mutation
timestamp만 그대로 재사용하고 MasterCode operation은 `RECOMPOSE`로 별도 설정한다. 요청 본문이나
쿼리 파라미터로 이 값을 받지 않는다.

MasterCode BEFORE INSERT/UPDATE 트리거는 신뢰 컨텍스트의 존재와 다음 상태 전이를 검증한다.

- INSERT는 `CREATE`이고 version은 1이며 deleted_at은 `NULL`이다.
- 참조가 실제로 바뀌면 `REFERENCE_UPDATE`이고 code도 같은 UPDATE에서 함께 변경되며 형식 CHECK를
  통과한다.
- 참조가 그대로이고 code만 바뀌면 `RECOMPOSE`이며 code는 형식 CHECK를 통과한다.
- deleted_at이 `NULL`에서 시각으로 바뀌면 `DELETE`이다.
- deleted_at이 시각에서 `NULL`로 바뀌면 `RESTORE`이다.
- 실제 상태 변경은 version을 정확히 1 증가시키고 updated_at에 컨텍스트의 mutation timestamp를
  사용한다.
- 상태가 바뀌지 않은 UPDATE는 허용하지 않는다.

트리거는 합성 알고리즘을 소유하지 않으므로 code가 참조들의 정확한 합성 결과인지는 재계산하지
않는다. 정확한 합성은 `MasterCode` 도메인 객체가 보장하고 트리거는 operation, 상태 전이, code의
동시 변경 여부와 구조적 형식만 검증한다.

MasterCode AFTER INSERT/UPDATE 트리거는 실제 `OLD`와 `NEW`에서 상태 스냅샷을 만들어
MasterCodeLog에 삽입한다. 애플리케이션은 MasterCodeLog 행을 직접 조립하거나 삽입하지 않는다.
MasterCodeLog의 BEFORE UPDATE/DELETE 트리거는 기존 로그 변경과 삭제를 거부하며 MasterCode의
BEFORE DELETE 트리거는 물리 삭제를 거부한다.

## 13. 오류 계약

모든 오류는 내부 SQL, 제약조건 이름과 기존 행 ID를 노출하지 않는 RFC 9457 Problem Details로
변환한다.

| 상황 | HTTP | 안정적인 code |
| --- | --- | --- |
| 인증되지 않은 쓰기 | 401 | `AUTHENTICATION_REQUIRED` |
| 인증됐지만 역할상 허용되지 않은 작업 | 403 | `AUTHORIZATION_DENIED` |
| 없는 또는 일반 경로에서 삭제된 MasterCode | 404 | `MASTER_CODE_NOT_FOUND` |
| 생성·참조 수정에서 없거나 삭제된 Dimension 참조 | 422 | `INVALID_DIMENSION_REFERENCE` |
| inline Dimension `CREATE`의 code 중복 | 409 | `DIMENSION_CODE_CONFLICT` |
| inline Dimension `CREATE`의 value 또는 동등 Memory 용량 중복 | 409 | `DIMENSION_VALUE_CONFLICT` |
| inline Dimension `CREATE`의 code와 value 모두 중복 | 409 | `DIMENSION_MULTIPLE_CONFLICTS` |
| 같은 참조 조합 또는 합성 code 중복 | 409 | `MASTER_CODE_CONFLICT` |
| 복원 중 non-null Dimension 참조가 삭제 상태 | 409 | `MASTER_CODE_REFERENCE_INACTIVE` |
| 활성 MasterCode 복원 | 409 | `MASTER_CODE_NOT_DELETED` |
| If-Match 불일치 | 412 | `PRECONDITION_FAILED` |
| 타입·형식·필수 필드 위반 | 422 | `VALIDATION_ERROR` |
| If-Match 누락 | 428 | `PRECONDITION_REQUIRED` |
| If-Match 문법 오류 | 400 | `INVALID_IF_MATCH` |

생성과 참조 수정에서는 모든 non-null Dimension 참조를 사전 조회해 잘못된 필드의 위치를
`company_id`, `memory_id`처럼 violations에 함께 담는다. 존재하지 않는 행과 논리 삭제된 행은
모두 사용할 수 없는 참조이므로 같은 `INVALID_DIMENSION_REFERENCE`로 응답한다. 동시 요청의
최종 무결성은 FK와 행 잠금이 보장하며 경쟁 중 뒤늦게 확인한 단일 위반만 반환할 수 있다.

복원은 과거에 유효했던 참조를 가진 기존 MasterCode의 현재 상태와 충돌하는 경우이므로
`MASTER_CODE_REFERENCE_INACTIVE`를 사용한다. 어느 Dimension이 삭제 상태인지 violations에
필드 위치를 제공하되 내부 데이터나 해당 행의 다른 필드는 노출하지 않는다.

inline Dimension `CREATE`의 사전 검사와 DB UNIQUE 충돌은 Dimension 계약의 세 안정적인 충돌
code를 그대로 사용하고 해당 타입의 요청 위치를 violations에 담는다. 승인 중 충돌하면 신규
Dimension, MasterCode, 모든 로그와 요청 상태 전이를 롤백하며 요청은 `PENDING`으로 유지한다.

## 14. 권한과 승인 경계

사람 사용자의 역할은 `USER`, `ADMIN`, `SUPER_ADMIN`의 세 단계이다.

| 작업 | USER | ADMIN | SUPER_ADMIN |
| --- | --- | --- | --- |
| 활성 MasterCode 일반 조회 | 허용 | 허용 | 허용 |
| 삭제 MasterCode tombstone 조회 | 금지 | 허용 | 허용 |
| 생성·참조 수정·삭제 요청 제출 | 허용 | 해당 없음 | 해당 없음 |
| MasterCode 직접 생성·참조 수정·삭제·복원 | 금지 | 허용 | 허용 |
| USER가 제출한 변경 요청 승인·거절 | 금지 | 허용 | 허용 |
| 하위 역할을 자신의 역할까지 승격 | 금지 | USER를 ADMIN으로 승격 | USER·ADMIN을 SUPER_ADMIN으로 승격 |
| 시스템·역할 관리 | 금지 | 금지 | 허용 |

`SYSTEM`은 사람 역할과 별개인 신뢰된 내부 super-user이다. 이 계약에 포함된 모든 조회와
Dimension·MasterCode 생성·수정·삭제·복원을 사람의 요청이나 승인 없이 수행할 수 있다. 다만
도메인 검증, ETag 검사, 유일성, 잠금 순서, 트랜잭션과 감사 로그는 우회할 수 없다. 공개 API의
인증 주체는 `SYSTEM`을 주장할 수 없고, 서버에 등록된 내부 실행 identity만 사용할 수 있다.
SYSTEM 로그는 `actor_kind=SYSTEM`, `actor_role=NULL`과 실제 내부 실행 주체의 `actor_id`를
기록한다.

따라서 `actor_kind`는 권한 서열이 아니라 변경의 기원을 나타낸다. 사람이 직접 수행하거나 승인한
변경과 그로부터 파생된 재합성은 `HUMAN` 및 당시 역할을 기록하고, 사람의 개별 승인 없이 내부
자동 작업이 시작한 변경과 파생 작업은 `SYSTEM`을 기록한다.

일반 사용자의 요청 제출은 MasterCode 상태를 직접 바꾸지 않는다. 관리자가 승인한 시점에 실제
변경 트랜잭션을 실행하며, 요청자와 승인자 및 원본 요청을 감사 가능하게 연결한다. 관리자가 직접
수행한 MasterCode 변경은 별도 승인 없이 즉시 반영한다.

Dimension code 변경으로 발생하는 `RECOMPOSE`는 별도 승인 대상이 아니다. 원인이 된 Dimension
변경이 승인된 요청에서 시작됐다면 그 요청과 승인 컨텍스트를 이어받고, 관리자의 직접 변경에서
시작됐다면 해당 관리자 컨텍스트를 이어받는다.

인증, 역할 저장, 변경 요청의 상태 모델과 승인 API 구현은 이 문서의 구현 범위가 아니다. 이
접근 계약은 인증·인가 계약 티켓과 후속 MasterCode 구현 티켓에 반영한다.

### 14.1 사용자 변경 요청과 관리자 검토

사용자 변경 요청의 상태는 다음 네 가지이다.

- `PENDING`: 관리자 검토 전 또는 충돌 확인 후 아직 결정하지 않은 상태
- `APPROVED`: 사용자가 요청한 내용을 수정하지 않고 승인한 상태
- `REJECTED`: 요청을 적용하지 않고 거절한 상태
- `MODIFIED_AND_APPROVED`: 관리자가 요청 내용을 수정해 승인한 상태

사용자가 제출한 원본 operation, 대상, payload, operation별 expected ETag, requester와 제출
시각은 이후 덮어쓰지 않는다. 관리자가 수정 후 승인하면 실제 적용한 approved payload와
operation별 적용 기준 ETag, approver, 승인 사유 및 승인 시각을 별도 필드로 보존한다.
승인·거절된 요청을 다시 변경하거나 재처리할 수 없다.

사용자가 제출할 수 있는 operation은 `CREATE`, `REFERENCE_UPDATE`, `DELETE`이다. 일반 사용자는
`RESTORE` 요청을 제출하지 않으며 관리자가 직접 복원한다.

- 원본 `CREATE`는 대상 MasterCode와 expected ETag가 모두 SQL `NULL`이어야 한다.
- 원본 `REFERENCE_UPDATE`와 `DELETE`는 대상 MasterCode ID와 제출 당시 expected ETag가 필수이다.
- 승인 적용안이 `CREATE`이면 적용 대상 ID와 적용 기준 ETag가 모두 SQL `NULL`이다.
- 승인 적용안이 `REFERENCE_UPDATE`, `DELETE` 또는 생성 대신 수행하는 `RESTORE`이면 적용 대상
  MasterCode ID와 관리자가 검토한 적용 기준 ETag가 필수이다.

관리자는 원칙적으로 수정 후 승인할 때 operation과 대상 MasterCode를 바꾸지 않는다. 다만
`CREATE` 요청과 같은 참조 조합의 논리 삭제된 MasterCode가 이미 있다면 새 행을 생성하지 않고
그 행을 `RESTORE`하는 승인 적용안으로 바꿀 수 있다. 이 경우 원본 operation `CREATE`와 대상이
없던 원본 요청은 그대로 보존하고, approved operation `RESTORE`와 복원한 MasterCode ID를 별도로
기록하며 상태를 `MODIFIED_AND_APPROVED`로 만든다. 그 밖에 operation 또는 대상 변경이 필요하면
기존 요청을 거절하고 별도의 작업으로 처리한다.

`REFERENCE_UPDATE` 또는 `DELETE`의 제출 당시 expected ETag와 검토 시점의 MasterCode ETag가
다르더라도 요청을 자동으로 종료하지 않는다. 관리자는 원본 요청, 현재 MasterCode 상태와 실제
적용 예정 상태의 차이를 모두 확인한 뒤 거절하거나 수정 후 승인할 수 있다. 충돌을 무시하고 원본
payload를 최신 상태에 자동으로 적용하지 않는다.

원본 payload를 그대로 적용하더라도 관리자가 제출 당시 값과 다른 최신 적용 기준 ETag를 선택하면
원본 precondition을 수정한 것이므로 `MODIFIED_AND_APPROVED`이다. approved payload는 원본과 같은
내용도 별도로 저장하고 최신 적용 기준 ETag 및 필수 review_message를 함께 보존한다.

관리자가 승인할 때 실제 적용 기준 ETag를 조건으로 관련 Dimension과 MasterCode를 잠그고 다시
검사한다. 검토 후 승인 트랜잭션 전에 ETag가 다시 바뀌면 변경을 적용하지 않고 요청을
`PENDING`으로 유지해
관리자가 최신 상태를 다시 검토하게 한다.

승인으로 실행된 MasterCodeLog는 실제 변경을 수행한 관리자를 actor로 기록하고 변경 요청을
식별할 수 있게 연결한다. `MODIFIED_AND_APPROVED`에서는 원본 payload와 approved payload를
변경 요청 기록에서 모두 확인할 수 있어야 한다.

승인된 변경 요청은 실제 적용 트랜잭션의 `applied_change_set_id`를 저장한다. 해당 요청으로
생성된 모든 DimensionLog와 MasterCodeLog는 이 값을 자신의 `change_set_id`로 사용한다. 로그마다
`change_request_id`를 중복 저장하지 않고 요청의 `applied_change_set_id`로 전체 감사 이력을
찾는다.

`applied_change_set_id`는 변경 요청 테이블에서 UNIQUE이며 `APPROVED`와
`MODIFIED_AND_APPROVED`일 때만 값이 있다. `PENDING`과 `REJECTED`에서는 `NULL`이다. 실제 데이터
변경·로그 생성과 요청의 승인 상태 및 applied_change_set_id 저장은 같은 트랜잭션에서 수행한다.
관리자의 직접 작업은 독립 change set을 사용하며 변경 요청과 연결하지 않는다.

관리자 `review_message`는 `MODIFIED_AND_APPROVED`와 `REJECTED`에서 필수이고, 요청 내용 그대로
승인한 `APPROVED`에서는 선택이다. 메시지는 변경 요청 기록에 영구 보존하며 요청자가 요청 상태를
조회할 때 함께 반환한다. 로그 reason과 동일하게 앞뒤 일반 공백만 제거하고 Unicode, 대소문자와
내부 일반 공백은 보존한다. 정규화 후 1~500자만 허용하며 빈 값, 탭, 줄바꿈과 제어문자는
거부한다. `APPROVED`에서 메시지를 생략한 경우만 SQL `NULL`을 허용한다.

승인으로 실제 데이터 변경과 로그를 만들 때 로그의 `reason`은 관리자의 정규화된
`review_message`를 사용한다. `APPROVED`에서 메시지를 생략하면 로그 reason은 SQL `NULL`이다.
사용자가 원본 요청에 적은 설명은 원본 요청 기록에만 보존하며 감사 로그 reason으로 복사하지
않는다. `REJECTED`는 데이터 변경과 로그가 없으므로 review_message만 요청 기록에 남는다.

승인 적용안이 현재 상태와 같아 실제 변경이 없으면 승인하지 않는다. 같은 활성 MasterCode가 이미
있는 생성 요청, 최종 참조가 현재 참조와 같은 수정 요청과 이미 삭제된 대상의 삭제 요청은
관리자가 현재 상태를 설명하는 review_message와 함께 `REJECTED` 처리한다. 이 경우
applied_change_set_id, 데이터 변경과 감사 로그는 만들지 않는다. 논리 삭제된 기존 MasterCode를
복원하는 생성 요청은 실제 상태 변경이 있으므로 이 no-op 규칙에 해당하지 않는다.

이메일, 푸시 또는 외부 메시지 같은 알림 전송은 현재 범위에 포함하지 않는다. 현재는 요청자가
상태 조회로 처리 결과와 관리자 메시지를 확인하며, 능동 알림은 별도 후속 요구사항으로 검토한다.

### 14.2 Dimension 생성을 포함한 MasterCode 요청

사용자는 Dimension만 별도로 추가하는 요청을 제출하지 않는다. 사용자 관점의 MasterCode
`CREATE` 요청과 `REFERENCE_UPDATE` 요청에서 각 대상 Dimension 자리마다 다음 중 하나를
제안한다.

- `REFERENCE`: 이미 존재하는 활성 Dimension 참조
- `CREATE`: 승인 시 새로 생성할 Dimension의 code와 타입별 value
- `NOT_APPLICABLE`: "해당 없음"

관리자가 요청을 승인하면 기존 참조를 검증하고 제안된 신규 Dimension을 생성한 뒤 그 결과 UUID와
`NULL` 자리를 사용해 MasterCode를 생성하거나 수정한다. 신규 Dimension 생성, DimensionLog
생성, MasterCode 생성·수정과 MasterCodeLog 생성은 하나의 DB 트랜잭션과 change set에서
수행한다. 하나라도 유일성, 참조, version, 검증 또는 감사 로그 생성에 실패하면 모두 롤백하고
요청은 승인 상태로 전이하지 않는다.

관리자는 신규 Dimension 생성 제안을 다른 입력으로 수정하거나 기존 Dimension 참조로 바꾼 뒤
`MODIFIED_AND_APPROVED`로 승인할 수 있다. 실제 생성된 Dimension과 MasterCode의 actor는 승인한
관리자이고, 두 감사 로그는 원본 요청과 승인 적용안을 추적할 수 있어야 한다.

승인 적용안의 참조 조합과 같은 논리 삭제된 MasterCode가 있으면 UNIQUE 충돌을 무시하거나 새
행을 만들지 않는다. 관리자는 해당 행의 모든 non-null Dimension이 활성 상태인지 확인한 뒤
명시적으로 복원을 선택할 수 있다. 복원에 성공하면 요청은 `MODIFIED_AND_APPROVED`, 실제
MasterCodeLog operation은 `RESTORE`가 된다. 관리자 검토 메시지에는 신규 생성 대신 기존
MasterCode를 복원했다는 사실을 기록해 요청자에게 보여준다.

## 15. 애플리케이션과 DB 책임

| 규칙 | 도메인·애플리케이션 | PostgreSQL |
| --- | --- | --- |
| Dimension code | 공통 `DimensionCode`가 정규화하고 N-only 예약 패턴 거부 | 정규형과 `^N+$` 제외 CHECK |
| MasterCode 합성 | `MasterCode`가 불변 순서와 `NNN` 규칙으로 한 번만 구현 | 형식·길이 CHECK, 알고리즘 중복 구현 안 함 |
| 요청 입력 | tagged union과 타입별 value 검증, 필드별 오류 수집 | 물리 타입과 최종 제약 |
| 참조 확인 | 유스케이스가 고정 순서의 잠금 의도 결정 | nullable FK와 `FOR SHARE` 잠금 |
| 유일성 | 사전 조회로 이해 가능한 충돌 안내 | code와 `NULLS NOT DISTINCT` 참조 조합 UNIQUE |
| 조건부 변경 | ETag 계산·If-Match 해석, 현재 상태 재검사 | 행 잠금과 원자적 UPDATE |
| version·시각 | 변경 의도와 mutation context 설정 | CHECK와 BEFORE 트리거 검증 |
| 감사 로그 | actor·reason·change set·operation context 설정 | AFTER 트리거 생성, UPDATE·DELETE 차단 |
| 승인 | 역할·원본/승인안·상태 전이 결정 | 요청 행 잠금과 데이터 변경을 한 트랜잭션으로 커밋 |
| 오류 | 제약 위반을 RFC 9457 Problem Details로 변환 | FK·UNIQUE·CHECK가 최종 무결성 보장 |

합성 알고리즘을 SQL 함수나 트리거에 복제하지 않으므로 PostgreSQL은 code가 실제 참조의 현재
code로 만들어졌는지 독립적으로 재계산하지 않는다. 정상 쓰기 경로는 반드시 `MasterCode` 도메인
객체를 사용한다. DB는 합성 code가 정확히 8개의 `A-Z0-9` 부분으로 구성되고 각 부분이 1~32자이며
하이픈 7개로 구분되는지 CHECK한다. 런타임 DB credential이 탈취되거나 트리거를 우회할 권한이
있는 직접 SQL에 대한 방어는 현재 신뢰 경계 밖이다.

## 16. 인덱스 기준

MasterCode 테이블의 초기 인덱스는 다음과 같다.

- UUID PK
- code UNIQUE
- 8개 FK 조합의 `UNIQUE NULLS NOT DISTINCT`
- `(created_at, id) WHERE deleted_at IS NULL` 활성 목록 인덱스
- 각 non-null Dimension FK의 개별 인덱스

Dimension FK 인덱스는 Dimension code 변경의 영향 행 검색과 Dimension 삭제 전 활성 참조 확인에
사용한다. 실제 쿼리 실행 계획을 확인하지 않고 조합 인덱스를 추가하지 않는다.

MasterCodeLog 테이블의 초기 인덱스는 다음과 같다.

- UUID PK
- `(master_code_id, master_code_version)` UNIQUE
- `(master_code_id, changed_at, id)` 변경 이력 인덱스
- `(change_set_id, master_code_id)` change set 조회 인덱스

각 DimensionLog 테이블은 Dimension 계약의 개별 이력 인덱스와 별도로
`(change_set_id, dimension_id, id)` 인덱스를 둔다. 변경 요청의 `applied_change_set_id`에서 시작해
8개 DimensionLog를 찾을 때 전체 스캔하지 않도록 change_set_id를 선두 컬럼으로 둔다.

변경 요청 테이블은 UUID PK, UNIQUE applied_change_set_id와 `(status, created_at, id)` 검토 대기열
인덱스를 둔다. requester, approver, operation 단독 인덱스는 실제 조회 요구와 실행 계획을 확인한
후 추가한다.

## 17. 수용 테스트 벡터

### 17.1 합성과 조회

| 사례 | 기대 결과 |
| --- | --- |
| 8개 기존 참조로 생성 | 합성 순서와 하이픈 규칙에 맞는 code, version 1 |
| Country만 `NOT_APPLICABLE` | 마지막 부분이 `NNN`, country_id와 응답 dimensions.country는 `null` |
| 모든 자리가 `NOT_APPLICABLE` | `NNN-NNN-NNN-NNN-NNN-NNN-NNN-NNN` |
| 같은 입력을 반복 합성 | 바이트 단위로 같은 code |
| 모든 자리 N/A인 ETag 고정 벡터 | 정의된 JSON 바이트와 SHA-256 결과가 정확히 일치 |
| Dimension value만 변경 | MasterCode version·code 불변, 전체 응답 ETag 변경 |
| Dimension code 변경 | 관련 활성·삭제 MasterCode code·version·ETag 변경 |
| 합성 순서 상수 변경 | 고정 테스트 벡터 실패로 계약 변경 감지 |

### 17.2 입력과 유일성

| 사례 | 기대 결과 |
| --- | --- |
| Dimension code `N`, `NNN`, `nnnn` | 정규화 후 N-only 예약 패턴으로 거부 |
| Dimension code `N1`, `AN`, `NAN` | 허용 |
| 생성 요청의 자리 누락 | 422 `VALIDATION_ERROR` |
| mode별 허용하지 않은 추가 필드 | 422 `VALIDATION_ERROR` |
| Company에만 존재하는 UUID를 Company와 Brand `REFERENCE.id`에 함께 입력 | `brand_id` 위치의 422 `INVALID_DIMENSION_REFERENCE` |
| 서로 다른 타입 테이블에 우연히 같은 UUID가 각각 존재 | 서로 다른 Dimension 행으로 각각 참조 가능 |
| 없거나 삭제된 `REFERENCE` | 필드별 422 `INVALID_DIMENSION_REFERENCE` |
| inline `CREATE`의 code, value 또는 둘 다 중복 | 해당 `DIMENSION_*_CONFLICT` 409, 전체 롤백 |
| 같은 참조 조합 재생성 | 409 `MASTER_CODE_CONFLICT` |
| `NULL` 위치까지 같은 참조 조합 재생성 | `NULLS NOT DISTINCT`에 의해 409 |
| 삭제된 MasterCode와 같은 조합 생성 | 신규 생성 불가, 관리자 복원 선택 가능 |

### 17.3 조건부 변경과 수명주기

| 사례 | 기대 결과 |
| --- | --- |
| 하나 이상의 참조 교체 | code 재합성, version 정확히 1 증가, 로그 한 행 |
| `CREATE` mode를 포함한 참조 수정 승인 | Dimension·로그·MasterCode·로그를 한 트랜잭션에서 생성·변경 |
| 최종 참조가 현재 상태와 같음 | 직접 변경은 no-op, 사용자 요청 승인은 메시지와 함께 `REJECTED` |
| Dimension value 변경 뒤 예전 ETag로 수정 | 412, 변경과 로그 없음 |
| USER의 tombstone 조회 또는 직접 복원 | 403 `AUTHORIZATION_DENIED`, 상태 변화 없음 |
| MasterCode 삭제 | 참조·code 보존, deleted_at 설정, version 1 증가 |
| 활성 MasterCode가 참조한 Dimension 삭제 | 409 `DIMENSION_IN_USE` |
| 삭제 MasterCode만 참조한 Dimension 삭제 | 허용, MasterCode 참조는 보존 |
| 비활성 Dimension 참조가 있는 복원 | 409 `MASTER_CODE_REFERENCE_INACTIVE` |
| 활성 참조만 있는 복원 | 현재 code로 재합성, deleted_at 해제, version 1 증가 |
| 삭제 행 tombstone 조회 | 현재 참조 상태의 ETag 반환, 상태 변화 없음 |

### 17.4 승인과 동시성

| 사례 | 기대 결과 |
| --- | --- |
| 두 관리자가 같은 요청 승인 | 요청 행 잠금으로 한 명만 최종 상태 전이 |
| 두 요청이 같은 Dimension `CREATE` | 한 트랜잭션만 성공, 다른 요청은 `PENDING` 유지 |
| 두 요청이 같은 MasterCode 생성 | 한 트랜잭션만 성공, 다른 요청은 `PENDING` 유지 |
| 승인 검토 뒤 ETag 재변경 | 전체 롤백, 요청은 `PENDING` 유지 |
| 원본 payload 그대로 최신 ETag로 승인 | 필수 메시지와 approved payload를 보존하고 `MODIFIED_AND_APPROVED` |
| MasterCode 생성과 Dimension code 변경 경합 | 오래된 합성 code가 커밋되지 않음 |
| 단건 조회의 JOIN 실행 중 Dimension 변경 커밋 | 본문과 ETag가 모두 변경 전 또는 모두 변경 후 snapshot |
| 참조 수정과 대상 Dimension 삭제 경합 | 둘 중 잠금 획득 순서에 따른 유효한 한 결과만 커밋 |
| 서로 다른 Dimension code 동시 변경 | MasterCode UUID 잠금 순서로 직렬화, 최종 code는 두 변경 모두 반영 |
| fan-out 재합성 중 한 로그 실패 | Dimension·모든 MasterCode·모든 로그 전체 롤백 |

### 17.5 감사

| 사례 | 기대 결과 |
| --- | --- |
| 사용자 요청 그대로 승인 | `APPROVED`, 요청 applied_change_set_id와 모든 로그 change_set_id 일치 |
| 관리자가 payload 수정 후 승인 | 원본·승인안 보존, 필수 메시지, `MODIFIED_AND_APPROVED` |
| 생성 대신 삭제 행 복원 | 요청은 수정 승인, 실제 MasterCodeLog는 `RESTORE` |
| 요청 거절 | 필수 메시지, 데이터·change set·로그 없음 |
| CREATE 요청에 expected ETag 또는 대상 ID 지정 | 422 `VALIDATION_ERROR` |
| REFERENCE_UPDATE·DELETE 요청의 대상 ID 또는 expected ETag 누락 | 422 `VALIDATION_ERROR` |
| review_message 501자 또는 탭·줄바꿈·제어문자 포함 | 422 `VALIDATION_ERROR`, 상태 변화 없음 |
| 관리자 직접 변경 | 감사 로그 존재, 연결된 변경 요청 없음 |
| HUMAN actor 로그 | actor_kind는 `HUMAN`, 변경 당시 역할은 actor_role에 저장되고 이후 역할 변경에도 불변 |
| SYSTEM actor 로그 | actor_kind는 `SYSTEM`, actor_role은 SQL `NULL`; 승인 없이 변경하되 모든 무결성 규칙 적용 |
| HUMAN인데 actor_role 누락 또는 미지원 값 | DB CHECK가 거부하고 상태·로그 변화 없음 |
| SYSTEM인데 actor_role 지정 | DB CHECK가 거부하고 상태·로그 변화 없음 |
| no-op 직접 수정 | version·시각·로그 변화 없음 |
| MasterCodeLog UPDATE 또는 DELETE | DB 트리거가 거부 |

### 17.6 후속 구현 티켓 매핑

- #6은 17.1의 생성·조회·ETag 벡터, 17.2의 생성 입력·유일성 벡터와 17.4의 동시 MasterCode
  생성을 구현한다.
- #8은 17.1의 활성·삭제 MasterCode 재합성과 17.4의 생성 중 Dimension code 변경 경합, 서로
  다른 Dimension code 동시 변경 및 fan-out 로그 실패 원자성을 구현한다.
- #30은 17.3의 참조 수정·no-op·stale ETag와 같은 MasterCode의 동시 참조 수정을 구현한다.
- #31은 17.3의 삭제·tombstone·복원 및 활성·삭제 MasterCode의 Dimension 삭제 차단 차이를
  구현하고, #17·#30이 준비된 뒤 참조 수정과 대상 Dimension 삭제 경합을 교차 검증한다.
- #32는 17.2의 tagged union 요청, 17.4의 승인·충돌·inline 생성 동시성과 17.5의 변경 요청 감사
  연결을 구현한다.
- #22, #16과 #27은 역할별 접근, HUMAN·SYSTEM 실행 주체, transaction-local 감사 컨텍스트와
  401·403 벡터를 구현 티켓이 공통으로 사용할 수 있게 정의·구현한다.
