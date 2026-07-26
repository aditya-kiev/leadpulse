import uuid
from datetime import datetime

from sqlalchemy import String, Text, Float, Boolean, DateTime, Enum as SAEnum, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database.session import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    plan_tier: Mapped[str] = mapped_column(String(50), nullable=False, default="starter")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    brand_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    custom_domain_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unverified")
    domain_verification_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tls_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users = relationship("User", back_populates="organization")
    crm_configs = relationship("CRMConfig", back_populates="organization")
    conversations = relationship("LeadConversation", back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="agent")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")


class CRMConfig(Base):
    __tablename__ = "crm_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = relationship("Organization", back_populates="crm_configs")


class LeadConversation(Base):
    __tablename__ = "lead_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    lead_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualification_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    lead_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lead_intent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lead_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    booking_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    meeting_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conversation_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    conversation_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    current_node: Mapped[str | None] = mapped_column(String(50), nullable=True)
    human_escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = relationship("Organization", back_populates="conversations")


class PushLog(Base):
    __tablename__ = "push_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    lead_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DailyOrgSummary(Base):
    __tablename__ = "daily_org_summaries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    total_conversations: Mapped[int] = mapped_column(default=0)
    qualified_leads: Mapped[int] = mapped_column(default=0)
    hot_leads: Mapped[int] = mapped_column(default=0)
    warm_leads: Mapped[int] = mapped_column(default=0)
    cold_leads: Mapped[int] = mapped_column(default=0)
    meetings_booked: Mapped[int] = mapped_column(default=0)
    human_escalations: Mapped[int] = mapped_column(default=0)
    gemini_calls: Mapped[int] = mapped_column(default=0)
    total_prompt_tokens: Mapped[int] = mapped_column(default=0)
    total_completion_tokens: Mapped[int] = mapped_column(default=0)
    total_cost: Mapped[float] = mapped_column(default=0.0)
    avg_qualification_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
