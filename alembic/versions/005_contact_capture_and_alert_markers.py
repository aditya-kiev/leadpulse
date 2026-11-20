"""lead contact capture + idempotency markers for alerts / CRM sync

Revision ID: 005_contact_alert_markers
Revises: 004_password_reset_tokens
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_contact_alert_markers"
down_revision: Union[str, None] = "004_password_reset_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "lead_conversations"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("lead_email", sa.String(255), nullable=True))
    op.add_column(_TABLE, sa.Column("lead_phone", sa.String(32), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("contact_declined", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(_TABLE, sa.Column("hot_alert_sent_at", sa.DateTime(), nullable=True))
    op.add_column(_TABLE, sa.Column("booking_alert_sent_at", sa.DateTime(), nullable=True))
    op.add_column(_TABLE, sa.Column("handoff_alert_sent_at", sa.DateTime(), nullable=True))
    op.add_column(_TABLE, sa.Column("crm_sync_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    for col in (
        "crm_sync_hash", "handoff_alert_sent_at", "booking_alert_sent_at",
        "hot_alert_sent_at", "contact_declined", "lead_phone", "lead_email",
    ):
        op.drop_column(_TABLE, col)
