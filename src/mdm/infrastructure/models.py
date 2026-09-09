"""SQLAlchemy model registry and authentication persistence models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, text
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
