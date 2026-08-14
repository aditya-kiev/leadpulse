import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_cors_preflight_returns_wildcard(client):
    r = await client.options(
        "/health",
        headers={"origin": "http://localhost:5500", "access-control-request-method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_cors_get_returns_wildcard(client):
    r = await client.get("/health", headers={"origin": "http://localhost:5500"})
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_cors_null_origin_accepted(client):
    r = await client.options(
        "/health",
        headers={"origin": "null", "access-control-request-method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_cors_file_origin_accepted(client):
    r = await client.options(
        "/health",
        headers={"origin": "file://", "access-control-request-method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == "*"


# ── Allowed-origins guard ────────────────────────────────────────────────


def test_wildcard_origin_rejected_when_auth_enabled():
    """ALLOWED_ORIGINS=['*'] + AUTH_ENABLED=true must fail fast."""
    from app.main import check_allowed_origins_production
    with patch("app.config.settings.settings.environment", "development"), \
         patch("app.config.settings.settings.auth_enabled", True), \
         patch("app.config.settings.settings.allowed_origins", ["*"]):
        with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
            check_allowed_origins_production()


def test_wildcard_origin_ok_when_dev_single_tenant():
    from app.main import check_allowed_origins_production
    with patch("app.config.settings.settings.environment", "development"), \
         patch("app.config.settings.settings.auth_enabled", False), \
         patch("app.config.settings.settings.allowed_origins", ["*"]):
        check_allowed_origins_production()


def test_explicit_origins_ok_when_production():
    from app.main import check_allowed_origins_production
    with patch("app.config.settings.settings.environment", "production"), \
         patch("app.config.settings.settings.auth_enabled", True), \
         patch("app.config.settings.settings.allowed_origins", ["https://dashboard.example.com"]):
        check_allowed_origins_production()
