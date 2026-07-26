import json
import uuid
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


def test_encrypt_decrypt_fallback_no_key():
    data = {"api_key": "test"}
    with patch("app.config.settings.settings.crm_encryption_key", ""):
        encrypted = encrypt_json(data)
        assert isinstance(encrypted, str)
        decrypted = decrypt_json(encrypted)
        assert decrypted == data


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


def test_encrypt_malicious_payload_is_inert_no_key():
    malicious = {
        "api_key": "__import__('os').system('id')",
        "nested": "eval('__import__(\"os\").system(\"id\")')",
    }
    with patch("app.config.settings.settings.crm_encryption_key", ""):
        encrypted = encrypt_json(malicious)
        decrypted = decrypt_json(encrypted)
        assert decrypted == malicious
        assert isinstance(decrypted["api_key"], str)
        assert "__import__" in decrypted["api_key"]


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


def test_development_allows_fallback():
    with patch("app.config.settings.settings.environment", "development"), \
         patch("app.config.settings.settings.crm_encryption_key", ""):
        data = {"api_key": "test"}
        encrypted = encrypt_json(data)
        decrypted = decrypt_json(encrypted)
        assert decrypted == data


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
        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_scalar
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
        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none.return_value = mock_db_row
        mock_session.execute.return_value = mock_scalar
        yield mock_session

    with patch("app.integrations.registry.async_session_factory", side_effect=mock_session_factory), \
         patch("app.integrations.registry.decrypt_json", return_value={"api_key": "fub-key"}):
        integration = await resolve_integration(TENANT_ID)
        assert integration.integration_type == "fub"


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
