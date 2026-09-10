"""SQLAlchemy model registry and authentication persistence models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class whose metadata is used by Alembic."""


class UserRecord(Base):
    """Current HUMAN user identity and credential state."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
        CheckConstraint(
            "char_length(normalized_email) BETWEEN 3 AND 254",
            name="ck_users_normalized_email_length",
        ),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name="ck_users_name_length",
        ),
        CheckConstraint("char_length(password_hash) > 0", name="ck_users_password_hash_present"),
        CheckConstraint(
            "role IN ('USER', 'ADMIN', 'SUPER_ADMIN')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_users_status",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_users_timestamp_order"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    normalized_email: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(11), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class UserSecurityEventRecord(Base):
    """Append-only-by-application audit of permanent user security changes."""

    __tablename__ = "user_security_events"
    __table_args__ = (
        CheckConstraint(
            """
            ((
                    initiator_type = 'AUTHENTICATED_USER'
                    AND initiator_user_id IS NOT NULL
                    AND initiator_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                ) OR (
                    initiator_type IN ('RESET_TOKEN', 'BOOTSTRAP_CLI')
                    AND initiator_user_id IS NULL
                    AND initiator_role IS NULL
                )) IS TRUE
            """,
            name="ck_user_security_events_initiator",
        ),
        CheckConstraint(
            """
            ((
                    event_type = 'INITIAL_SUPER_ADMIN_BOOTSTRAPPED'
                    AND initiator_type = 'BOOTSTRAP_CLI'
                    AND previous_role IS NULL
                    AND new_role = 'SUPER_ADMIN'
                    AND previous_status IS NULL
                    AND new_status = 'ACTIVE'
                ) OR (
                    event_type = 'USER_ROLE_CHANGED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                    AND new_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                    AND previous_role <> new_role
                    AND previous_status IS NULL
                    AND new_status IS NULL
                ) OR (
                    event_type = 'USER_DISABLED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status = 'ACTIVE'
                    AND new_status = 'DISABLED'
                ) OR (
                    event_type = 'USER_ENABLED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status = 'DISABLED'
                    AND new_status = 'ACTIVE'
                ) OR (
                    event_type = 'PASSWORD_CHANGED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status IS NULL
                    AND new_status IS NULL
                ) OR (
                    event_type = 'PASSWORD_RESET'
                    AND initiator_type = 'RESET_TOKEN'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status IS NULL
                    AND new_status IS NULL
                )) IS TRUE
            """,
            name="ck_user_security_events_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    initiator_type: Mapped[str] = mapped_column(String(18), nullable=False)
    initiator_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
    )
    initiator_role: Mapped[str | None] = mapped_column(String(11))
    previous_role: Mapped[str | None] = mapped_column(String(11))
    new_role: Mapped[str | None] = mapped_column(String(11))
    previous_status: Mapped[str | None] = mapped_column(String(8))
    new_status: Mapped[str | None] = mapped_column(String(8))


class CompanyRecord(Base):
    """Current state of one Company Dimension."""

    __tablename__ = "dimension_companies"
    __table_args__ = (
        UniqueConstraint("code", name="uq_dimension_companies_code"),
        UniqueConstraint("value", name="uq_dimension_companies_value"),
        CheckConstraint("code ~ '^[A-Z0-9]{1,32}$'", name="ck_dimension_companies_code"),
        CheckConstraint("code !~ '^N+$'", name="ck_dimension_companies_code_reserved"),
        CheckConstraint(
            "value ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' AND char_length(value) <= 128",
            name="ck_dimension_companies_value",
        ),
        CheckConstraint("version >= 1", name="ck_dimension_companies_version"),
        CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name="ck_dimension_companies_timestamp_order",
        ),
        Index(
            "ix_dimension_companies_active_created_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompanyLogRecord(Base):
    """Append-only field audit record for one Company mutation."""

    __tablename__ = "dimension_company_logs"
    __table_args__ = (
        CheckConstraint("dimension_version >= 1", name="ck_dimension_company_logs_version"),
        CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')",
            name="ck_dimension_company_logs_operation",
        ),
        CheckConstraint(
            "field_name IN ('CODE', 'VALUE', 'DELETED')",
            name="ck_dimension_company_logs_field",
        ),
        CheckConstraint(
            "((operation = 'CREATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NULL AND new_value IS NOT NULL) OR "
            "(operation = 'UPDATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value <> new_value) OR "
            "(operation = 'DELETE' AND field_name = 'DELETED' "
            "AND old_value = 'false'::jsonb AND new_value = 'true'::jsonb) OR "
            "(operation = 'RESTORE' AND field_name = 'DELETED' "
            "AND old_value = 'true'::jsonb AND new_value = 'false'::jsonb)) IS TRUE",
            name="ck_dimension_company_logs_change_shape",
        ),
        CheckConstraint(
            "((field_name = 'CODE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (old_value #>> '{}') !~ '^N+$')) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (new_value #>> '{}') !~ '^N+$'))) OR "
            "(field_name = 'VALUE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(old_value #>> '{}') <= 128)) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(new_value #>> '{}') <= 128))) OR "
            "(field_name = 'DELETED' AND "
            "jsonb_typeof(old_value) = 'boolean' AND jsonb_typeof(new_value) = 'boolean'))",
            name="ck_dimension_company_logs_value_shape",
        ),
        CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name="ck_dimension_company_logs_reason",
        ),
        CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name="ck_dimension_company_logs_actor",
        ),
        CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name="ck_dimension_company_logs_actor_id",
        ),
        UniqueConstraint(
            "dimension_id",
            "change_set_id",
            "field_name",
            name="uq_dimension_company_logs_change_field",
        ),
        Index(
            "ix_dimension_company_logs_dimension_changed_id",
            "dimension_id",
            "changed_at",
            "id",
        ),
        Index(
            "ix_dimension_company_logs_change_dimension_id",
            "change_set_id",
            "dimension_id",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_companies.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    change_set_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    dimension_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(7), nullable=False)
    field_name: Mapped[str] = mapped_column(String(7), nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB)
    new_value: Mapped[object | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_kind: Mapped[str] = mapped_column(String(6), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(11))
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _string_dimension_record_constraints(plural: str) -> tuple[object, ...]:
    return (
        UniqueConstraint("code", name=f"uq_dimension_{plural}_code"),
        UniqueConstraint("value", name=f"uq_dimension_{plural}_value"),
        CheckConstraint("code ~ '^[A-Z0-9]{1,32}$'", name=f"ck_dimension_{plural}_code"),
        CheckConstraint("code !~ '^N+$'", name=f"ck_dimension_{plural}_code_reserved"),
        CheckConstraint(
            "value ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' AND char_length(value) <= 128",
            name=f"ck_dimension_{plural}_value",
        ),
        CheckConstraint("version >= 1", name=f"ck_dimension_{plural}_version"),
        CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name=f"ck_dimension_{plural}_timestamp_order",
        ),
        Index(
            f"ix_dimension_{plural}_active_created_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


def _string_dimension_log_constraints(singular: str) -> tuple[object, ...]:
    table = f"dimension_{singular}_logs"
    return (
        CheckConstraint("dimension_version >= 1", name=f"ck_{table}_version"),
        CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')",
            name=f"ck_{table}_operation",
        ),
        CheckConstraint(
            "field_name IN ('CODE', 'VALUE', 'DELETED')",
            name=f"ck_{table}_field",
        ),
        CheckConstraint(
            "((operation = 'CREATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NULL AND new_value IS NOT NULL) OR "
            "(operation = 'UPDATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value <> new_value) OR "
            "(operation = 'DELETE' AND field_name = 'DELETED' "
            "AND old_value = 'false'::jsonb AND new_value = 'true'::jsonb) OR "
            "(operation = 'RESTORE' AND field_name = 'DELETED' "
            "AND old_value = 'true'::jsonb AND new_value = 'false'::jsonb)) IS TRUE",
            name=f"ck_{table}_change_shape",
        ),
        CheckConstraint(
            "((field_name = 'CODE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (old_value #>> '{}') !~ '^N+$')) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (new_value #>> '{}') !~ '^N+$'))) OR "
            "(field_name = 'VALUE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(old_value #>> '{}') <= 128)) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(new_value #>> '{}') <= 128))) OR "
            "(field_name = 'DELETED' AND "
            "jsonb_typeof(old_value) = 'boolean' AND jsonb_typeof(new_value) = 'boolean'))",
            name=f"ck_{table}_value_shape",
        ),
        CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name=f"ck_{table}_reason",
        ),
        CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name=f"ck_{table}_actor",
        ),
        CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name=f"ck_{table}_actor_id",
        ),
        UniqueConstraint(
            "dimension_id",
            "change_set_id",
            "field_name",
            name=f"uq_{table}_change_field",
        ),
        Index(
            f"ix_{table}_dimension_changed_id",
            "dimension_id",
            "changed_at",
            "id",
        ),
        Index(
            f"ix_{table}_change_dimension_id",
            "change_set_id",
            "dimension_id",
            "id",
        ),
    )


class _StringDimensionRecordMixin:
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelRecord(_StringDimensionRecordMixin, Base):
    __tablename__ = "dimension_models"
    __table_args__ = _string_dimension_record_constraints("models")


class BrandRecord(_StringDimensionRecordMixin, Base):
    __tablename__ = "dimension_brands"
    __table_args__ = _string_dimension_record_constraints("brands")


class CountryRecord(_StringDimensionRecordMixin, Base):
    __tablename__ = "dimension_countries"
    __table_args__ = _string_dimension_record_constraints("countries")


class CategoryRecord(_StringDimensionRecordMixin, Base):
    __tablename__ = "dimension_categories"
    __table_args__ = _string_dimension_record_constraints("categories")


class _StringDimensionLogRecordMixin:
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    change_set_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    dimension_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(7), nullable=False)
    field_name: Mapped[str] = mapped_column(String(7), nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB)
    new_value: Mapped[object | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_kind: Mapped[str] = mapped_column(String(6), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(11))
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelLogRecord(_StringDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_model_logs"
    __table_args__ = _string_dimension_log_constraints("model")
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_models.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )


class BrandLogRecord(_StringDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_brand_logs"
    __table_args__ = _string_dimension_log_constraints("brand")
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_brands.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )


class CountryLogRecord(_StringDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_country_logs"
    __table_args__ = _string_dimension_log_constraints("country")
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_countries.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )


class CategoryLogRecord(_StringDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_category_logs"
    __table_args__ = _string_dimension_log_constraints("category")
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_categories.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )


def _numeric_dimension_record_constraints(
    plural: str, minimum: int, maximum: int
) -> tuple[object, ...]:
    return (
        UniqueConstraint("code", name=f"uq_dimension_{plural}_code"),
        UniqueConstraint("value", name=f"uq_dimension_{plural}_value"),
        CheckConstraint("code ~ '^[A-Z0-9]{1,32}$'", name=f"ck_dimension_{plural}_code"),
        CheckConstraint("code !~ '^N+$'", name=f"ck_dimension_{plural}_code_reserved"),
        CheckConstraint(
            f"value BETWEEN {minimum} AND {maximum}", name=f"ck_dimension_{plural}_value"
        ),
        CheckConstraint("version >= 1", name=f"ck_dimension_{plural}_version"),
        CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name=f"ck_dimension_{plural}_timestamp_order",
        ),
        Index(
            f"ix_dimension_{plural}_active_created_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


def _numeric_dimension_log_constraints(
    singular: str, minimum: int, maximum: int
) -> tuple[object, ...]:
    table = f"dimension_{singular}_logs"
    old_numeric = _numeric_json_constraint("old_value", minimum, maximum)
    new_numeric = _numeric_json_constraint("new_value", minimum, maximum)
    return (
        CheckConstraint("dimension_version >= 1", name=f"ck_{table}_version"),
        CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')",
            name=f"ck_{table}_operation",
        ),
        CheckConstraint("field_name IN ('CODE', 'VALUE', 'DELETED')", name=f"ck_{table}_field"),
        CheckConstraint(
            "((operation = 'CREATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NULL AND new_value IS NOT NULL) OR "
            "(operation = 'UPDATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value <> new_value) OR "
            "(operation = 'DELETE' AND field_name = 'DELETED' "
            "AND old_value = 'false'::jsonb AND new_value = 'true'::jsonb) OR "
            "(operation = 'RESTORE' AND field_name = 'DELETED' "
            "AND old_value = 'true'::jsonb AND new_value = 'false'::jsonb)) IS TRUE",
            name=f"ck_{table}_change_shape",
        ),
        CheckConstraint(
            "((field_name = 'CODE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (old_value #>> '{}') !~ '^N+$')) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (new_value #>> '{}') !~ '^N+$'))) OR "
            f"(field_name = 'VALUE' AND (old_value IS NULL OR ({old_numeric})) "
            f"AND (new_value IS NULL OR ({new_numeric}))) OR "
            "(field_name = 'DELETED' AND "
            "jsonb_typeof(old_value) = 'boolean' AND jsonb_typeof(new_value) = 'boolean'))",
            name=f"ck_{table}_value_shape",
        ),
        CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name=f"ck_{table}_reason",
        ),
        CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name=f"ck_{table}_actor",
        ),
        CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name=f"ck_{table}_actor_id",
        ),
        UniqueConstraint(
            "dimension_id",
            "change_set_id",
            "field_name",
            name=f"uq_{table}_change_field",
        ),
        Index(
            f"ix_{table}_dimension_changed_id",
            "dimension_id",
            "changed_at",
            "id",
        ),
        Index(
            f"ix_{table}_change_dimension_id",
            "change_set_id",
            "dimension_id",
            "id",
        ),
    )


def _numeric_json_constraint(value: str, minimum: int, maximum: int) -> str:
    return (
        f"jsonb_typeof({value}) = 'number' "
        f"AND ({value} #>> '{{}}') ~ '^[0-9]+$' "
        f"AND ({value} #>> '{{}}')::integer BETWEEN {minimum} AND {maximum}"
    )


class _NumericDimensionRecordMixin:
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("statement_timestamp()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class YearRecord(_NumericDimensionRecordMixin, Base):
    __tablename__ = "dimension_years"
    __table_args__ = _numeric_dimension_record_constraints("years", 2000, 2999)


class NetworkRecord(_NumericDimensionRecordMixin, Base):
    __tablename__ = "dimension_networks"
    __table_args__ = _numeric_dimension_record_constraints("networks", 1, 5)


class _NumericDimensionLogRecordMixin:
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )
    change_set_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    dimension_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(7), nullable=False)
    field_name: Mapped[str] = mapped_column(String(7), nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB)
    new_value: Mapped[object | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_kind: Mapped[str] = mapped_column(String(6), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(11))
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class YearLogRecord(_NumericDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_year_logs"
    __table_args__ = _numeric_dimension_log_constraints("year", 2000, 2999)
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_years.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )


class NetworkLogRecord(_NumericDimensionLogRecordMixin, Base):
    __tablename__ = "dimension_network_logs"
    __table_args__ = _numeric_dimension_log_constraints("network", 1, 5)
    dimension_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("dimension_networks.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
