"""Release 6.0.5 password-recovery persistence kernel.

Revision ID: 0014_password_recovery
Revises: 0013_purchase_receipts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_password_recovery"
down_revision: str | None = "0013_purchase_receipts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SHA256_CHECK = (
    "length(token_hash) = 64 AND token_hash = lower(token_hash) AND "
    "length(replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "token_hash,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),"
    "'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')) = 0"
)


def upgrade() -> None:
    op.create_table(
        "password_recovery_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_password_recovery_expires_after_created",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR invalidated_at IS NULL",
            name="ck_password_recovery_terminal_state_exclusive",
        ),
        sa.CheckConstraint(
            _SHA256_CHECK,
            name="ck_password_recovery_token_hash_sha256",
        ),
    )
    op.create_index(
        "uq_password_recovery_token_hash",
        "password_recovery_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_password_recovery_tokens_expires_at",
        "password_recovery_tokens",
        ["expires_at"],
    )
    op.create_index(
        "uq_password_recovery_outstanding_per_user",
        "password_recovery_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL AND invalidated_at IS NULL"),
        sqlite_where=sa.text("consumed_at IS NULL AND invalidated_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_password_recovery_outstanding_per_user",
        table_name="password_recovery_tokens",
    )
    op.drop_index(
        "ix_password_recovery_tokens_expires_at",
        table_name="password_recovery_tokens",
    )
    op.drop_index(
        "uq_password_recovery_token_hash",
        table_name="password_recovery_tokens",
    )
    op.drop_table("password_recovery_tokens")
