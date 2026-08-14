"""add organizations.widget_key and organizations.notification_phone

Revision ID: 002_widget_key_notif
Revises: 001_initial_schema
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_widget_key_notif"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("widget_key", sa.String(128), nullable=True))
    op.create_index("ix_organizations_widget_key", "organizations", ["widget_key"], unique=True)
    op.add_column("organizations", sa.Column("notification_phone", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_organizations_widget_key", table_name="organizations")
    op.drop_column("organizations", "widget_key")
    op.drop_column("organizations", "notification_phone")
