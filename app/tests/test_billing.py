"""REAL Postgres tests for Task 1: billing/subscription tracking.

Covers:
  - defaults on a new Organization row
  - ``is_billing_current`` across all five statuses
  - real-PG service mutations (mark_paid / past_due / suspend / reactivate)
  - CLI ``scripts/manage_billing.py`` mutations on a real row
  - ``--list-overdue`` with seeded due dates
  - Stripe webhook: signature verification (valid vs forged), event mapping,
    and 404 when STRIPE_ENABLED is off.

The billing router is mounted in app/main.py only when STRIPE_ENABLED is true,
so the webhook is tested through a dedicated app that includes the router and
overrides the DB session with the real Postgres test factory.
"""

import hmac
import hashlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)

WH_SECRET = "whsec_test_secret_0123456789abcdef"


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


async def _seed_org(factory, *, slug: str, **overrides) -> str:
    async with factory() as session:
        org = org_kwargs(slug, **overrides)
        session.add(org)
        await session.flush()
        org_id = str(org.id)
        await session.commit()
    return org_id


def org_kwargs(slug: str, **overrides) -> "object":
    from app.database.models import Organization

    defaults = dict(name=f"Billing Org {slug}", slug=slug)
    defaults.update(overrides)
    return Organization(**defaults)


async def _cleanup_org(factory, org_id: str):
    if not org_id:
        return
    from app.database.models import Organization

    async with factory() as session:
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


def _signed_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header exactly as Stripe does:
    HMAC-SHA256(secret, f"{t}.{payload}") as hex, prefixed by t=.
    """
    t = timestamp if timestamp is not None else int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


# ── Model defaults ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_org_defaults_to_trialing(pg_session_factory):
    org_id = None
    try:
        org_id = await _seed_org(pg_session_factory, slug=f"bill-default-{uuid4().hex[:8]}")
        from app.database.models import Organization

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "trialing"
            assert org.billing_provider_customer_id is None
            assert org.last_payment_at is None
            assert org.next_payment_due_at is None
            assert org.is_active is True
    finally:
        await _cleanup_org(pg_session_factory, org_id)


# ── is_billing_current ─────────────────────────────────────────────────────

class TestIsBillingCurrent:
    @pytest.mark.parametrize("status", ["trialing", "active"])
    def test_current_statuses(self, status):
        from app.services.billing import is_billing_current
        from app.database.models import Organization

        org = Organization(name="X", slug=f"x-{status}")
        org.billing_status = status
        assert is_billing_current(org) is True

    @pytest.mark.parametrize("status", ["past_due", "suspended", "canceled", "unknown"])
    def test_non_current_statuses(self, status):
        from app.services.billing import is_billing_current
        from app.database.models import Organization

        org = Organization(name="X", slug=f"x-{status}")
        org.billing_status = status
        assert is_billing_current(org) is False

    def test_tolerates_bare_object(self):
        from app.services.billing import is_billing_current

        assert is_billing_current(SimpleNamespace()) is True


# ── Real-PG service mutations ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_paid_sets_active_and_due_date(pg_session_factory):
    from app.database.models import Organization
    from app.services.billing import mark_org_paid

    org_id = None
    try:
        org_id = await _seed_org(pg_session_factory, slug=f"bill-paid-{uuid4().hex[:8]}")
        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            await mark_org_paid(session, org, provider_customer_id="cus_123")
            await session.commit()

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "active"
            assert org.billing_provider_customer_id == "cus_123"
            assert org.last_payment_at is not None
            assert org.next_payment_due_at is not None
            delta = org.next_payment_due_at - org.last_payment_at
            assert delta.days == 30
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_status_mutations_suspend_and_reactivate(pg_session_factory):
    from app.database.models import Organization
    from app.services.billing import mark_org_past_due, suspend_org, reactivate_org

    org_id = None
    try:
        org_id = await _seed_org(pg_session_factory, slug=f"bill-cycle-{uuid4().hex[:8]}")
        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            await mark_org_past_due(session, org)
            await suspend_org(session, org)
            await reactivate_org(session, org)
            await session.commit()

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "active"
            assert org.last_payment_at is not None
            assert org.next_payment_due_at is not None
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_widget_key_rejects_suspended_org_real_db(pg_session_factory):
    """A suspended org's widget key must stop resolving, without touching is_active."""
    from app.database.crud import get_organization_by_widget_key
    from app.database.models import Organization

    org_id = None
    try:
        slug = f"bill-susp-{uuid4().hex[:8]}"
        wk = f"wk-{uuid4().hex}"
        org_id = await _seed_org(
            pg_session_factory, slug=slug, widget_key=wk, billing_status="suspended"
        )
        with patch("app.database.session.async_session_factory", pg_session_factory):
            async with pg_session_factory() as session:
                found = await get_organization_by_widget_key(session, wk)
        assert found is None, "suspended org widget key must not resolve"

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.is_active is True, "is_active must stay True"
            assert org.billing_status == "suspended"
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_widget_key_still_resolves_active_org_real_db(pg_session_factory):
    from app.database.crud import get_organization_by_widget_key

    org_id = None
    try:
        slug = f"bill-active-{uuid4().hex[:8]}"
        wk = f"wk-{uuid4().hex}"
        org_id = await _seed_org(
            pg_session_factory, slug=slug, widget_key=wk, billing_status="active"
        )
        with patch("app.database.session.async_session_factory", pg_session_factory):
            async with pg_session_factory() as session:
                found = await get_organization_by_widget_key(session, wk)
        assert found is not None
        assert str(found.id) == org_id
    finally:
        await _cleanup_org(pg_session_factory, org_id)


# ── list_overdue ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_overdue_real_db(pg_session_factory):
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.database.models import Organization
    from app.services.billing import list_overdue_orgs

    overdue_id = active_id = suspended_id = None
    try:
        now = datetime.utcnow()
        overdue_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-overdue-{uuid4().hex[:8]}",
            billing_status="active",
            next_payment_due_at=now - timedelta(days=5),
        )
        active_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-future-{uuid4().hex[:8]}",
            billing_status="active",
            next_payment_due_at=now + timedelta(days=10),
        )
        suspended_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-suspend-{uuid4().hex[:8]}",
            billing_status="suspended",
            next_payment_due_at=now - timedelta(days=5),
        )

        async with pg_session_factory() as session:
            overdue = await list_overdue_orgs(session)
        overdue_slugs = {o.slug for o in overdue}
        assert any(slug.startswith("bill-overdue-") for slug in overdue_slugs), overdue_slugs
        assert not any(slug.startswith("bill-future-") for slug in overdue_slugs)
        assert not any(slug.startswith("bill-suspend-") for slug in overdue_slugs), (
            "suspended orgs must be excluded from list-overdue"
        )
    finally:
        for oid in (overdue_id, active_id, suspended_id):
            await _cleanup_org(pg_session_factory, oid)


# ── CLI ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_mark_paid_suspend_reactivate_real_db(pg_session_factory):
    sys_path = _import_manage_billing()
    manage = sys_path["manage"]
    from app.database.models import Organization

    org_id = None
    try:
        slug = f"bill-cli-{uuid4().hex[:8]}"
        org_id = await _seed_org(pg_session_factory, slug=slug)

        with patch("app.database.session.async_session_factory", pg_session_factory):
            rc = await manage.run_cmd(SimpleNamespace(
                list_overdue=False, mark_paid=True, mark_past_due=False,
                suspend=False, reactivate=False, ref=slug, customer_id="cus_cli",
            ))
            assert rc == 0
        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.slug == slug)
            )).scalar_one()
            assert org.billing_status == "active"
            assert org.billing_provider_customer_id == "cus_cli"

        with patch("app.database.session.async_session_factory", pg_session_factory):
            rc = await manage.run_cmd(SimpleNamespace(
                list_overdue=False, mark_paid=False, mark_past_due=False,
                suspend=True, reactivate=False, ref=slug, customer_id=None,
            ))
            assert rc == 0
        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.slug == slug)
            )).scalar_one()
            assert org.billing_status == "suspended"
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_cli_list_overdue_real_db(pg_session_factory):
    from datetime import datetime, timedelta
    manage = _import_manage_billing()["manage"]

    overdue_id = None
    try:
        now = datetime.utcnow()
        overdue_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-cli-overdue-{uuid4().hex[:8]}",
            billing_status="past_due",
            next_payment_due_at=now - timedelta(days=1),
        )
        with patch("app.database.session.async_session_factory", pg_session_factory):
            rc = await manage.run_cmd(SimpleNamespace(list_overdue=True))
        assert rc == 0
    finally:
        await _cleanup_org(pg_session_factory, overdue_id)


def _import_manage_billing():
    import sys
    sys.path.insert(0, ".")
    from scripts import manage_billing as manage
    return {"manage": manage}


# ── Stripe webhook ─────────────────────────────────────────────────────────

@pytest.fixture
async def billing_client(pg_session_factory):
    from fastapi import FastAPI
    from app.api.billing import router as billing_router
    from app.database.session import get_session

    test_app = FastAPI()
    test_app.include_router(billing_router)

    async def _override_session():
        async with pg_session_factory() as session:
            yield session

    test_app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _event(type_: str, **object_kwargs) -> bytes:
    evt = {
        "id": f"evt_{uuid4().hex}",
        "object": "event",
        "type": type_,
        "data": {"object": {"object": "event_payload", **object_kwargs}},
    }
    return json.dumps(evt).encode()


@pytest.mark.asyncio
async def test_webhook_404_when_disabled(billing_client, pg_session_factory):
    """When STRIPE_ENABLED is false the route must not process anything."""
    with patch("app.config.settings.settings.stripe_enabled", False):
        resp = await billing_client.post(
            "/billing/webhook",
            content=_event("checkout.session.completed", customer="cus_x"),
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_webhook_rejects_forged_signature(billing_client):
    payload = _event("checkout.session.completed", customer="cus_forged")
    with patch("app.config.settings.settings.stripe_enabled", True), \
         patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
         patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
        resp = await billing_client.post(
            "/billing/webhook",
            content=payload,
            headers={"Stripe-Signature": f"t={int(time.time())},v1={'0' * 64}"},
        )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_webhook_missing_signature_header(billing_client):
    with patch("app.config.settings.settings.stripe_enabled", True), \
         patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
         patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
        resp = await billing_client.post("/billing/webhook", content=_event("checkout.session.completed"))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_webhook_checkout_session_completed_marks_paid(billing_client, pg_session_factory):
    from app.database.models import Organization

    org_id = None
    try:
        org_id = await _seed_org(pg_session_factory, slug=f"bill-webhook-{uuid4().hex[:8]}")
        payload = _event("checkout.session.completed", client_reference_id=org_id, customer="cus_paid")

        with patch("app.config.settings.settings.stripe_enabled", True), \
             patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
             patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
            resp = await billing_client.post(
                "/billing/webhook",
                content=payload,
                headers={"Stripe-Signature": _signed_header(payload, WH_SECRET)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["handled"] is True

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "active"
            assert org.billing_provider_customer_id == "cus_paid"
            assert org.last_payment_at is not None
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_webhook_invoice_payment_failed_marks_past_due(billing_client, pg_session_factory):
    from app.database.models import Organization

    org_id = None
    try:
        org_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-pd-{uuid4().hex[:8]}",
            billing_provider_customer_id="cus_pd",
            billing_status="active",
        )
        payload = _event("invoice.payment_failed", customer="cus_pd")

        with patch("app.config.settings.settings.stripe_enabled", True), \
             patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
             patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
            resp = await billing_client.post(
                "/billing/webhook",
                content=payload,
                headers={"Stripe-Signature": _signed_header(payload, WH_SECRET)},
            )
        assert resp.status_code == 200, resp.text

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "past_due"
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_webhook_subscription_deleted_suspends(billing_client, pg_session_factory):
    from app.database.models import Organization

    org_id = None
    try:
        org_id = await _seed_org(
            pg_session_factory,
            slug=f"bill-cancel-{uuid4().hex[:8]}",
            billing_provider_customer_id="cus_cancel",
            billing_status="active",
        )
        payload = _event("customer.subscription.deleted", customer="cus_cancel")

        with patch("app.config.settings.settings.stripe_enabled", True), \
             patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
             patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
            resp = await billing_client.post(
                "/billing/webhook",
                content=payload,
                headers={"Stripe-Signature": _signed_header(payload, WH_SECRET)},
            )
        assert resp.status_code == 200, resp.text

        async with pg_session_factory() as session:
            org = (await session.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one()
            assert org.billing_status == "suspended"
            assert org.is_active is True
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_webhook_unknown_event_type_acknowledged_not_handled(billing_client, pg_session_factory):
    org_id = None
    try:
        org_id = await _seed_org(pg_session_factory, slug=f"bill-unhandled-{uuid4().hex[:8]}")
        payload = _event("payment_intent.succeeded", client_reference_id=org_id, customer="cus_x")

        with patch("app.config.settings.settings.stripe_enabled", True), \
             patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
             patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
            resp = await billing_client.post(
                "/billing/webhook",
                content=payload,
                headers={"Stripe-Signature": _signed_header(payload, WH_SECRET)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["handled"] is False
        assert resp.json()["reason"] == "unhandled_type"
    finally:
        await _cleanup_org(pg_session_factory, org_id)


@pytest.mark.asyncio
async def test_webhook_unknown_org_acknowledged_not_handled(billing_client):
    payload = _event("checkout.session.completed", customer="cus_ghost")
    with patch("app.config.settings.settings.stripe_enabled", True), \
         patch("app.config.settings.settings.stripe_api_key", "sk_test_x"), \
         patch("app.config.settings.settings.stripe_webhook_secret", WH_SECRET):
        resp = await billing_client.post(
            "/billing/webhook",
            content=payload,
            headers={"Stripe-Signature": _signed_header(payload, WH_SECRET)},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["handled"] is False
    assert resp.json()["reason"] == "unknown_org"
