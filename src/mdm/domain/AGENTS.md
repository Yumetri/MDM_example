# Domain Guidelines

## Canonical Contract

Read `docs/domain/dimension-data-contract.md` before changing Dimension or DimensionLog behavior.
That document is the source of truth for field types, normalization, lifecycle, concurrency, audit,
and database invariants. Do not weaken or reinterpret it in an implementation ticket.

## Modeling Rules

- Keep the domain independent of FastAPI, Pydantic, SQLAlchemy, settings, and HTTP concerns.
- Model the eight fixed Dimension types explicitly. Do not add a generic database `type` column or
  a catch-all Dimension table.
- Use immutable, type-specific value objects. Share common Dimension state through a generic
  `Dimension[ValueT]` only where it preserves the concrete value type.
- Keep `DimensionCode` separate from Dimension values. Do not derive one from the other.
- Put normalization and friendly invariant failures in domain value-object construction. Database
  constraints remain the final integrity boundary.

## State and Concurrency Rules

- Treat IDs and creation timestamps as immutable.
- Use version preconditions and row locking together; neither replaces the other.
- A normalized no-op changes neither `version` nor `updated_at` and emits no audit log.
- Apply code and value changes atomically when both are requested. Recompose affected MasterCodes
  only when the code actually changes, in the same transaction as the Dimension update.
- Use soft deletion and explicit restoration. Never physically delete a Dimension.
- Do not expose deleted Dimensions through ordinary reads or allow them in new MasterCode references.

## Audit Rules

- Every committed create, value change, code change, soft deletion, and restoration emits one
  append-only DimensionLog row per changed business field.
- Logs for one mutation share a change-set ID, timestamp, resulting Dimension version, actor, and
  optional reason. Never emit logs for automatic technical-field changes or no-ops.
- Obtain actor identity from trusted execution context, never from a client-controlled body field.
- Keep audit persistence and trigger code in `infrastructure/`; expose only domain concepts and ports
  inward.

## Verification Rules

- Test value objects without a database, including accepted, normalized, boundary, and rejected
  inputs from the canonical contract.
- Test every database CHECK, UNIQUE, FK, soft-delete, optimistic-concurrency, trigger, and audit
  invariant against PostgreSQL.
- Include concurrent integration tests for duplicate creation, conditional updates, deletion versus
  MasterCode creation, and code changes versus MasterCode creation.
