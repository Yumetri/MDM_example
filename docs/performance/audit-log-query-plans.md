# #18 감사 로그 조회 인덱스 근거

## 측정 조건과 한계

2026-09-15, PostgreSQL 18.4 aarch64 Alpine 로컬 Docker에서 측정했다.
`scripts/explain_audit_logs.py`는 `mdm_test`에서만 실행한다. 통합 테스트가 만든 승인 로그를
기초로 각 로그 테이블에 자료형·DB 제약을 만족하는 합성 이력 10,000건씩을 추가한다.
이는 동일 대상의 반복 이력에 치우친 실행 계획 실험이며 실제 운영 데이터의 분포나 SLA를
대표하지 않는다. 단일 로컬 측정이므로 경과 시간보다 읽은 행과 계획 형태를 선택 근거로 삼는다.

스크립트는 실험 대상인 `ix_<table>_changed_at_id`를 트랜잭션 안에서 제거한 뒤 기존 인덱스만
있는 상태를 측정한다. 이어 새 인덱스를 만들어 같은 쿼리를 측정한다. fixture와 인덱스 변경은
끝에 롤백하므로 마이그레이션 적용 후에도 전후 비교를 재현할 수 있다.

## 쿼리와 결과

개별 목록은 `(changed_at DESC, id DESC)`로 정렬하고 `limit + 1`행을 요청한다.
다음 페이지에서는 `(changed_at, id) < (:changed_at, :id)` 조건을 사용한다.
아래 값은 필터 없는 첫 페이지 `limit=50`의 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 결과다.

| 로그 | 기존 인덱스만 사용 (ms) | `(changed_at, id)` 추가 (ms) |
| --- | ---: | ---: |
| Company | 3.528 | 0.055 |
| Model | 5.523 | 0.043 |
| Brand | 4.640 | 0.045 |
| Country | 3.615 | 0.039 |
| Category | 3.613 | 0.037 |
| Year | 3.743 | 0.042 |
| Network | 3.722 | 0.038 |
| Memory | 3.779 | 0.036 |
| MasterCode | 4.686 | 0.061 |

모든 개별 목록에서 기존 `Seq Scan → Sort → Limit`이 새 인덱스의
`Index Scan Backward → Limit`으로 바뀌었다. Company의 경우 읽은 행은 10,004건에서 51건으로,
shared hit blocks는 233개에서 2개로 줄었다. 기간 조건과 다음 페이지 조건도 같은 선두 정렬
키를 사용한다. 전체 목록에 대상 ID가 없는 경우를 기존 `(dimension_id, changed_at, id)`와
`(master_code_id, changed_at, id)` 인덱스만으로 처리하기 어려워 9개 로그 테이블 모두에
`ix_<table>_changed_at_id`를 추가했다. 쓰기마다 인덱스 유지 비용과 저장 공간이 추가된다.

change set 통합 조회는 9개 테이블을 한 `UNION ALL` 문장으로 읽는다. 각 분기는 동일한
`change_set_id` 조건과 정렬·`limit + 1`을 사용한다. 합친 결과에
`changed_at DESC, source_kind ASC, source_type ASC, id DESC`를 적용하며, 출처 문자열은
`C` collation으로 정렬한다. 같은 시각의 출처 순서를 커서 조건에도 반영한다.

승인 한 건의 17개 로그를 조회하는 계획은 전후 모두 기존 change-set 선두 인덱스 9개를
사용했고 shared hit blocks는 각각 27개였다. 경과 시간은 0.341ms와 1.716ms로 차이가 났지만
스캔 경로·읽은 행·버퍼 수의 증가는 없었다. 이 결과를 통합 조회의 속도 개선으로 주장하지
않으며, change set 전용 인덱스를 추가하지 않는다. actor·operation 조합 인덱스도 이번
측정으로 필요성이 확인되지 않아 추가하지 않는다.

## 재현

```sh
make setup
make jwt-key-local  # 로컬 키가 없을 때만 실행
make db-up
make migrate-test
MDM_DATABASE_URL=postgresql+asyncpg://mdm:mdm-local@127.0.0.1:55432/mdm_test \
  uv run pytest 'tests/integration/test_audit_log_queries.py::test_approved_change_set_pages_include_every_typed_log_once[50]'
MDM_DATABASE_URL=postgresql+asyncpg://mdm:mdm-local@127.0.0.1:55432/mdm_test \
  uv run python scripts/explain_audit_logs.py
```

세부 계획은 ignored 파일 `.artifacts/audit-query-plans.json`에 저장한다. 테스트는 테스트 DB의
업무 데이터를 초기화하므로 다른 테스트와 동시에 실행하지 않는다.
