DEV_DATABASE_URL ?= postgresql+asyncpg://mdm:mdm-local@127.0.0.1:55432/mdm
TEST_DATABASE_URL ?= postgresql+asyncpg://mdm:mdm-local@127.0.0.1:55432/mdm_test
CI_COMPOSE_PROJECT ?= mdm-example-ci-check
CI_POSTGRES_PORT ?= 55433
CI_TEST_DATABASE_URL ?= postgresql+asyncpg://mdm:mdm-local@127.0.0.1:$(CI_POSTGRES_PORT)/mdm_test

.PHONY: setup db-up db-ready db-down migrate migrate-test dev format lint typecheck \
	architecture test-unit test-integration test openapi openapi-check lock-check check \
	ci-check install-hooks

setup: install-hooks
	@test -f .env || cp .env.example .env
	uv sync --locked

install-hooks:
	git config core.hooksPath .githooks

db-up:
	docker compose up -d db
	$(MAKE) db-ready

db-ready:
	@for attempt in $$(seq 1 30); do \
		docker compose exec -T db sh -c \
			'PGPASSWORD="$${POSTGRES_PASSWORD}" psql -h 127.0.0.1 -U "$${POSTGRES_USER}" -d mdm_test -tAc "SELECT 1"' \
			2>/dev/null | grep -qx 1 && exit 0; \
		sleep 1; \
	done; \
	echo "PostgreSQL did not become ready" >&2; exit 1

db-down:
	docker compose down

migrate:
	MDM_DATABASE_URL=$(DEV_DATABASE_URL) uv run alembic upgrade head

migrate-test:
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

dev:
	MDM_DATABASE_URL=$(DEV_DATABASE_URL) uv run fastapi dev src/mdm/main.py

format:
	uv run ruff check --fix .
	uv run ruff format .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run pyrefly check

architecture:
	uv run lint-imports

test-unit:
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest -m unit

test-integration: db-up migrate-test
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest -m "integration or api"

test: db-up migrate-test
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

openapi:
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run python scripts/export_openapi.py

openapi-check:
	MDM_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest tests/api/test_openapi_contract.py

lock-check:
	uv lock --check

check: lock-check lint typecheck architecture test openapi openapi-check

ci-check:
	@docker compose -p "$(CI_COMPOSE_PROJECT)" down -v --remove-orphans >/dev/null 2>&1 || true
	@set -eu; \
		trap 'docker compose -p "$(CI_COMPOSE_PROJECT)" down -v --remove-orphans >/dev/null 2>&1' EXIT; \
		COMPOSE_PROJECT_NAME="$(CI_COMPOSE_PROJECT)" \
		MDM_POSTGRES_PORT="$(CI_POSTGRES_PORT)" \
		$(MAKE) check TEST_DATABASE_URL="$(CI_TEST_DATABASE_URL)"
