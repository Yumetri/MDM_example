# MDM API

FastAPI service for master data whose denormalized codes are derived from referenced dimensions.

## Local development

Requirements: Python 3.13, `uv`, Docker, and Docker Compose.

```bash
make setup
make jwt-key-local
make db-up
make migrate
make dev
```

최초 `SUPER_ADMIN`은 migration이나 HTTP endpoint가 아니라 명시적인 대화형 CLI로 생성한다.

```bash
uv run mdm auth bootstrap-super-admin
```

CLI가 email과 name을 차례로 묻고 password와 password 확인값은 각각 terminal no-echo prompt로
입력받는다. 두 password가 NFC 정규화 후 일치하지 않으면 hash 생성이나 DB 접근 없이
`BOOTSTRAP_PASSWORD_CONFIRMATION_MISMATCH`를 출력하고 종료 코드 `2`로 끝난다. 운영에서는 항상
같은 email을 사용해야 한다. 같은 email의 `ACTIVE + SUPER_ADMIN`이 같은 password로 이미 존재하면
아무 행도 변경하지 않고 성공하며, 상태·역할·password가 다르면 `BOOTSTRAP_STATE_CONFLICT`로
종료한다. 이 명령은 강등·비활성화된 관리자의 복구 수단이 아니다. 종료 코드는 성공 `0`, bootstrap
상태·실행 실패 `1`, 명령 사용·입력값·대화형 입력 환경 오류 `2`, `Ctrl+C` 취소 `130`을 사용한다.

`make jwt-key-local`은 최초 한 번만 실행하며 `secrets/local/`에 `0600` RS256 private PEM과
public-only JWKS를 생성한다. 기존 파일은 덮어쓰지 않는다. 이 디렉터리는 Git과 Docker build
context에서 제외된다. JWT adapter는 `.env`의 `MDM_AUTH_JWT_PRIVATE_KEY_PATH`와
`MDM_AUTH_JWT_JWKS_PATH`를 통해 파일을 읽으며 PEM 원문을 환경변수에 저장하지 않는다.

배포에서는 private PEM을 container image에 포함하지 않는다. Cloud Run은 Google Cloud Secret
Manager의 특정 secret version을 읽기 전용 파일로 마운트하고, 동일한 path 설정을 사용한다.
`MDM_AUTH_JWT_ACTIVE_KID`와 public JWKS에는 비밀값이 없으며 새 public key 배포, active signer
전환, 930초 후 retired public key 제거 순서로 회전한다.

Interactive API documentation is rendered by Scalar at `http://127.0.0.1:8000/docs`. The OpenAPI
JSON document remains available at `http://127.0.0.1:8000/openapi.json`; FastAPI's default Swagger
UI and ReDoc pages are disabled.

`make setup` installs the repository's pre-commit hook. Every commit runs `make ci-check`, the same
clean-database acceptance gate used by GitHub Actions. Run `make check` when you want the faster
version that reuses the development database. See `AGENTS.md` for architecture and contribution
rules.
