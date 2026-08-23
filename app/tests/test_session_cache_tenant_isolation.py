"""CRITICAL cross-tenant regression: Redis session cache must be tenant-scoped.

cache_session_state/get_cached_session_state/invalidate_session_cache used to
key on session_id ALONE (``f"session:{session_id}"``), and memory.load_state()
returned a cache hit WITHOUT any tenant verification -- only the Postgres
fallback filtered by tenant. session_id is client-supplied via /webhook/start
and /webhook/message, so any tenant could read another tenant's cached lead
PII (name, budget, financial details, full transcript) within the 300s TTL by
supplying a colliding session_id.

These tests run against REAL Redis and REAL Postgres and prove the boundary
specifically on the CACHE-HIT path: state is seeded directly into Redis so a
DB round-trip cannot mask the bug.
"""

import json
import os
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.redis as redis_mod
from app.agent import graph as graph_mod
from app.main import app
from app.services.memory import memory_service
from app.services.redis import (
    cache_session_state,
    get_cached_session_state,
    invalidate_session_cache,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)

TENANT_A = UUID("a1111111-1111-1111-1111-111111111111")
TENANT_B = UUID("b2222222-2222-2222-2222-222222222222")
TENANT_C = UUID("c3333333-3333-3333-3333-333333333333")


@pytest.fixture(autouse=True)
async def _real_redis():
    """Force a fresh REAL Redis client (settings.redis_url) for every test."""
    redis_mod._redis = None
    yield
    redis_mod._redis = None


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _pii_state(marker: str) -> dict:
    """A 'juicy' cached lead state; every value carries the unique marker."""
    return {
        "lead_name": f"{marker}-lead",
        "company_name": f"{marker}-corp",
        "budget": 987654.0 if marker.startswith("A") else 12.5,
        "timeline": "ASAP",
        "problem_statement": f"{marker}-financial-problem",
        "conversation_history": [
            {"role": "user", "content": f"{marker}-user-msg"},
            {"role": "assistant", "content": f"{marker}-secret-transcript"},
        ],
        "lead_status": "hot",
        "conversation_stage": "collecting",
        "current_node": "info_collection",
        "qualification_score": 91.0,
    }


def _markers(marker: str) -> list[str]:
    return [
        f"{marker}-lead",
        f"{marker}-corp",
        f"{marker}-user-msg",
        f"{marker}-secret-transcript",
        f"{marker}-financial-problem",
    ]


async def _purge_session_keys(session_id: str) -> None:
    r = await redis_mod.get_redis()
    if r is None:
        return
    keys = [k for k in await r.keys("session:*") if session_id in k]
    if keys:
        await r.delete(*set(keys))


# ── Direct cache-boundary tests (cache-hit path, seeded by hand) ──────────


@pytest.mark.asyncio
async def test_same_session_id_two_tenants_are_isolated_in_cache():
    """The SAME session_id string cached by tenants A and B must give each
    tenant ONLY its own state -- never the other tenant's."""
    sid = f"iso-{uuid4().hex[:12]}"
    try:
        await cache_session_state(sid, _pii_state(f"A{sid[-6:]}"), tenant_id=TENANT_A)
        await cache_session_state(sid, _pii_state(f"B{sid[-6:]}"), tenant_id=TENANT_B)

        got_a = await get_cached_session_state(sid, tenant_id=TENANT_A)
        got_b = await get_cached_session_state(sid, tenant_id=TENANT_B)

        assert got_a is not None and got_b is not None
        assert got_a["lead_name"].startswith("A"), got_a["lead_name"]
        assert got_b["lead_name"].startswith("B"), got_b["lead_name"]
        assert got_a["budget"] != got_b["budget"]

        # A third tenant with the same session_id gets NOTHING from the cache
        assert await get_cached_session_state(sid, tenant_id=TENANT_C) is None
    finally:
        await _purge_session_keys(sid)


@pytest.mark.asyncio
async def test_cross_tenant_read_returns_none_not_foreign_data():
    """Only tenant A cached session X -> tenant B's lookup must be a MISS
    (None), never tenant A's data. Invalidation is scoped too."""
    sid = f"xread-{uuid4().hex[:12]}"
    try:
        await cache_session_state(sid, _pii_state(f"A{sid[-6:]}"), tenant_id=TENANT_A)

        foreign = await get_cached_session_state(sid, tenant_id=TENANT_B)
        assert foreign is None, f"tenant B read tenant A's cache entry: {foreign}"

        own = await get_cached_session_state(sid, tenant_id=TENANT_A)
        assert own is not None and own["lead_name"].startswith("A")

        # A invalidates THEIR entry only; B's stays intact
        await cache_session_state(sid, _pii_state(f"B{sid[-6:]}"), tenant_id=TENANT_B)
        await invalidate_session_cache(sid, tenant_id=TENANT_A)
        assert await get_cached_session_state(sid, tenant_id=TENANT_A) is None
        assert await get_cached_session_state(sid, tenant_id=TENANT_B) is not None
    finally:
        await _purge_session_keys(sid)


@pytest.mark.asyncio
async def test_none_tenant_key_is_stable_and_isolated():
    """Legacy/no-tenant traffic (tenant_id=None) uses an unambiguous 'none'
    segment and cannot collide with a real tenant's key."""
    sid = f"none-{uuid4().hex[:12]}"
    try:
        await cache_session_state(sid, _pii_state("PLAT"), tenant_id=None)

        legacy = await get_cached_session_state(sid, tenant_id=None)
        assert legacy is not None and legacy["lead_name"].startswith("PLAT")

        assert await get_cached_session_state(sid, tenant_id=TENANT_A) is None

        await invalidate_session_cache(sid, tenant_id=None)
        assert await get_cached_session_state(sid, tenant_id=None) is None
    finally:
        await _purge_session_keys(sid)


# ── Service-level boundary on the exact load_state() production path ─────


@pytest.mark.asyncio
async def test_load_state_cache_hit_never_crosses_tenants(pg_session_factory):
    """memory.load_state(session, tenant_id=B) on a CACHE HIT seeded under
    tenant A must return None/B-data. This was the exact leak: the old code
    returned A's state without checking tenant_id at all."""
    sid = f"svc-{uuid4().hex[:12]}"
    marker_a = f"A{uuid4().hex[:8]}"
    try:
        await cache_session_state(sid, _pii_state(marker_a), tenant_id=TENANT_A)

        with patch("app.services.memory.async_session_factory", pg_session_factory):
            leaked = await memory_service.load_state(sid, tenant_id=TENANT_B)
        assert leaked is None, (
            f"CROSS-TENANT LEAK: tenant B got tenant A's cached state {leaked}"
        )

        with patch("app.services.memory.async_session_factory", pg_session_factory):
            own = await memory_service.load_state(sid, tenant_id=TENANT_A)
        assert own is not None and own["lead_name"] == f"{marker_a}-lead"
    finally:
        await _purge_session_keys(sid)


@pytest.mark.asyncio
async def test_stale_unscoped_legacy_entry_never_served_cross_tenant(pg_session_factory):
    """A PRE-FIX-format cache entry -- the unscoped ``session:{session_id}``
    key the vulnerable build wrote, which can linger in Redis for up to
    ttl=300s after the fix deploys -- must NEVER be served across a tenant
    boundary by memory.load_state(). Seeded via RAW Redis (not the cache
    API) so this test fails on vulnerable code with a genuine LEAK
    assertion rather than a signature TypeError."""
    sid = f"legacy-{uuid4().hex[:12]}"
    marker_a = f"A{uuid4().hex[:8]}"
    r = await redis_mod.get_redis()
    assert r is not None, "these tests require REAL Redis"
    try:
        await r.setex(
            f"session:{sid}", 300, json.dumps(_pii_state(marker_a), default=str)
        )

        with patch("app.services.memory.async_session_factory", pg_session_factory):
            leaked = await memory_service.load_state(sid, tenant_id=TENANT_B)
        assert leaked is None, (
            f"CROSS-TENANT LEAK: stale unscoped cache entry handed to "
            f"tenant B: {leaked}"
        )

        # ...and no real tenant can read the legacy bucket either.
        with patch("app.services.memory.async_session_factory", pg_session_factory):
            assert await memory_service.load_state(sid, tenant_id=TENANT_C) is None
    finally:
        await _purge_session_keys(sid)


# ── Webhook-level attack reproduction ────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_attack_widget_key_cannot_read_foreign_cached_session(
    client, pg_session_factory
):
    """Full attack path over real HTTP + real widget-key auth:

    Tenant A caches lead PII under session X (the 300s TTL window).
    Tenant B -- with their own VALID widget key -- POSTs /webhook/message
    supplying session_id X.  Tenant B must NOT receive any of A's lead data,
    and the production load_state() inside run_agent must not hand back A's
    state either.
    """
    from app.database.models import Organization
    from sqlalchemy import delete, text as sa_text

    slug_a = f"wattack-a-{uuid4().hex[:8]}"
    slug_b = f"wattack-b-{uuid4().hex[:8]}"
    wk_a, wk_b = f"wk-a-{uuid4().hex[:10]}", f"wk-b-{uuid4().hex[:10]}"
    sid = f"attack-{uuid4().hex[:12]}"
    marker_a = f"A{uuid4().hex[:8]}"
    org_a = org_b = None
    captured_loads: list[tuple[str, object]] = []

    original_load = memory_service.load_state

    def fake_model(**kwargs):
        mock_model = MagicMock()

        async def _ainvoke(*a, **k):
            resp = MagicMock()
            resp.content = "Hello! How can I help you today?"
            return resp

        mock_model.ainvoke = _ainvoke
        return mock_model

    async def spying_load(session_id, tenant_id=None):
        result = await original_load(session_id, tenant_id=tenant_id)
        if session_id == sid:
            captured_loads.append((str(tenant_id), result))
        return result

    try:
        async with pg_session_factory() as session:
            org_a = Organization(
                name="Attack Org A", slug=slug_a, brand_name="Org A", logo_url="",
                primary_color="#000000", custom_domain="", custom_domain_status="unverified",
                tls_status="none", domain_verification_token=None, widget_key=wk_a,
            )
            org_b = Organization(
                name="Attack Org B", slug=slug_b, brand_name="Org B", logo_url="",
                primary_color="#111111", custom_domain="", custom_domain_status="unverified",
                tls_status="none", domain_verification_token=None, widget_key=wk_b,
            )
            session.add_all([org_a, org_b])
            await session.flush()
            org_a_id, org_b_id = org_a.id, org_b.id
            await session.commit()

        # Tenant A caches their lead state under session X (direct cache seed:
        # proves the boundary on the CACHE-HIT path specifically).
        await cache_session_state(sid, _pii_state(marker_a), tenant_id=org_a_id)

        with patch.object(memory_service, "load_state", spying_load), \
             patch.object(graph_mod, "ChatGoogleGenerativeAI", side_effect=fake_model), \
             patch.object(graph_mod, "RetryingGeminiModel", side_effect=lambda model, **kw: model), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.memory.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.webhook_rpm_limit", 0):
            resp = await client.post(
                "/webhook/message",
                json={"session_id": sid, "message": "Hi there!"},
                headers={"X-Widget-Key": wk_b},
            )

        assert resp.status_code == 200, resp.text
        body = resp.text
        for pii in _markers(marker_a):
            assert pii not in body, (
                f"CROSS-TENANT LEAK over /webhook/message: response contains "
                f"tenant A's data ({pii!r}): {body}"
            )

        # Whatever production load_state() returned for THIS session under
        # tenant B must not carry A's PII either.
        b_loads = [r for t, r in captured_loads if t == str(org_b_id)]
        assert b_loads, "expected at least one load_state call for tenant B"
        for result in b_loads:
            if result is None:
                continue
            blob = str(result)
            for pii in _markers(marker_a):
                assert pii not in blob, (
                    f"load_state handed tenant A's state to tenant B: {blob}"
                )
    finally:
        await _purge_session_keys(sid)
        async with pg_session_factory() as session:
            if org_a_id or org_b_id:
                if org_a_id:
                    await session.execute(sa_text(
                        "DELETE FROM lead_conversations WHERE tenant_id = :t"), {"t": org_a_id})
                    await session.execute(delete(Organization).where(Organization.id == org_a_id))
                if org_b_id:
                    await session.execute(sa_text(
                        "DELETE FROM lead_conversations WHERE tenant_id = :t"), {"t": org_b_id})
                    await session.execute(delete(Organization).where(Organization.id == org_b_id))
                await session.commit()
