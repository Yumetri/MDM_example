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
