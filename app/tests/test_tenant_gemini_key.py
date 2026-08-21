"""REAL Postgres regression tests for per-tenant Gemini API keys.

build_graph() used to hardcode ``api_key=settings.gemini_api_key``, so every
tenant shared the platform key and the per-tenant key that onboard_client
stores in ``crm_configs`` was never used.  Now run_agent resolves the
tenant's own key and get_graph caches per-tenant graphs.
"""

import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.graph import get_graph, resolve_tenant_gemini_key
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


async def _seed_tenant(factory, *, slug: str, api_key: str) -> str:
    async with factory() as session:
        org = Organization(name=f"KeyOrg {slug}", slug=slug)
        session.add(org)
        await session.flush()
        org_id = str(org.id)
        with patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY):
            session.add(CRMConfig(
                organization_id=org.id,
                integration_type="gemini",
                config=encrypt_json({"api_key": api_key}, tenant_id=org.id),
                is_active=True,
            ))
        await session.commit()
    return org_id


@pytest.mark.asyncio
async def test_resolve_tenant_gemini_key_uses_per_tenant_keys(pg_session_factory):
    """Each org resolves to its OWN stored Gemini key, not the platform key."""
    have_own = None
    try:
        have_own = await _seed_tenant(
            pg_session_factory, slug=f"gk-own-{uuid4().hex[:8]}", api_key="tenant-key-alpha"
        )
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch("app.config.settings.settings.gemini_api_key", "platform-key"):
            resolved = await resolve_tenant_gemini_key(have_own)
        assert resolved == "tenant-key-alpha", (
            f"tenant must use its own key, got {resolved!r}"
        )
    finally:
        if have_own:
            await _cleanup_org(pg_session_factory, have_own)


@pytest.mark.asyncio
async def test_resolve_tenant_gemini_key_falls_back_to_platform_key(pg_session_factory):
    """An org with no stored gemini config falls back to settings.gemini_api_key."""
    have_own = None
    try:
        have_own = await _seed_tenant(
            pg_session_factory, slug=f"gk-none-{uuid4().hex[:8]}", api_key="not-used"
        )
        async with pg_session_factory() as session:
            await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == have_own))
            await session.commit()
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch("app.config.settings.settings.gemini_api_key", "platform-key"):
            resolved = await resolve_tenant_gemini_key(have_own)
        assert resolved == "platform-key"
    finally:
        if have_own:
            await _cleanup_org(pg_session_factory, have_own)


@pytest.mark.asyncio
async def test_get_graph_uses_tenant_key_and_caches_per_tenant(pg_session_factory):
    """build_graph must be called with the tenant's own key, and graphs must
    be cached per tenant so org A's key never leaks into org B's model."""
    from app.agent import graph as graph_mod
    from app.agent.state import AgentState

    org_a = org_b = None
    try:
        org_a = await _seed_tenant(
            pg_session_factory, slug=f"gk-a-{uuid4().hex[:8]}", api_key="key-a"
        )
        org_b = await _seed_tenant(
            pg_session_factory, slug=f"gk-b-{uuid4().hex[:8]}", api_key="key-b"
        )

        # Record the api_key each build_graph would use (conftest already
        # mocks ChatGoogleGenerativeAI to a MagicMock, so graph building is safe).
        build_api_keys: list = []

        original_build = graph_mod.build_graph

        def spying_build(api_key=None, tenant_id=None):
            build_api_keys.append(api_key)
            return original_build(api_key=api_key, tenant_id=tenant_id)

        with patch.object(graph_mod, "build_graph", side_effect=spying_build), \
             patch("app.agent.graph._tenant_graph_keys", {}), \
             patch("app.agent.graph._tenant_graphs", {}):
            g1 = get_graph(org_a, api_key="key-a")
            g2 = get_graph(org_b, api_key="key-b")
            g1_again = get_graph(org_a, api_key="key-a")

        assert build_api_keys == ["key-a", "key-b"], build_api_keys
        assert g1 is g1_again, "per-tenant graph must be cached (not rebuilt)"
    finally:
        if org_a:
            await _cleanup_org(pg_session_factory, org_a)
        if org_b:
            await _cleanup_org(pg_session_factory, org_b)


async def _cleanup_org(factory, org_id):
    async with factory() as session:
        await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_end_to_end_each_org_uses_own_gemini_key(pg_session_factory):
    """End-to-end: sending a message through each org's context must build
    its agent graph with THAT org's stored key (org A never runs on org B's
    or the platform key).

    Regression: build_graph hardcoded ``api_key=settings.gemini_api_key``,
    so per-tenant billing/keys in crm_configs were completely ignored."""
    from unittest.mock import AsyncMock

    from app.agent import graph as graph_mod
    from app.agent.graph import run_agent
    from app.services.memory import memory_service

    org_a = org_b = None
    used_keys: list[str] = []
    try:
        org_a = await _seed_tenant(
            pg_session_factory, slug=f"gk-e2e-a-{uuid4().hex[:8]}", api_key="key-a"
        )
        org_b = await _seed_tenant(
            pg_session_factory, slug=f"gk-e2e-b-{uuid4().hex[:8]}", api_key="key-b"
        )

        def recording_model(**kwargs):
            used_keys.append(kwargs.get("api_key"))
            mock_model = MagicMock()
            mock_model.ainvoke = AsyncMock(
                return_value=MagicMock(content="This is a mock response.")
            )
            return mock_model

        with patch.object(graph_mod, "ChatGoogleGenerativeAI", side_effect=recording_model), \
             patch.object(graph_mod, "RetryingGeminiModel", side_effect=lambda model, **kwargs: model), \
             patch.object(graph_mod, "_agent_graph", None), \
             patch.object(graph_mod, "_tenant_graphs", {}), \
             patch.object(graph_mod, "_tenant_graph_keys", {}), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch.object(memory_service, "load_state", new_callable=AsyncMock) as mock_load, \
             patch.object(graph_mod, "get_last_usage", return_value=None):
            mock_load.return_value = None
            await run_agent(f"e2e-{uuid4().hex[:8]}", "Hello", tenant_id=org_a)
            await run_agent(f"e2e-{uuid4().hex[:8]}", "Hello", tenant_id=org_b)

        assert used_keys == ["key-a", "key-b"], (
            f"each org must build its graph with its own key, got {used_keys!r}"
        )
    finally:
        if org_a:
            await _cleanup_org(pg_session_factory, org_a)
        if org_b:
            await _cleanup_org(pg_session_factory, org_b)