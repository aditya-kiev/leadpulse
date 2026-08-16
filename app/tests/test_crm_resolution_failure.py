"""REAL Postgres regression tests: a CRM integration *resolution* failure
must never crash the conversation turn.

update_crm() used to call ``await resolve_integration(tenant_id)`` outside any
try/except, so a DB outage / encryption-key-missing / unexpected row state
raised straight up through the graph node and the end_conversation reply was
never delivered.  Now resolution failures are caught, logged to PushLog with
status="resolution_failed", and update_crm returns a failed dict instead of
raising.
"""

import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.tools.crm import update_crm
from app.database.models import CRMConfig, Organization, PushLog
from app.integrations.encryption import encrypt_json

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)

_ENC_KEY = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


async def _seed_tenant(factory, *, slug: str) -> str:
    async with factory() as session:
        org = Organization(name=f"ResolveOrg {slug}", slug=slug)
        session.add(org)
        await session.flush()
        org_id = str(org.id)
        with patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY):
            session.add(CRMConfig(
                organization_id=org.id,
                integration_type="gemini",
                config=encrypt_json({"api_key": "tenant-key"}, tenant_id=org.id),
                is_active=True,
            ))
        await session.commit()
    return org_id


async def _cleanup_org(factory, org_id):
    async with factory() as session:
        await session.execute(delete(PushLog).where(PushLog.organization_id == org_id))
        await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_update_crm_resolution_failure_does_not_raise_and_logs_push(pg_session_factory):
    """resolve_integration raising must degrade to a logged failed push, not crash."""
    org_id = None
    try:
        org_id = await _seed_tenant(pg_session_factory, slug=f"resfail-{uuid4().hex[:8]}")

        async def _boom(tenant_id):
            raise RuntimeError("simulated resolution outage")

        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.database.crud.async_session_factory", pg_session_factory), \
             patch("app.agent.tools.crm.resolve_integration", side_effect=_boom):
            result = await update_crm(
                session_id=f"sess-{uuid4().hex[:8]}",
                lead_data={"lead_name": "Resolve Fail", "lead_status": "hot"},
                tenant_id=org_id,
            )

        assert result == {"status": "failed", "error": "resolution_failed"}

        async with pg_session_factory() as session:
            rows = list((await session.execute(
                select(PushLog).where(
                    PushLog.organization_id == org_id,
                    PushLog.status == "resolution_failed",
                )
            )).scalars().all())
        assert len(rows) == 1, "a resolution_failed PushLog row must be persisted"
        assert "simulated resolution outage" in rows[0].error_message
        assert rows[0].integration_type == "unknown"
    finally:
        if org_id:
            await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_end_conversation_still_delivers_reply_when_resolution_fails(pg_session_factory):
    """The full end_conversation node must still return its final message
    even when CRM resolution explodes."""
    from app.agent.nodes.end_conversation import create_end_conversation_node

    org_id = None
    try:
        org_id = await _seed_tenant(pg_session_factory, slug=f"resnode-{uuid4().hex[:8]}")
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = type("Resp", (), {"content": "We'll be in touch!"})()

        node = create_end_conversation_node(mock_model)

        async def _boom(tenant_id):
            raise RuntimeError("simulated resolution outage")

        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.database.crud.async_session_factory", pg_session_factory), \
             patch("app.agent.tools.crm.resolve_integration", side_effect=_boom), \
             patch("app.agent.nodes.end_conversation.notify_tenant", new_callable=AsyncMock):
            out = await node({
                "session_id": f"sess-{uuid4().hex[:8]}",
                "tenant_id": org_id,
                "messages": [HumanMessage(content="Goodbye")],
                "lead_name": "Resolve Node",
                "company_name": "ACME",
                "lead_status": "hot",
                "booking_confirmed": False,
                "human_escalated": False,
            })

        assert out["messages"][0].content == "We'll be in touch!"
        assert out["current_node"] == "end"
    finally:
        if org_id:
            await _cleanup_org(pg_session_factory, org_id)
