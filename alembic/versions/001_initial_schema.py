"""baseline: current schema after all Phase 1-5 work

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-07-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, index=True, unique=True),
        sa.Column("plan_tier", sa.String(50), nullable=False, server_default="starter"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("brand_name", sa.String(255), nullable=True),
        sa.Column("logo_url", sa.String(1024), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=True),
        sa.Column("custom_domain", sa.String(255), nullable=True),
        sa.Column("custom_domain_status", sa.String(20), nullable=False, server_default="unverified"),
        sa.Column("domain_verification_token", sa.String(64), nullable=True),
        sa.Column("tls_status", sa.String(20), nullable=False, server_default="none"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "users",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("email", sa.String(255), nullable=False, index=True, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), nullable=False, server_default="agent"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "crm_configs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("integration_type", sa.String(50), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "lead_conversations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("session_id", sa.String(255), nullable=False, index=True, unique=True),
        sa.Column("lead_name", sa.String(255), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=True),
        sa.Column("industry", sa.String(255), nullable=True),
        sa.Column("budget", sa.Float(), nullable=True),
        sa.Column("timeline", sa.String(255), nullable=True),
        sa.Column("problem_statement", sa.Text(), nullable=True),
        sa.Column("qualification_score", sa.Float(), nullable=True),
        sa.Column("lead_status", sa.String(50), nullable=True),
        sa.Column("lead_intent", sa.String(50), nullable=True),
        sa.Column("lead_type", sa.String(50), nullable=True),
        sa.Column("booking_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("meeting_time", sa.DateTime(), nullable=True),
        sa.Column("conversation_history", sa.JSON(), nullable=True),
        sa.Column("conversation_stage", sa.String(50), nullable=True),
        sa.Column("current_node", sa.String(50), nullable=True),
        sa.Column("human_escalated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "push_logs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("integration_type", sa.String(50), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("lead_data", sa.JSON(), nullable=True),
        sa.Column("response_data", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "usage_logs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "daily_org_summaries",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("date", sa.String(10), nullable=False),
        sa.Column("total_conversations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("qualified_leads", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("hot_leads", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("warm_leads", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cold_leads", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("meetings_booked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("human_escalations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("gemini_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_cost", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("avg_qualification_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("daily_org_summaries")
    op.drop_table("usage_logs")
    op.drop_table("push_logs")
    op.drop_table("lead_conversations")
    op.drop_table("crm_configs")
    op.drop_table("users")
    op.drop_table("organizations")
