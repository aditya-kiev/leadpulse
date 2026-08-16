"""REAL Postgres regression tests: per-tenant vertical/business_name are live.

onboard_client() stores the tenant's ``vertical`` and ``business_name`` in the
``gemini`` crm_configs row together with the API key.  resolve_tenant_gemini_key
used to read only ``api_key``, so lead scoring and prompt selection always ran
on the process-global ``settings.vertical``/``settings.business_name`` — every
tenant on the box shared the platform's vertical even when onboarded with
``--vertical real_estate``.  Now run_agent resolves the full tenant config and
threads it into state so get_prompts() and compute_lead_score() are
per-tenant.
"""

import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.graph import resolve_tenant_config
from app.database.models import CRMConfig, Organization
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


async def _seed_tenant(factory, *, slug: str, vertical: str, business_name: str) -> str:
    async with factory() as session:
        org = Organization(name=f"ConfigOrg {slug}", slug=slug)
        session.add(org)
        await session.flush()
        org_id = str(org.id)
        with patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY):
            session.add(CRMConfig(
                organization_id=org.id,
                integration_type="gemini",
                config=encrypt_json({
                    "api_key": f"key-{slug}",
                    "vertical": vertical,
                    "business_name": business_name,
                }, tenant_id=org.id),
                is_active=True,
            ))
        await session.commit()
    return org_id


async def _cleanup_org(factory, org_id):
    async with factory() as session:
        await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_resolve_tenant_config_returns_full_row(pg_session_factory):
    """resolve_tenant_config must return vertical + business_name, not just api_key."""
    org_id = None
    try:
        org_id = await _seed_tenant(
            pg_session_factory,
            slug=f"cfg-full-{uuid4().hex[:8]}",
            vertical="real_estate",
            business_name="Bella Vista Realty",
        )
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY):
            cfg = await resolve_tenant_config(org_id)
        assert cfg.get("api_key") == f"key-cfg-full-{org_id}" or cfg.get("api_key").startswith("key-cfg-full-"), cfg
        assert cfg.get("vertical") == "real_estate", cfg
        assert cfg.get("business_name") == "Bella Vista Realty", cfg
    finally:
        if org_id:
            await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_qualification_scores_two_tenants_with_own_verticals(pg_session_factory):
    """Two orgs in the SAME process with different verticals must score the
    same lead differently: real_estate $750k is HOT-band, insurance $150/mo is
    HOT-band — but a $100 budget is HOT for insurance yet COLD for real_estate.

    Regression: qualification_node hardcoded ``vertical=settings.vertical``,
    so both orgs scored with the platform's global vertical regardless of the
    tenant's stored vertical."""
    from app.agent.nodes.qualification import create_qualification_node
    from unittest.mock import AsyncMock, MagicMock

    org_re = org_ins = None
    try:
        org_re = await _seed_tenant(
            pg_session_factory, slug=f"q-re-{uuid4().hex[:8]}",
            vertical="real_estate", business_name="Bella Vista Realty",
        )
        org_ins = await _seed_tenant(
            pg_session_factory, slug=f"q-ins-{uuid4().hex[:8]}",
            vertical="insurance", business_name="Acme Insurance",
        )

        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="QUALIFICATION: strong lead")
        node = create_qualification_node(mock_model)

        async def _run(org_id):
            return await node({
                "session_id": f"sess-{uuid4().hex[:8]}",
                "tenant_id": org_id,
                "vertical": "real_estate" if org_id == org_re else "insurance",
                "business_name": "Bella Vista Realty" if org_id == org_re else "Acme Insurance",
                "lead_name": "Alice",
                "company_name": "Acme LLC",
                "industry": "Real Estate" if org_id == org_re else "Insurance",
                "budget": 100,
                "timeline": "ASAP",
                "problem_statement": "We need a new policy and a thorough comparison of options and pricing across providers.",
                "lead_intent": "purchase",
                "lead_type": "individual" if org_id == org_ins else "company",
                "messages": [],
                "conversation_history": [
                    {"role": "user", "content": "Hi, I'm interested."},
                    {"role": "assistant", "content": "Great, tell me more."},
                ],
                "qualification_score": None,
                "lead_status": None,
                "current_node": "qualification",
                "next_action": None,
                "conversation_stage": "collecting",
            })

        out_re = await _run(org_re)
        out_ins = await _run(org_ins)

        # $100 budget: insurance HOT band (>= $150? no — 100 < 150, so insurance
        # gives 0.20 from the 75+ band); real_estate gives 0.05 (below 200k).
        score_re = out_re["qualification_score"]
        score_ins = out_ins["qualification_score"]
        assert score_ins > score_re, (
            f"insurance $100 budget must outscore real_estate $100 budget: "
            f"ins={score_ins} re={score_re}"
        )
    finally:
        if org_re:
            await _cleanup_org(pg_session_factory, org_re)
        if org_ins:
            await _cleanup_org(pg_session_factory, org_ins)


@pytest.mark.asyncio
async def test_run_agent_threads_tenant_vertical_into_state(pg_session_factory):
    """End-to-end: run_agent must inject the tenant's stored vertical/business_name
    into turn_input so downstream nodes see it."""
    from app.agent import graph as graph_mod
    from unittest.mock import AsyncMock

    org_id = None
    try:
        org_id = await _seed_tenant(
            pg_session_factory, slug=f"cfg-thread-{uuid4().hex[:8]}",
            vertical="insurance", business_name="Acme Insurance",
        )
        captured = {}

        class FakeGraph:
            checkpointer = None

            async def ainvoke(self, state, config=None):
                captured["vertical"] = state.get("vertical")
                captured["business_name"] = state.get("business_name")
                captured["tenant_id"] = state.get("tenant_id")
                return {**state, "conversation_stage": "greeting"}

        fake = FakeGraph()

        with patch.object(graph_mod, "get_graph", return_value=fake), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch.object(graph_mod.memory_service, "load_state", new_callable=AsyncMock) as mock_load, \
             patch.object(graph_mod, "get_last_usage", return_value=None):
            mock_load.return_value = None
            await graph_mod.run_agent(f"sess-{uuid4().hex[:8]}", "Hello", tenant_id=org_id)

        assert captured.get("vertical") == "insurance", captured
        assert captured.get("business_name") == "Acme Insurance", captured
    finally:
        if org_id:
            await _cleanup_org(pg_session_factory, org_id)
