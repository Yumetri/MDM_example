# MDM API

FastAPI service for master data whose denormalized codes are derived from referenced dimensions.

## Local development

Requirements: Python 3.13, `uv`, Docker, and Docker Compose.

```bash
make setup
make db-up
make migrate
make dev
```

The API documentation is available at `http://127.0.0.1:8000/docs`. Run `make check` before
opening a pull request. See `AGENTS.md` for architecture and contribution rules.
