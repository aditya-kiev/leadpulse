import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.base import CRMIntegration, CRMConfig, PushResult
from app.integrations.encryption import encrypt_json, decrypt_json, check_production_encryption_key
from app.integrations.registry import register_integration, get_integration_class, resolve_integration
from app.integrations.retry import retry_with_backoff
from app.integrations.webhook_fallback import WebhookFallbackIntegration

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ── CRMConfig / PushResult ────────────────────────────────────────────────

def test_push_result_defaults():
    r = PushResult(success=True)
    assert r.success
    assert r.external_id is None
    assert r.status == "unknown"
    assert r.error_message is None
    assert r.raw_response is None
    assert r.timestamp is not None


def test_crm_config_dataclass():
    cfg = CRMConfig(integration_type="fub", credentials={"api_key": "sekret"})
    assert cfg.integration_type == "fub"
    assert cfg.credentials["api_key"] == "sekret"
    assert cfg.field_mapping is None
    assert cfg.is_active is True


# ── Encryption ────────────────────────────────────────────────────────────

def test_encrypt_decrypt_round_trip():
    data = {"api_key": "super-secret-123", "source": "test", "nested": {"inner": 42}}
    with patch("app.config.settings.settings.crm_encryption_key", "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
        encrypted = encrypt_json(data)
        decrypted = decrypt_json(encrypted)
        assert decrypted == data


def test_encrypt_decrypt_no_key_raises():
    data = {"api_key": "test"}
    with patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError, match="CRM_ENCRYPTION_KEY must be configured"):
            encrypt_json(data)


def test_encrypt_malicious_payload_is_inert_with_key():
    malicious = {
        "api_key": "__import__('os').system('id')",
        "nested": "eval('__import__(\"os\").system(\"id\")')",
    }
    with patch("app.config.settings.settings.crm_encryption_key", "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
        encrypted = encrypt_json(malicious)
        decrypted = decrypt_json(encrypted)
        assert decrypted == malicious
        assert isinstance(decrypted["api_key"], str)
        assert "__import__" in decrypted["api_key"]


def test_encrypt_no_key_never_base64_encodes():
    malicious = {
        "api_key": "__import__('os').system('id')",
        "nested": "eval('__import__(\"os\").system(\"id\")')",
    }
    with patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError):
            encrypt_json(malicious)


def test_encrypt_decrypt_binary_safe_values():
    data = {
        "token": "eyJhbGciOiJIUzI1NiJ9.dGVzdA.abc123",
        "secret": "abc123+/=",
        "url": "https://api.example.com/v1?key=val&other=1",
    }
    with patch("app.config.settings.settings.crm_encryption_key", "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
        encrypted = encrypt_json(data)
        decrypted = decrypt_json(encrypted)
        assert decrypted == data


def test_production_raises_without_key():
    with patch("app.config.settings.settings.environment", "production"), \
         patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError, match="CRM_ENCRYPTION_KEY must be set"):
            check_production_encryption_key()


def test_production_allows_with_key():
    with patch("app.config.settings.settings.environment", "production"), \
         patch("app.config.settings.settings.crm_encryption_key", "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
        check_production_encryption_key()


def test_auth_enabled_requires_key():
    """AUTH_ENABLED=true with no CRM_ENCRYPTION_KEY must fail fast."""
    with patch("app.config.settings.settings.environment", "development"), \
         patch("app.config.settings.settings.auth_enabled", True), \
         patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError, match="CRM_ENCRYPTION_KEY must be set"):
            check_production_encryption_key()


def test_development_no_key_raises_on_encrypt():
    """Even in dev, encrypt_json must never silently base64-encode."""
    with patch("app.config.settings.settings.environment", "development"), \
         patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError, match="CRM_ENCRYPTION_KEY must be configured"):
            encrypt_json({"api_key": "test"})


def test_decrypt_no_key_raises():
    with patch("app.config.settings.settings.crm_encryption_key", ""):
        with pytest.raises(RuntimeError, match="CRM_ENCRYPTION_KEY must be configured"):
            decrypt_json("not-a-token")


# ── REAL Postgres: stored CRM config must never be reversible plaintext ──
#
# Regression guard for the "silent base64 fallback" class of bug: whatever
# lands in ``crm_configs.config`` must be Fernet-ciphertext, NOT a base64
# encoding of the JSON credentials (which any bystander could decode).

import os
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stored_crm_config_is_not_base64_json(pg_session_factory):
    """A config encrypted via encrypt_json and persisted must NOT decode as
    base64 JSON (that would mean it was stored as reversible plaintext)."""
    from app.database.models import CRMConfig, Organization
    from sqlalchemy import select

    org_id = None
    try:
        async with pg_session_factory() as session:
            org = Organization(name="Enc IT", slug=f"enc-it-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            org_id = org.id
            with patch("app.config.settings.settings.crm_encryption_key",
                       "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
                session.add(CRMConfig(
                    organization_id=org.id,
                    integration_type="gemini",
                    config=encrypt_json({"api_key": "sekret", "vertical": "real_estate"}, tenant_id=org.id),
                    is_active=True,
                ))
            await session.commit()

        async with pg_session_factory() as session:
            row = (await session.execute(
                select(CRMConfig).where(CRMConfig.organization_id == org_id)
            )).scalar_one_or_none()
            assert row is not None
            stored = row.config
            assert isinstance(stored, str)
            with patch("app.config.settings.settings.crm_encryption_key",
                       "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="):
                assert decrypt_json(stored, tenant_id=org_id) == {
                    "api_key": "sekret",
                    "vertical": "real_estate",
                }
            try:
                import json as _json
                decoded = _json.loads(base64.b64decode(stored))
                pytest.fail(
                    f"stored config decoded as base64 JSON (reversible plaintext!): {decoded}"
                )
            except Exception:
                pass  # expected: not base64-encoded JSON
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


# ── Webhook Fallback ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_fallback_connect():
    integration = WebhookFallbackIntegration(TENANT_ID, CRMConfig(integration_type="webhook", credentials={}))
    assert await integration.connect() is True


@pytest.mark.asyncio
async def test_webhook_fallback_push_lead():
    integration = WebhookFallbackIntegration(TENANT_ID, CRMConfig(integration_type="webhook", credentials={}))
    result = await integration.push_lead({"lead_name": "Test"})
    assert result.success is True
    assert result.status == "logged"
    assert result.external_id is None


@pytest.mark.asyncio
async def test_webhook_fallback_pull_status():
    integration = WebhookFallbackIntegration(TENANT_ID, CRMConfig(integration_type="webhook", credentials={}))
    result = await integration.pull_status("some-id")
    assert result == {"external_id": "some-id", "status": "unknown"}


# ── Registry ──────────────────────────────────────────────────────────────

def test_register_and_resolve():
    cls = get_integration_class("fub")
    assert cls is not None
    assert cls.integration_type == "fub"


def test_register_unknown():
    cls = get_integration_class("nonexistent")
    assert cls is None


@pytest.mark.asyncio
async def test_resolve_integration_no_config():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_session_factory():
        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result
        yield mock_session

    with patch("app.integrations.registry.async_session_factory", side_effect=mock_session_factory):
        integration = await resolve_integration(TENANT_ID)
        assert isinstance(integration, WebhookFallbackIntegration)


@pytest.mark.asyncio
async def test_resolve_integration_with_fub_config():
    from contextlib import asynccontextmanager

    mock_db_row = MagicMock()
    mock_db_row.integration_type = "fub"
    mock_db_row.config = json.dumps({"api_key": "fub-key"})
    mock_db_row.is_active = True

    @asynccontextmanager
    async def mock_session_factory():
        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_db_row]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result
        yield mock_session

    with patch("app.integrations.registry.async_session_factory", side_effect=mock_session_factory), \
         patch("app.integrations.registry.decrypt_json", return_value={"api_key": "fub-key"}):
        integration = await resolve_integration(TENANT_ID)
        assert integration.integration_type == "fub"


# ── REAL Postgres: gemini credentials row must never be treated as a CRM ──
#
# Every tenant onboarded via onboard_client gets a ``gemini`` crm_configs
# row (for the per-tenant API key). Once they connect a real CRM a second
# active row (fub/kvcore/...) exists, and the old ``scalar_one_or_none()``
# query crashed with MultipleResultsFound.

_ENCRYPTION_KEY = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="


@pytest.mark.asyncio
async def test_resolve_integration_gemini_plus_fub_returns_fub(pg_session_factory):
    """gemini (credentials) row + fub (CRM) row → resolve to the fub CRM,
    NOT a MultipleResultsFound crash."""
    from app.database.models import CRMConfig, Organization

    org_id = None
    try:
        async with pg_session_factory() as session:
            org = Organization(name="CRM Resolve IT", slug=f"crm-res-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            org_id = org.id
            with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
                session.add_all([
                    CRMConfig(
                        organization_id=org.id,
                        integration_type="gemini",
                        config=encrypt_json({"api_key": "gem-key", "vertical": "real_estate"}, tenant_id=org.id),
                        is_active=True,
                    ),
                    CRMConfig(
                        organization_id=org.id,
                        integration_type="fub",
                        config=encrypt_json({"api_key": "fub-key"}, tenant_id=org.id),
                        is_active=True,
                    ),
                ])
            await session.commit()

        with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
            with patch("app.integrations.registry.async_session_factory", pg_session_factory):
                integration = await resolve_integration(org_id)
        assert integration is not None
        assert integration.integration_type == "fub"
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.asyncio
async def test_resolve_integration_only_gemini_falls_back_to_webhook(pg_session_factory):
    """gemini-only tenant (no CRM connected) → WebhookFallbackIntegration,
    gateway of the current no-CRM behavior."""
    from app.database.models import CRMConfig, Organization

    org_id = None
    try:
        async with pg_session_factory() as session:
            org = Organization(name="Gemini-Only IT", slug=f"gem-only-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            org_id = org.id
            with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
                session.add(CRMConfig(
                    organization_id=org.id,
                    integration_type="gemini",
                    config=encrypt_json({"api_key": "gem-key"}, tenant_id=org.id),
                    is_active=True,
                ))
            await session.commit()

        with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
            with patch("app.integrations.registry.async_session_factory", pg_session_factory):
                integration = await resolve_integration(org_id)
        assert isinstance(integration, WebhookFallbackIntegration)
        assert integration.integration_type == "webhook"
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.asyncio
async def test_resolve_integration_two_crm_rows_picks_most_recent(pg_session_factory):
    """If two real CRM rows are somehow active at once, pick the newest and
    log a warning rather than raising."""
    from sqlalchemy import update as sa_update
    from app.database.models import CRMConfig, Organization

    org_id = None
    try:
        async with pg_session_factory() as session:
            org = Organization(name="Two CRM IT", slug=f"two-crm-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            org_id = org.id
            with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
                session.add_all([
                    CRMConfig(
                        organization_id=org.id,
                        integration_type="kvcore",
                        config=encrypt_json({"api_key": "old-key"}, tenant_id=org.id),
                        is_active=True,
                    ),
                    CRMConfig(
                        organization_id=org.id,
                        integration_type="fub",
                        config=encrypt_json({"api_key": "new-key"}, tenant_id=org.id),
                        is_active=True,
                    ),
                ])
            await session.commit()

        # Force distinct created_at so ordering is deterministic (fub newest).
        async with pg_session_factory() as session:
            await session.execute(sa_update(CRMConfig)
                                  .where(CRMConfig.integration_type == "kvcore",
                                         CRMConfig.organization_id == org_id)
                                  .values(created_at=datetime(2024, 1, 1)))
            await session.execute(sa_update(CRMConfig)
                                  .where(CRMConfig.integration_type == "fub",
                                         CRMConfig.organization_id == org_id)
                                  .values(created_at=datetime(2024, 1, 2)))
            await session.commit()

        with patch("app.config.settings.settings.crm_encryption_key", _ENCRYPTION_KEY):
            with patch("app.integrations.registry.async_session_factory", pg_session_factory):
                integration = await resolve_integration(org_id)
        assert integration.integration_type == "fub"
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


# ── Retry ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_success_first_try():
    fn = AsyncMock(return_value="ok")
    result = await retry_with_backoff(fn, "arg1", kwarg1="v1", max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_eventually_succeeds():
    call_count = 0

    async def flaky(arg):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("not yet")
        return "success"

    result = await retry_with_backoff(flaky, "arg", max_retries=5, base_delay=0.01)
    assert result == "success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_exhausted():
    fn = AsyncMock(side_effect=ValueError("always fail"))
    with pytest.raises(ValueError, match="always fail"):
        await retry_with_backoff(fn, max_retries=2, base_delay=0.01)
    assert fn.call_count <= 3


# ── Field Mapping ─────────────────────────────────────────────────────────

def test_field_mapping_no_mapping():
    cfg = CRMConfig(integration_type="test", credentials={})
    integration = WebhookFallbackIntegration(TENANT_ID, cfg)
    result = integration._apply_field_mapping({"lead_name": "Alice", "budget": 100})
    assert result == {"lead_name": "Alice", "budget": 100}


def test_field_mapping_with_mapping():
    cfg = CRMConfig(integration_type="test", credentials={}, field_mapping={"lead_name": "fullName", "budget": "price"})
    integration = WebhookFallbackIntegration(TENANT_ID, cfg)
    result = integration._apply_field_mapping({"lead_name": "Alice", "budget": 100, "industry": "tech"})
    assert result == {"fullName": "Alice", "price": 100}


# ── FUB Integration ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fub_connect_missing_api_key():
    from app.integrations.fub import FollowUpBossIntegration
    cfg = CRMConfig(integration_type="fub", credentials={})
    integration = FollowUpBossIntegration(TENANT_ID, cfg)
    result = await integration.connect()
    assert result is False


@pytest.mark.asyncio
async def test_fub_push_fallback_to_webhook():
    from app.integrations.fub import FollowUpBossIntegration
    cfg = CRMConfig(integration_type="fub", credentials={"api_key": "test"})
    integration = FollowUpBossIntegration(TENANT_ID, cfg)
    result = await integration.push_lead({"lead_name": "Test"})
    assert result.success is False
    assert result.status == "connect_failed"


# ── kvCORE Integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kvcore_connect_no_credentials():
    from app.integrations.kvcore import KVCoreIntegration
    cfg = CRMConfig(integration_type="kvcore", credentials={})
    integration = KVCoreIntegration(TENANT_ID, cfg)
    result = await integration.connect()
    assert result is False


@pytest.mark.asyncio
async def test_kvcore_push_no_company_id():
    from app.integrations.kvcore import KVCoreIntegration
    cfg = CRMConfig(integration_type="kvcore", credentials={"api_key": "k", "api_secret": "s", "access_token": "tok"})
    integration = KVCoreIntegration(TENANT_ID, cfg)
    assert await integration.connect() is True
    result = await integration.push_lead({"lead_name": "Test"})
    assert result.success is False
    assert result.status == "no_company_id"


# ── AMS360 Integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ams360_connect_missing_credentials():
    from app.integrations.ams360 import AMS360Integration
    cfg = CRMConfig(integration_type="ams360", credentials={})
    integration = AMS360Integration(TENANT_ID, cfg)
    result = await integration.connect()
    assert result is False


@pytest.mark.asyncio
async def test_ams360_push_without_connect():
    from app.integrations.ams360 import AMS360Integration
    cfg = CRMConfig(integration_type="ams360", credentials={"api_key": "k", "api_secret": "s"})
    integration = AMS360Integration(TENANT_ID, cfg)
    result = await integration.push_lead({"lead_name": "Test"})
    assert result.success is False
    assert result.status == "connect_failed"
