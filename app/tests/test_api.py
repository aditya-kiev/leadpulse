from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from httpx import AsyncClient, ASGITransport

import app.services.redis as redis_mod
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    prev = redis_mod._redis
    redis_mod._redis = fake
    with patch("app.services.redis.settings.redis_url", "redis://fake:6379"):
        yield fake
    redis_mod._redis = prev


@pytest.mark.asyncio
async def test_health_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_webhook_message_requires_api_key(client):
    """Without API key header, webhook/message should return 401 when API key is set."""
    with patch("app.config.settings.settings.api_key", "test-secret-key"):
        payload = {"session_id": "test-session", "message": "Hello"}
        response = await client.post("/webhook/message", json=payload)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_message_with_valid_api_key(client):
    """With valid API key header, webhook/message should accept the request."""
    with patch("app.config.settings.settings.api_key", "test-secret-key"):
        with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = {
                "conversation_history": [],
                "lead_status": None,
                "booking_confirmed": False,
                "meeting_time": None,
                "human_escalated": False,
                "next_action": None,
            }
            payload = {"session_id": "test-session", "message": "Hello"}
            response = await client.post(
                "/webhook/message",
                json=payload,
                headers={"X-API-Key": "test-secret-key"},
            )
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_message_skips_auth_when_no_key_configured(client):
    """When settings.api_key is empty (local dev), auth should be skipped."""
    with patch("app.config.settings.settings.api_key", ""):
        with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = {
                "conversation_history": [],
                "lead_status": None,
                "booking_confirmed": False,
                "meeting_time": None,
                "human_escalated": False,
                "next_action": None,
            }
            payload = {"session_id": "test-session", "message": "Hello"}
            response = await client.post(
                "/webhook/message", json=payload
            )
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_debug_state_not_mounted_in_production(client):
    """Debug router should not be mounted when settings.debug is False."""
    with patch("app.config.settings.settings.debug", False):
        response = await client.get("/debug/state/test-session")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_demo_token_endpoint_returns_token(client):
    """POST /demo/token must return 200 with session_id, token, expires_at."""
    with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"):
        response = await client.post("/demo/token")
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["session_id"].startswith("demo-")
        assert "token" in data
        assert "expires_at" in data
        assert isinstance(data["expires_at"], int)


@pytest.mark.asyncio
async def test_demo_token_authenticates_webhook(client):
    """A valid demo token must authenticate POST /webhook/message."""
    with patch("app.config.settings.settings.api_key", "master-key"):
        with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"):
            # Get a demo token
            resp = await client.post("/demo/token")
            assert resp.status_code == 200
            tok_data = resp.json()

            with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
                mock_agent.return_value = {
                    "conversation_history": [],
                    "lead_status": None,
                    "booking_confirmed": False,
                    "meeting_time": None,
                    "human_escalated": False,
                    "next_action": None,
                }
                payload = {"session_id": tok_data["session_id"], "message": "Hello"}
                response = await client.post(
                    "/webhook/message",
                    json=payload,
                    headers={"X-Demo-Token": tok_data["token"]},
                )
                assert response.status_code == 200


@pytest.mark.asyncio
async def test_demo_token_rejected_for_wrong_session(client):
    """A demo token issued for session A must be rejected for session B."""
    with patch("app.config.settings.settings.api_key", "master-key"):
        with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"):
            resp = await client.post("/demo/token")
            assert resp.status_code == 200
            tok_data = resp.json()

            payload = {"session_id": "some-other-session", "message": "Hello"}
            response = await client.post(
                "/webhook/message",
                json=payload,
                headers={"X-Demo-Token": tok_data["token"]},
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_demo_token_rejected_when_expired(client):
    """An expired demo token must be rejected (401)."""
    with patch("app.config.settings.settings.api_key", "master-key"):
        with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"):
            resp = await client.post("/demo/token")
            assert resp.status_code == 200
            tok_data = resp.json()

            # Fast-forward past expiry
            with patch("time.time", return_value=tok_data["expires_at"] + 1):
                payload = {"session_id": tok_data["session_id"], "message": "Hello"}
                response = await client.post(
                    "/webhook/message",
                    json=payload,
                    headers={"X-Demo-Token": tok_data["token"]},
                )
                assert response.status_code == 401


@pytest.mark.asyncio
async def test_demo_token_rate_limited_per_ip(client, fake_redis):
    """POST /demo/token should return 429 after exceeding the per-IP rate limit."""
    with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"), \
         patch("app.config.settings.settings.demo_token_mint_rpm_limit", 1):
        assert (await client.post("/demo/token")).status_code == 200
        response = await client.post("/demo/token")
        assert response.status_code == 429
        assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_master_api_key_still_works_with_demo_token_feature(client):
    """x-api-key (master key) must still work exactly as before — no regression."""
    with patch("app.config.settings.settings.api_key", "master-key"):
        with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = {
                "conversation_history": [],
                "lead_status": None,
                "booking_confirmed": False,
                "meeting_time": None,
                "human_escalated": False,
                "next_action": None,
            }
            payload = {"session_id": "test-session", "message": "Hello"}
            response = await client.post(
                "/webhook/message",
                json=payload,
                headers={"X-API-Key": "master-key"},
            )
            assert response.status_code == 200
