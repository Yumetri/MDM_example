"""Index administrator audit lists by stable chronological keyset.

Revision ID: 20260915_0014
Revises: 20260915_0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0014"
down_revision: str | None = "20260915_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "dimension_company_logs",
    "dimension_model_logs",
    "dimension_brand_logs",
    "dimension_country_logs",
    "dimension_category_logs",
    "dimension_year_logs",
    "dimension_network_logs",
    "dimension_memory_logs",
    "master_code_logs",
)


def upgrade() -> None:
    for table in _TABLES:
        op.create_index(f"ix_{table}_changed_at_id", table, ["changed_at", "id"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_changed_at_id", table_name=table)
