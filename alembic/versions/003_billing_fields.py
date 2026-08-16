"""add billing/subscription fields to organizations

Revision ID: 003_billing_fields
Revises: 002_widget_key_notif
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_billing_fields"
down_revision: Union[str, None] = "002_widget_key_notif"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("billing_status", sa.String(20), nullable=False, server_default="trialing"),
    )
    op.add_column(
        "organizations",
        sa.Column("billing_provider_customer_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_organizations_billing_provider_customer_id",
        "organizations",
        ["billing_provider_customer_id"],
        unique=False,
    )
    op.add_column("organizations", sa.Column("last_payment_at", sa.DateTime(), nullable=True))
    op.add_column("organizations", sa.Column("next_payment_due_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "next_payment_due_at")
    op.drop_column("organizations", "last_payment_at")
    op.drop_index("ix_organizations_billing_provider_customer_id", table_name="organizations")
    op.drop_column("organizations", "billing_provider_customer_id")
    op.drop_column("organizations", "billing_status")
