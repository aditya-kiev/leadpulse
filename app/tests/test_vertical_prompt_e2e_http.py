"""FIX 1 + FIX 2a regression: per-tenant vertical prompts over real HTTP.

Two REAL Postgres tenants, each with its own ``gemini`` crm_configs row
carrying ``api_key``, ``vertical`` and ``business_name``, talk through the
real HTTP layer (widget-key auth → POST /webhook/start).  Each request must:

1. build the tenant's graph with THAT tenant's stored api_key,
2. stamp the graph's rate limiter with THAT tenant's id,
3. render prompts with THAT tenant's business_name (never the other
   tenant's, never the platform defaults),
4. cache under the tenant's own slot while the platform singleton stays
   untouched — a stale ``_agent_graph`` must never be served to a tenant.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_tenant(factory, *, slug, api_key, vertical, business_name):
    async with factory() as session:
        org = Organization(
            name=business_name,
            slug=slug,
            brand_name=business_name,
            logo_url="",
            primary_color="#000000",
            custom_domain="",
            custom_domain_status="unverified",
            tls_status="none",
            domain_verification_token=None,
            widget_key=f"wk-{slug}",
        )
        session.add(org)
        await session.flush()
        org_id = org.id
        with patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY):
            session.add(CRMConfig(
                organization_id=org_id,
                integration_type="gemini",
                config=encrypt_json(
                    {
                        "api_key": api_key,
                        "vertical": vertical,
                        "business_name": business_name,
                    },
                    tenant_id=org_id,
                ),
                is_active=True,
            ))
        await session.commit()
    return org_id


async def _cleanup_org(factory, org_id):
    async with factory() as session:
        await session.execute(
            text("DELETE FROM lead_conversations WHERE tenant_id = :tid"), {"tid": org_id}
        )
        await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_each_tenant_gets_own_key_vertical_and_cached_graph(client, pg_session_factory):
    from app.agent import graph as graph_mod
    from app.services.memory import memory_service

    slug_a = f"vpe-a-{uuid4().hex[:8]}"
    slug_b = f"vpe-b-{uuid4().hex[:8]}"
    biz_a = f"Aurora Realty {uuid4().hex[:6]}"
    biz_b = f"Sentinel Insurance {uuid4().hex[:6]}"
    key_a, key_b = f"key-re-a-{uuid4().hex[:6]}", f"key-ins-b-{uuid4().hex[:6]}"
    org_a = org_b = None
    try:
        org_a = await _seed_tenant(
            pg_session_factory, slug=slug_a, api_key=key_a,
            vertical="real_estate", business_name=biz_a,
        )
        org_b = await _seed_tenant(
            pg_session_factory, slug=slug_b, api_key=key_b,
            vertical="insurance", business_name=biz_b,
        )

        built_keys: list = []
        built_tenant_ids: list = []
        prompt_corpus: dict = {}

        def recording_model(**kwargs):
            built_keys.append(kwargs.get("api_key"))
            mock_model = MagicMock()

            async def _ainvoke(*a, **k):
                prompt_corpus.setdefault(kwargs.get("api_key"), []).append(str(a) + str(k))
                resp = MagicMock()
                resp.content = "This is a mock response from the AI assistant."
                return resp

            mock_model.ainvoke = _ainvoke
            return mock_model

        def passthrough_retry(model, **kwargs):
            built_tenant_ids.append(kwargs.get("tenant_id"))
            return model

        with patch.object(graph_mod, "ChatGoogleGenerativeAI", side_effect=recording_model), \
             patch.object(graph_mod, "RetryingGeminiModel", side_effect=passthrough_retry), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.memory.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.webhook_rpm_limit", 0), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch.object(memory_service, "load_state", new_callable=AsyncMock, return_value=None):
            resp_a = await client.post(
                "/webhook/start", json={"channel": "web"},
                headers={"X-Widget-Key": f"wk-{slug_a}"},
            )
            resp_b = await client.post(
                "/webhook/start", json={"channel": "web"},
                headers={"X-Widget-Key": f"wk-{slug_b}"},
            )

        assert resp_a.status_code == 200, resp_a.text
        assert resp_b.status_code == 200, resp_b.text

        # 1. each tenant's graph built with ITS OWN stored key
        assert built_keys == [key_a, key_b], built_keys

        # 2. each graph's rate limiter stamped with the right tenant id
        assert built_tenant_ids == [org_a, org_b], built_tenant_ids

        # 3. prompts personalized per tenant — and never cross-contaminated
        corpus_a = "\n".join(prompt_corpus.get(key_a, []))
        corpus_b = "\n".join(prompt_corpus.get(key_b, []))
        assert biz_a in corpus_a, "tenant A's business_name missing from its own prompts"
        assert biz_b not in corpus_a, "tenant B's business_name leaked into A's prompts"
        assert biz_b in corpus_b, "tenant B's business_name missing from its own prompts"
        assert biz_a not in corpus_b, "tenant A's business_name leaked into B's prompts"

        # 4. two distinct cached tenant graphs; platform singleton untouched
        assert graph_mod._agent_graph is None, "tenant traffic must not build the platform singleton"
        assert set(graph_mod._tenant_graphs) == {str(org_a), str(org_b)}
        assert graph_mod._tenant_graphs[str(org_a)] is not graph_mod._tenant_graphs[str(org_b)]
    finally:
        if org_a:
            await _cleanup_org(pg_session_factory, org_a)
        if org_b:
            await _cleanup_org(pg_session_factory, org_b)


@pytest.mark.asyncio
async def test_stale_platform_singleton_never_served_to_tenant(client, pg_session_factory):
    """Regression for the audit's cache-poisoning scenario: a platform graph
    cached BEFORE the tenant existed must not be served to tenant traffic —
    the tenant gets its own freshly-built graph instead."""
    from app.agent import graph as graph_mod
    from app.services.memory import memory_service

    slug = f"vpe-stale-{uuid4().hex[:8]}"
    biz = f"Poison Probe {uuid4().hex[:6]}"
    key = f"key-stale-{uuid4().hex[:6]}"
    org_id = None
    try:
        org_id = await _seed_tenant(
            pg_session_factory, slug=slug, api_key=key,
            vertical="real_estate", business_name=biz,
        )

        built_keys: list = []

        def recording_model(**kwargs):
            built_keys.append(kwargs.get("api_key"))
            mock_model = MagicMock()

            async def _ainvoke(*a, **k):
                resp = MagicMock()
                resp.content = "This is a mock response from the AI assistant."
                return resp

            mock_model.ainvoke = _ainvoke
            return mock_model

        # Poison the platform singleton; conftest's autouse fixture has
        # already cleared the per-tenant caches for this test.
        graph_mod._agent_graph = object()

        with patch.object(graph_mod, "ChatGoogleGenerativeAI", side_effect=recording_model), \
             patch.object(graph_mod, "RetryingGeminiModel", side_effect=lambda model, **kw: model), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.memory.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.webhook_rpm_limit", 0), \
             patch("app.config.settings.settings.crm_encryption_key", _ENC_KEY), \
             patch.object(memory_service, "load_state", new_callable=AsyncMock, return_value=None):
            resp = await client.post(
                "/webhook/start", json={"channel": "web"},
                headers={"X-Widget-Key": f"wk-{slug}"},
            )

        assert resp.status_code == 200, resp.text
        # The poisoned singleton was NOT used: a fresh graph was built with
        # the tenant's OWN key.
        assert built_keys == [key], built_keys
        assert str(org_id) in graph_mod._tenant_graphs
    finally:
        if org_id:
            await _cleanup_org(pg_session_factory, org_id)
