# Dimension 데이터 계약과 무결성 규칙

## 1. 문서 상태와 범위

이 문서는 GitHub 이슈 #2에서 합의한 Dimension 및 DimensionLog의 구현 정본이다. 후속 구현
티켓은 이 계약을 참조하며 서로 다른 규칙을 다시 정의하지 않는다.

이 문서가 정의하는 범위는 다음과 같다.

- 고정 Dimension 타입과 타입별 필드
- 입력 정규화, 유일성, 수정, 삭제, 복원 규칙
- 낙관적 버전 검사, 행 잠금, 트랜잭션 경계
- DimensionLog의 필드, 생성 조건, 불변성
- 애플리케이션과 PostgreSQL의 검증 책임
- 정상값, 정규화값, 경계값, 거부값
- 후속 구현 티켓의 책임 경계

Dimension·MasterCode CRUD 구현, Alembic 마이그레이션, 인증 구현, MasterCode 합성 알고리즘,
SCD Type 2 조회 모델은 이 티켓의 구현 범위가 아니다. DimensionLog는 변경 사실을 보존하는
append-only 감사 기록이며 과거 시점의 Dimension을 서비스하는 SCD Type 2 모델이 아니다.

## 2. 고정 Dimension 타입

Dimension 타입은 운영 중 추가할 수 있는 데이터가 아니라 코드와 마이그레이션으로 관리하는
폐쇄된 집합이다. 공통 `dimensions` 테이블과 `type` 컬럼을 두지 않는다.

| 타입 | 테이블 | 도메인 값 객체 | 물리 value 표현 |
| --- | --- | --- | --- |
| Year | `dimension_years` | `YearValue` | `value SMALLINT` |
| Memory | `dimension_memories` | `MemoryValue` | `amount`, `unit`, `capacity_mb` |
| Company | `dimension_companies` | `CompanyValue` | `value VARCHAR(128)` |
| Model | `dimension_models` | `ModelValue` | `value VARCHAR(128)` |
| Brand | `dimension_brands` | `BrandValue` | `value VARCHAR(128)` |
| Country | `dimension_countries` | `CountryValue` | `value VARCHAR(128)` |
| Network | `dimension_networks` | `NetworkGeneration` | `value SMALLINT` |
| Category | `dimension_categories` | `CategoryValue` | `value VARCHAR(128)` |

새 타입은 테이블, 마이그레이션, 값 객체, repository, API 계약과 테스트를 명시적으로 추가해야
한다. `OTHER` 같은 열린 타입이나 별도의 `DimensionItem` 모델은 두지 않는다.

## 3. 공통 필드 계약

모든 Dimension 테이블은 다음 공통 필드를 가진다.

| 필드 | PostgreSQL 타입 | nullable | 기본값 | 유일성·제약 | 변경 정책 |
| --- | --- | --- | --- | --- | --- |
| `id` | `UUID` | 아니요 | `uuidv7()` | PK | 변경 금지 |
| `code` | `VARCHAR(32)` | 아니요 | 없음 | 테이블 내 UNIQUE, `^[A-Z0-9]{1,32}$` | 조건부 PATCH로 변경 가능 |
| `version` | `INTEGER` | 아니요 | `1` | `version >= 1` | 실제 상태 변경마다 정확히 1 증가 |
| `created_at` | `TIMESTAMPTZ` | 아니요 | `CURRENT_TIMESTAMP` | DB 시각 | 변경 금지 |
| `updated_at` | `TIMESTAMPTZ` | 아니요 | `CURRENT_TIMESTAMP` | DB 시각 | 실제 상태 변경 시 갱신 |
| `deleted_at` | `TIMESTAMPTZ` | 예 | `NULL` | 활성 상태는 `IS NULL` | 삭제·복원 명령만 변경 가능 |

클라이언트는 `id`, `version`, `created_at`, `updated_at`, `deleted_at`을 입력하지 않는다. 모든
업무 필드에는 기본값이 없으며 명시적인 입력이 필요하다. 현재 연도, 기본 Network, 기본 Memory
단위 같은 추정 기본값을 두지 않는다.

모든 테이블은 다음 시간·버전 관계를 CHECK로 보장한다.

```text
version >= 1
updated_at >= created_at
deleted_at IS NULL OR (
    deleted_at >= created_at
    AND deleted_at <= updated_at
)
```

시간은 PostgreSQL의 `CURRENT_TIMESTAMP`를 사용한다. 애플리케이션 호스트의 로컬 시계를
저장하지 않으며 API에서는 UTC 기준 시각으로 직렬화한다.

## 4. 타입별 value 계약

### 4.1 Year

| 항목 | 규칙 |
| --- | --- |
| 타입 | `SMALLINT NOT NULL` |
| 허용 범위 | `2000` 이상 `2999` 이하 |
| 기본값 | 없음 |
| 유일성 | 테이블 내 `UNIQUE(value)` |
| 입력 | 엄격한 JSON 정수 |

`"2026"`, `2026.0`, `true`는 정수로 변환하지 않고 거부한다.

### 4.2 Memory

Memory의 논리적 value는 `MemoryValue(amount, unit)` 하나이며 DB에서는 다음 컬럼으로 펼친다.

| 필드 | 타입 | nullable | 기본값 | 제약 |
| --- | --- | --- | --- | --- |
| `amount` | `INTEGER` | 아니요 | 없음 | `amount > 0` |
| `unit` | `VARCHAR(2)` | 아니요 | 없음 | `MB`, `GB`, `TB`, `PB` 중 하나 |
| `capacity_mb` | `BIGINT` | 아니요 | 생성식 | `UNIQUE(capacity_mb)` |

단위는 십진 SI 배수를 사용한다.

```text
MB = amount
GB = amount * 1,000
TB = amount * 1,000,000
PB = amount * 1,000,000,000
```

`capacity_mb`는 `GENERATED ALWAYS AS (...) STORED` 컬럼이며 클라이언트와 애플리케이션이
직접 지정하지 않는다. `1 TB`, `1000 GB`, `1000000 MB`는 같은 value이므로 동시에 저장할 수
없다. 동일 행을 동등한 용량의 다른 단위 표현으로 PATCH해도 no-op이며 최초 입력의 `amount`와
`unit`을 보존한다. `amount`는 양의 PostgreSQL INTEGER 범위만 사용하며 별도의 업무상 상한은
두지 않는다.

도메인에서는 `MemoryUnit` Enum을 사용한다. DB 네이티브 ENUM 대신 문자열과 CHECK를 사용한다.
단위 입력은 앞뒤 일반 공백을 제거하고 대문자로 바꾼 다음 검증한다. `" gb "`는 `GB`가 되지만
내부 공백, 탭, 줄바꿈, 제어문자는 거부한다.

### 4.3 Network

| 항목 | 규칙 |
| --- | --- |
| 타입 | `SMALLINT NOT NULL` |
| 허용 범위 | `1` 이상 `5` 이하 |
| 기본값 | 없음 |
| 유일성 | 테이블 내 `UNIQUE(value)` |
| 입력 | 엄격한 JSON 정수 |

도메인에서는 산술 연산을 허용하는 `IntEnum` 대신 값이 `1`부터 `5`인 일반
`NetworkGeneration` Enum을 사용한다. `"5"`, `"5G"`, `true`는 거부한다. `5G` 같은 표시값은
저장하지 않고 표현 계층에서 파생한다.

### 4.4 Company, Model, Brand, Country, Category

다섯 타입은 공통 문자열 규칙을 사용한다.

| 항목 | 규칙 |
| --- | --- |
| 타입 | `VARCHAR(128) NOT NULL` |
| 허용 길이 | 정규화 후 1자 이상 128자 이하 |
| 허용 문자 | ASCII `A-Z`, `0-9`, `_` |
| 정규형 | `^[A-Z0-9]+(?:_[A-Z0-9]+)*$` |
| 기본값 | 없음 |
| 유일성 | 테이블 내 `UNIQUE(value)` |

문자열 value 정규화 순서는 다음과 같다.

1. 입력이 JSON 문자열인지 엄격하게 검사한다.
2. 앞뒤 일반 공백을 제거한다.
3. ASCII 소문자를 대문자로 바꾼다.
4. 내부의 연속된 일반 공백을 `_` 하나로 바꾼다.
5. 연속된 `_`를 `_` 하나로 축약한다.
6. 앞뒤 `_`를 제거한다.
7. 정규형, 길이, 빈 값 여부를 검사한다.

탭, 줄바꿈, 제어문자, 비 ASCII 문자와 그 밖의 특수문자는 자동 변환하지 않고 거부한다.
`A_B_C`처럼 `_`가 여러 위치에 나타날 수 있지만 `A__B`는 `A_B`로 저장한다.

## 5. code 계약

`code`는 MasterCode 합성에 사용하는 대표 코드이며 모든 Dimension 타입에서 같은 값 객체와
규칙을 사용한다.

- JSON 문자열만 받는다.
- 앞뒤 공백과 내부 공백을 허용하지 않는다.
- ASCII 소문자는 서버가 대문자로 바꾼다.
- 정규화 후 `A-Z`, `0-9`만 허용한다.
- 길이는 1자 이상 32자 이하이다.
- `_`, `-`, Unicode 문자와 그 밖의 특수문자는 거부한다.
- 테이블 안에서 유일하지만 서로 다른 Dimension 테이블 사이의 동일한 code는 허용한다.
- code는 value로부터 자동 파생하지 않는다.

## 6. 정규화와 유일성

도메인 값 객체가 정규화와 사용자 친화적인 오류 생성을 담당한다. PostgreSQL은 값을 자동으로
수정하지 않고 CHECK로 정규형만 허용한다. 직접 SQL로 비정규형을 삽입하면 DB가 거부한다.

각 Dimension 테이블에는 다음 유일성 규칙을 적용한다.

- `UNIQUE(code)`
- Year, Network, 문자열 Dimension: `UNIQUE(value)`
- Memory: `UNIQUE(capacity_mb)`

논리 삭제된 행도 유일성 범위에 포함한다. 삭제된 code나 value로 새 행을 생성할 수 없으며 기존
행을 복원해야 한다. 중복 사전 조회는 여러 필드 오류를 한 번에 안내하기 위한 사용자 경험용
검사이고, 동시 요청의 최종 무결성은 이름이 고정된 DB UNIQUE 제약이 보장한다.

## 7. 도메인 모델

각 값은 immutable Value Object로 모델링한다.

```text
Dimension[YearValue]
Dimension[MemoryValue]
Dimension[CompanyValue]
Dimension[ModelValue]
Dimension[BrandValue]
Dimension[CountryValue]
Dimension[NetworkGeneration]
Dimension[CategoryValue]
```

공통 Dimension 상태는 구체적인 value 타입을 보존하는 `Dimension[ValueT]`로 공유할 수 있다.
실제 공통 동작이 없는 표식용 `DimensionValue` 인터페이스는 만들지 않는다. DB 테이블,
repository Protocol, API DTO는 타입별 경계를 유지한다. `DimensionCode`는 별도 값 객체이다.

## 8. 생성, 조회, 수정

### 8.1 생성

- 인증된 actor가 code, 타입별 value와 선택적 reason을 제공한다.
- 업무 필드는 모두 필수이며 `NULL`을 허용하지 않는다.
- 서버 생성 필드를 클라이언트가 지정할 수 없다.
- 생성 version은 `1`이다.
- code와 value 생성 로그가 같은 change set과 시각으로 각각 한 행씩 기록된다.
- 정규화된 code와 value가 기존 삭제 행과 충돌해도 `409`이다.

### 8.2 조회

- 일반 목록과 단건 조회는 `deleted_at IS NULL`인 행만 반환한다.
- 삭제된 행의 일반 단건 조회는 `404 DIMENSION_NOT_FOUND`이다.
- 일반 API에 `include_deleted` 옵션을 두지 않는다.
- 응답 본문에 version을 포함하고 단건 응답은 같은 version의 강한 ETag를 제공한다.
- 타입별 컬렉션 경로를 사용한다. 예: `/dimensions/years`, `/dimensions/memories`.

### 8.3 조건부 PATCH

- value와 code 중 하나 이상을 제공해야 한다.
- 둘 다 제공하면 같은 트랜잭션에서 원자적으로 바꾼다.
- Memory value를 제공할 때 `amount`와 `unit`은 함께 하나의 값으로 제공한다.
- 정규화 후 현재값과 같은 필드는 no-op이다.
- 모든 제공 필드가 no-op이면 UPDATE, version 증가, updated_at 변경, 로그 생성을 하지 않는다.
- code가 실제로 바뀔 때만 관련 MasterCode를 재합성한다.
- 실제 변경이 하나 이상이면 version을 정확히 한 번 증가하고 updated_at을 갱신한다.

## 9. 낙관적 검사, 행 잠금, 원자성

수정·삭제·복원은 다음 조건부 요청 계약을 사용한다.

- 응답 본문 version과 `ETag: "<version>"`은 항상 같다.
- 요청은 정확히 하나의 강한 `If-Match: "<version>"`을 제공한다.
- `If-Match: *`, 약한 ETag, 여러 ETag 목록은 거부한다.
- 헤더 누락은 `428 PRECONDITION_REQUIRED`이다.
- 문법 오류는 `400 INVALID_IF_MATCH`이다.
- 현재 version과 불일치는 `412 PRECONDITION_FAILED`이다.
- 존재하지 않거나 일반 경로에서 삭제된 행은 `404 DIMENSION_NOT_FOUND`이다.

version 검사는 사용자의 조회 이후 발생한 변경을 감지한다. `SELECT ... FOR UPDATE` 행 잠금은
현재 요청의 트랜잭션 동안 동시 변경을 직렬화한다. 둘을 함께 사용한다.

repository SQL은 기대 version을 조건으로 확인하고 실제 변경 시 `version + 1`과
`updated_at = CURRENT_TIMESTAMP`를 명시한다. DB BEFORE 트리거는 값을 자동 보정하지 않고
다음을 검증한다.

- `id`와 `created_at`이 바뀌지 않았다.
- 업무 상태가 실제로 바뀌면 version이 정확히 1 증가했다.
- 실제 변경 시 updated_at이 DB의 현재 트랜잭션 시각이다.
- 업무 상태가 바뀌지 않으면 version과 updated_at도 바뀌지 않았다.
- 신뢰된 actor 트랜잭션 컨텍스트가 존재한다.

규칙을 누락한 직접 SQL은 실패한다. repository는 `RETURNING`으로 최종 행을 받는다.

## 10. MasterCode 영향과 참조 무결성

- value 변경은 MasterCode 합성 코드를 바꾸지 않는다.
- code 변경은 영향을 받는 모든 MasterCode 재합성을 동기적으로 수행한다.
- Dimension code, version, updated_at과 모든 MasterCode 합성 코드는 한 DB 트랜잭션에서
  갱신한다.
- 한 건이라도 유일성 충돌이나 DB 오류가 발생하면 Dimension, DimensionLog, MasterCode 변경을
  모두 롤백한다.
- eventual consistency와 보상 트랜잭션을 사용하지 않는다.
- 정확한 합성 순서와 모든 Dimension·MasterCode가 공유할 잠금 순서는 MasterCode 계약 이슈
  #4에서 확정하며 구현 전에 재사용한다. 이 문서는 동기적 단일 트랜잭션 경계를 고정한다.
- concurrent task 사이에서 SQLAlchemy 세션을 공유하지 않는다.

FK는 `ON DELETE RESTRICT`, `ON UPDATE RESTRICT`를 사용한다. UUID PK는 변경하지 않는다.

## 11. 논리 삭제와 복원

### 11.1 논리 삭제

- 물리 DELETE를 지원하지 않으며 DB BEFORE DELETE 트리거가 거부한다.
- 삭제 명령은 타입별 `POST /dimensions/{collection}/{id}/delete`를 사용한다.
- `If-Match`와 인증된 actor가 필수이고 reason은 선택이다.
- Dimension 행을 잠그고 MasterCode 참조 존재 여부를 검사한다.
- 현재 계약에서 MasterCode는 삭제 수명주기가 없으므로 하나라도 참조하면
  `409 DIMENSION_IN_USE`이다.
- 참조가 없을 때만 deleted_at과 updated_at을 같은 DB 시각으로 설정하고 version을 증가한다.
- 삭제 로그 한 행은 `DELETED: false -> true`로 기록한다.

MasterCode 생성도 참조할 Dimension을 잠그고 활성 상태를 확인해야 한다. 이 규칙은 참조 확인과
동시 생성 사이의 경쟁 조건을 막는다.

### 11.2 복원

- 복원 명령은 타입별 `POST /dimensions/{collection}/{id}/restore`를 사용한다.
- 삭제된 기존 UUID, code와 value를 그대로 사용한다.
- deleted_at을 `NULL`로 바꾸고 updated_at과 version을 갱신한다.
- 복원 로그 한 행은 `DELETED: true -> false`로 기록한다.
- 일반 PATCH로 deleted_at을 바꿀 수 없다.
- 이미 활성인 행의 복원은 `409 DIMENSION_NOT_DELETED`이다.

## 12. DimensionLog 계약

### 12.1 테이블

| Dimension 테이블 | 로그 테이블 |
| --- | --- |
| `dimension_years` | `dimension_year_logs` |
| `dimension_memories` | `dimension_memory_logs` |
| `dimension_companies` | `dimension_company_logs` |
| `dimension_models` | `dimension_model_logs` |
| `dimension_brands` | `dimension_brand_logs` |
| `dimension_countries` | `dimension_country_logs` |
| `dimension_networks` | `dimension_network_logs` |
| `dimension_categories` | `dimension_category_logs` |

각 로그 테이블은 자기 Dimension 테이블만 참조한다. FK는 `ON DELETE RESTRICT`,
`ON UPDATE RESTRICT`이다. 로그에는 deleted_at을 두지 않으며 자동 삭제와 보존 만료가 없는
영구 append-only 기록이다.

### 12.2 공통 필드

| 필드 | PostgreSQL 타입 | nullable | 기본값·제약 |
| --- | --- | --- | --- |
| `id` | `UUID` | 아니요 | PK, `uuidv7()` |
| `dimension_id` | `UUID` | 아니요 | 해당 Dimension FK |
| `change_set_id` | `UUID` | 아니요 | 변경 요청 단위 UUIDv7 |
| `dimension_version` | `INTEGER` | 아니요 | 변경 완료 후 version, `>= 1` |
| `operation` | `VARCHAR(7)` | 아니요 | `CREATE`, `UPDATE`, `DELETE`, `RESTORE` |
| `field_name` | `VARCHAR(7)` | 아니요 | `CODE`, `VALUE`, `DELETED` |
| `old_value` | `JSONB` | 조건부 | operation 규칙 참조 |
| `new_value` | `JSONB` | 조건부 | operation 규칙 참조 |
| `reason` | `VARCHAR(500)` | 예 | 앞뒤 공백 제거, 빈 값은 `NULL` |
| `actor_type` | `VARCHAR(6)` | 아니요 | `USER`, `SYSTEM` |
| `actor_id` | `VARCHAR(255)` | 아니요 | 1~255자, 앞뒤 공백 없음 |
| `changed_at` | `TIMESTAMPTZ` | 아니요 | DB `CURRENT_TIMESTAMP` |

같은 변경에서 생성한 로그는 서로 다른 id를 갖지만 `change_set_id`, `dimension_version`,
`changed_at`, actor와 reason이 같다. `(change_set_id, field_name)`은 UNIQUE이다. code와 value를
함께 바꾸면 같은 change set의 로그가 정확히 두 행 생성된다.

reason은 요청 전체의 선택적 자연어 메타데이터이다. Unicode, 대소문자와 내부 일반 공백을
보존하고 앞뒤 공백만 제거한다. 빈 문자열과 공백 문자열은 `NULL`이 된다. 탭, 줄바꿈,
제어문자는 거부한다. actor는 요청 body에서 받지 않고 신뢰된 인증·실행 컨텍스트에서 얻는다.
익명 쓰기는 허용하지 않는다. `USER`와 `SYSTEM` 모두 안정적인 actor_id가 필수이다.

### 12.3 로그 생성 규칙

- 생성은 `CODE: null -> code`, `VALUE: null -> value` 로그 두 행을 만든다.
- value 변경은 VALUE 로그 한 행을 만든다.
- code 변경은 CODE 로그 한 행을 만든다.
- code와 value 동시 변경은 같은 change set과 시각의 로그 두 행을 만든다.
- 논리 삭제는 `DELETED: false -> true` 로그 한 행을 만든다.
- 복원은 `DELETED: true -> false` 로그 한 행을 만든다.
- no-op과 자동 기술 필드 변경에는 로그를 만들지 않는다.
- Memory value는 `amount`, `unit`, `capacity_mb`를 포함하는 JSON 객체 하나로 기록한다.

operation·field·전후 값 CHECK는 다음 조합만 허용한다.

| operation | field_name | old_value | new_value |
| --- | --- | --- | --- |
| CREATE | CODE 또는 VALUE | SQL `NULL` | 값 |
| UPDATE | CODE 또는 VALUE | 값 | 서로 다른 값 |
| DELETE | DELETED | JSON `false` | JSON `true` |
| RESTORE | DELETED | JSON `true` | JSON `false` |

각 로그 테이블은 JSONB 내부 타입도 원본 테이블에 맞게 CHECK한다.

- code는 정규형 JSON 문자열이다.
- Year VALUE는 2000~2999의 JSON 정수이다.
- Memory VALUE는 추가 키 없이 `amount`, `unit`, `capacity_mb`를 가진 JSON 객체이며 원본
  환산 규칙을 만족한다.
- 문자열 VALUE는 1~128자의 정규형 JSON 문자열이다.
- Network VALUE는 1~5의 JSON 정수이다.
- DELETED는 JSON boolean이다.

### 12.4 트리거와 원자성

Dimension BEFORE INSERT/UPDATE 트리거는 actor 컨텍스트와 쓰기 불변식을 검증한다. BEFORE
DELETE 트리거는 물리 삭제를 거부한다. AFTER INSERT/UPDATE 트리거는 OLD와 NEW를 비교하고
실제 변경 필드마다 로그를 삽입한다. 로그 테이블의 BEFORE UPDATE/DELETE 트리거는 로그 변조를
거부한다.

AFTER 트리거는 한 행 변경에 사용할 change_set_id를 한 번 생성한다. `CURRENT_TIMESTAMP`는
트랜잭션 동안 안정적이므로 같은 변경의 모든 로그와 Dimension updated_at이 같은 시각을 갖는다.
로그 생성이나 이후 MasterCode 재합성이 실패하면 전체 트랜잭션이 롤백된다.

현재 단계에서는 마이그레이션 소유자와 애플리케이션 DB 역할을 분리하지 않는다. 따라서 DB
계정이 직접 가짜 로그를 삽입하거나 소유자 권한으로 트리거를 우회할 수 있는 감사 변조 위험이
남는다. 정상 애플리케이션·배치 쓰기는 트리거를 우회할 수 없지만 tamper-proof 감사 저장소는
아니다. 역할 분리, 로그 DML 권한 회수와 `SECURITY DEFINER` 강화는 별도 보안 티켓에서 처리한다.

## 13. 애플리케이션과 DB 책임

| 규칙 | 도메인·애플리케이션 | PostgreSQL |
| --- | --- | --- |
| 입력 타입·정규화 | 엄격한 타입 검사, 자동 정규화, 한국어 오류 | 정규형 CHECK |
| 길이·범위 | 저장 전 검사 | CHECK와 물리 타입 |
| 유일성 | 충돌 사전 조회, 복수 오류 수집 | UNIQUE가 최종 보장 |
| Memory 환산 | `MemoryValue`에서 동일 규칙 사용 | 생성 컬럼과 UNIQUE |
| 낙관적 검사 | If-Match 해석과 기대 version 전달 | 조건부 쓰기·트리거 검증 |
| 행 잠금 | 유스케이스가 잠금 의도 결정 | `FOR UPDATE` 수행 |
| 시간·version | SQL에 DB 시각과 증가를 명시 | BEFORE 트리거가 정확성 검증 |
| 감사 주체·이유 | 신뢰된 컨텍스트 설정 | 트리거가 존재·형식 검증 |
| 로그 생성 | 직접 조립하지 않음 | AFTER 트리거가 필드별 생성 |
| 삭제·복원 | 유스케이스와 오류 결정 | CHECK, 트리거, FK 최종 보장 |

## 14. 오류 계약

모든 오류는 내부 SQL, 제약조건 이름, 기존 행 ID를 노출하지 않는 RFC 9457 Problem Details로
변환한다.

| 상황 | HTTP | 안정적인 code |
| --- | --- | --- |
| If-Match 문법 오류 | 400 | `INVALID_IF_MATCH` |
| 인증되지 않은 쓰기 | 401 | `AUTHENTICATION_REQUIRED` |
| 없는 또는 일반 경로에서 삭제된 Dimension | 404 | `DIMENSION_NOT_FOUND` |
| code 중복 | 409 | `DIMENSION_CODE_CONFLICT` |
| value 또는 동등 Memory 용량 중복 | 409 | `DIMENSION_VALUE_CONFLICT` |
| code와 value 모두 중복 | 409 | `DIMENSION_MULTIPLE_CONFLICTS` |
| 참조 중 삭제 | 409 | `DIMENSION_IN_USE` |
| 활성 행 복원 | 409 | `DIMENSION_NOT_DELETED` |
| If-Match 불일치 | 412 | `PRECONDITION_FAILED` |
| 타입·형식·길이·범위·필수값 위반 | 422 | `VALIDATION_ERROR` |
| If-Match 누락 | 428 | `PRECONDITION_REQUIRED` |

일반 경로에서는 code와 value 충돌을 함께 사전 조회해 두 violations를 반환한다. 동시 생성
경합에서 DB UNIQUE가 뒤늦게 발견한 경우에는 확인된 단일 충돌만 반환할 수 있다. 같은 기존
행과 code·value가 모두 같아도 생성 성공으로 취하지 않고 409를 반환한다.

잠금 타임아웃, 데드락이나 일시적인 DB 장애는 DB가 현재 트랜잭션 전체를 롤백한 뒤 내부 정보를
숨긴 일시적 서비스 오류로 변환한다. 자동 보상 트랜잭션은 실행하지 않는다.

## 15. 예시와 경계값

### 15.1 허용 및 정규화

| 타입·필드 | 입력 | 저장 결과 |
| --- | --- | --- |
| 공통 code | `"sam01"` | `"SAM01"` |
| Company value | `"  Samsung   Electronics  "` | `"SAMSUNG_ELECTRONICS"` |
| Model value | `"galaxy__s24___ultra"` | `"GALAXY_S24_ULTRA"` |
| Year value | `2000` | `2000` |
| Year value | `2999` | `2999` |
| Network value | `1` | `1` |
| Network value | `5` | `5` |
| Memory value | `{"amount": 128, "unit": "gb"}` | `128 GB`, `capacity_mb=128000` |

### 15.2 거부

| 입력 | 이유 |
| --- | --- |
| code `"A_B"` | code에는 `_`를 허용하지 않음 |
| code `"A-B"` | code에는 `-`를 허용하지 않음 |
| code `" ABC "` | code에는 공백을 허용하지 않음 |
| 문자열 value `"대한민국"` | 문자열 value는 ASCII 정규형만 허용 |
| 문자열 value `"A+B"` | 허용하지 않는 특수문자 |
| 문자열 value `"A\tB"` | 탭은 자동 정규화하지 않음 |
| Year `1999`, `3000` | 범위 밖 |
| Year `"2026"`, `true` | 엄격한 정수 타입 위반 |
| Network `0`, `6`, `"5G"` | 범위 또는 타입 위반 |
| Memory `amount=0` | 양수가 아님 |
| Memory `unit="GiB"` | 허용 단위가 아님 |
| 기존 `1 TB` 뒤 `1000 GB` 생성 | 동등 용량 중복 |

### 15.3 수정·감사

| 요청 | 결과 |
| --- | --- |
| 현재 `SAMSUNG`에 `samsung` PATCH | no-op, version/time/log 변화 없음 |
| code만 실제 변경 | version +1, CODE 로그 1개, MasterCode 재합성 |
| value만 실제 변경 | version +1, VALUE 로그 1개, MasterCode 변화 없음 |
| code와 value 실제 변경 | version +1, 같은 change set/time의 로그 2개 |
| stale If-Match | 412, Dimension·로그·MasterCode 변화 없음 |
| 참조 중 삭제 | 409, 삭제 로그 없음 |
| 삭제 성공 | version +1, DELETED false→true 로그 1개 |
| 복원 성공 | version +1, DELETED true→false 로그 1개 |

## 16. 인덱스 기준

초기 구현은 다음 최소 인덱스만 둔다.

Dimension 테이블:

- UUID PK
- code UNIQUE
- value UNIQUE 또는 Memory capacity_mb UNIQUE
- `(created_at, id) WHERE deleted_at IS NULL` 활성 목록 인덱스

DimensionLog 테이블:

- UUID PK
- `(change_set_id, field_name)` UNIQUE
- `(dimension_id, changed_at, id)` 변경 이력 인덱스

actor, operation, 기간 단독 인덱스는 관리자 로그 조회 티켓에서 실제 필터와 실행 계획을 확인한
후 추가한다.

## 17. 후속 구현 경계

기존 티켓은 이 계약에 맞게 보강한다.

- #4: 고정 Dimension 테이블 참조, MasterCode 합성·공유 잠금 규칙
- #5: 여덟 Dimension 테이블, 값 객체, 생성·조회, 생성 로그
- #7: If-Match 조건부 PATCH 기반, value 변경과 수정 로그
- #8: 같은 PATCH의 code 변경, value 동시 변경, MasterCode 원자적 재합성, 로그

별도 티켓은 다음 책임을 가진다.

- #16: 인증된 actor와 DB 트랜잭션 컨텍스트
- #17: Dimension 논리 삭제·복원과 로그
- #18: 관리자용 DimensionLog 조회 API
- #19: DB 역할 분리와 감사 로그 변조 방지 강화

일반 Dimension 응답에는 감사 로그를 포함하지 않는다. 로그 조회 API는 인증된 관리자 전용,
읽기 전용, 안정적 정렬과 커서 페이지네이션으로 별도 설계한다.
