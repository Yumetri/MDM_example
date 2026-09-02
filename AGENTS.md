# Repository Guidelines

## Project Purpose

This service manages master data built from Dimensions. MasterCode stores their foreign keys and a
denormalized composite code. Dimension changes update affected codes. Reads show current values; do
not implement SCD Type 2 without requirements.

## Technology and Runtime

Use Python 3.13, FastAPI, Pydantic, PostgreSQL 18, async SQLAlchemy, asyncpg, and Alembic. Use `uv`
and Docker Compose. Quality tools are pytest, Hypothesis, Ruff, Pyrefly, and Import Linter.

## Architecture Rules

Use `src/mdm/{domain,application,api,infrastructure}`. Dependencies flow from `api` or
`infrastructure` to `application` to `domain`; `api` and `infrastructure` cannot import each other.
Domain imports no framework, HTTP, or configuration code. Use cases depend on repository
`Protocol`s, never `AsyncSession`. Keep SQLAlchemy code in `infrastructure/`. Routers only handle
validation, authentication, serialization, and use-case calls. Restrict `Depends` to API/composition
boundaries. Keep `main.py` to assembly and registration. Never share sessions across concurrent tasks.

## Required Commands

Use `make setup`, `make db-up`, `make migrate`, and `make dev` locally. Apply `make format`; run
targeted `make lint`, `make typecheck`, `make architecture`, `make test-unit`,
`make test-integration`, or `make openapi-check`. `make check` is the fast local acceptance gate.
`make ci-check` is the clean-database gate shared by the pre-commit hook and CI.

## TDD and Testing

For behavior changes, observe a targeted failing pytest before implementation. Configuration,
documentation, and behavior-preserving refactors are exempt. Use Hypothesis for meaningful input
spaces or invariants, not token coverage. Domain/application tests need no database. Test persistence
on `mdm_test` and APIs with HTTPX `AsyncClient`. Name tests `test_*.py` and `test_<behavior>`. Never
weaken, delete, or skip checks to pass CI.

## FastAPI and OpenAPI Rules

Use `async def` only for awaited I/O and never block its event loop. Separate Pydantic DTOs from DB
models. Every endpoint needs a stable `operation_id`, tag, `response_model`, explicit status,
summary, and user-facing English description. Describe public fields and examples. Document errors
in `responses` with RFC 9457 Problem Details. Translate failures without leaking internals.

After OpenAPI changes, run `make openapi`. Give only `.artifacts/openapi.json` and the review rubric
to a fresh subagent without conversation context. Check typos, omissions, internal terminology,
unclear wording, and response/example consistency. Apply valid findings, then rerun
`make openapi-check` and `make check`. Never commit the JSON.

## Security and Configuration

Use typed settings only. Never commit `.env`, credentials, or real data; maintain `.env.example`.

## Completion and Failure Reporting

Finish code changes with `make ci-check`. Fix failures or report the blocker and unverified scope.

## Commit and Pull Request Workflow

Before every agent-authored commit, stage all intended changes and require the working tree to have
no unstaged tracked files or non-ignored untracked files. Review the complete staged diff with the
`$review-agent` skill in read-only mode. If the skill is unavailable, report a blocker instead of
skipping review. Do not commit while the review has actionable findings: fix them, restage, and
repeat the review until it reports `No findings.` Commit only after that result and a successful
pre-commit `make ci-check` run.

After pushing a new commit that addresses feedback on an existing pull request, wait for the
required `quality` check to pass. Reply to every addressed review thread with a concise summary and
the fixing commit SHA, and resolve a thread only when its feedback is fully addressed. Compare the
pull request head SHA with the commit from the latest Codex review. If they differ and no Codex
review is pending, post exactly one separate top-level `@codex review` comment for that head SHA.
Never request more than one review for the same head commit. Do not post this manual request for a
newly opened pull request, description-only edits, or comment-only updates. If the new review has
actionable findings, repeat the fix, validation, reply, and rereview cycle.

## Code Review Rules

Verify router purity, composition-root scope, nonblocking async I/O, explicit transactions,
use-case-aligned errors, and consumer-oriented OpenAPI.
