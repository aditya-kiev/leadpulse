import io
import logging

import pytest

from app.agent.nodes.helpers import log_fingerprint
from app.agent.tools.sms import _mask_phone
from app.services.logging_config import JSONFormatter, configure_logging, redact


class TestRedact:
    @pytest.mark.parametrize("secret_value", [
        "api_key=sk-1234567890abcdef",
        'secret="abc123xyz789"',
        "password=hunter2",
        "token=abc.def.ghi",
    ])
    def test_key_value_secrets_redacted(self, secret_value):
        assert redact(secret_value) == "[REDACTED]"

    def test_bearer_jwt_redacted(self):
        jwt = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.xBcGFThROTP93xjO0V2QmkUZ7Z9zzF_a_xZVDxN5E"
        out = redact(jwt)
        assert "eyJhbGciOiJIUzI1NiJ9" not in out
        assert "[REDACTED]" in out

    def test_standalone_jwt_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.xBcGFThROTP93xjO0V2QmkUZ7Z9zzF_a_xZVDxN5E"
        assert redact(jwt) == "[REDACTED]"

    def test_url_credentials_redacted(self):
        out = redact("postgresql+asyncpg://postgres:postgres@localhost:5432/test")
        assert ":postgres@" not in out
        assert "[REDACTED]" in out
        assert out == "postgresql+asyncpg[REDACTED]localhost:5432/test"

    def test_redis_url_credentials_redacted(self):
        assert redact("redis://:supersecret@localhost:6379/0") == "redis[REDACTED]localhost:6379/0"

    def test_innocuous_message_untouched(self):
        msg = "meeting booked at 2026-08-11T10:00:00 for lead_status=hot"
        assert redact(msg) == msg

    @pytest.mark.parametrize("env_assignment", [
        "GEMINI_API_KEY=AIzaSyD-1234567890abcdef",
        "JWT_SECRET_KEY=super-secret-jwt-signing-key",
        "STRIPE_WEBHOOK_SECRET=whsec_1234567890abcdef",
        "DEMO_TOKEN_SECRET=test-demo-token-secret-32-chars",
        "CRM_ENCRYPTION_KEY=MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
    ])
    def test_snake_case_suffix_secret_names_redacted(self, env_assignment):
        """The real settings env-var names: keyword is a SUFFIX of the
        identifier, which the old r"\\b(keyword)\\b" pattern never matched."""
        out = redact(env_assignment)
        assert out == "[REDACTED]"
        _, value = env_assignment.split("=", 1)
        assert value not in out

    def test_lowercase_snake_case_identifier_redacted(self):
        out = redact("stripe_webhook_secret=whsec_abcdef123456")
        assert out == "[REDACTED]"

    def test_dict_repr_single_quotes_redacted(self):
        out = redact("{'api_key': 'sk-live-hiddenvalue'}")
        assert "sk-live-hiddenvalue" not in out
        assert "[REDACTED]" in out

    def test_json_repr_double_quotes_redacted(self):
        out = redact('{"JWT_SECRET_KEY": "leaked-signing-key"}')
        assert "leaked-signing-key" not in out
        assert "[REDACTED]" in out

    def test_dict_repr_inside_larger_log_line(self):
        msg = "config loaded {'stripe_webhook_secret': 'whsec_hidden1'} ok"
        out = redact(msg)
        assert "whsec_hidden1" not in out
        assert "[REDACTED]" in out
        assert "config loaded" in out and "ok" in out

    def test_existing_working_case_not_regressed(self):
        assert redact("api_key=X") == "[REDACTED]"


class TestLogFingerprint:
    def test_returns_length_and_hash_prefix(self):
        fp = log_fingerprint("hello lead, tell me about your pricing")
        assert fp.startswith("len=")
        assert "sha256=" in fp

    def test_empty_input(self):
        assert log_fingerprint("") == "len=0 sha256=" + "e3b0c44298fc"


class TestMaskPhone:
    def test_masks_all_but_last_four(self):
        assert _mask_phone("+15551234567").endswith("4567")

    def test_short_phone_masked_fully(self):
        assert _mask_phone("123") == "***"


class TestJSONFormatter:
    def _emit(self, message: str, exc_info=None) -> str:
        handler = logging.StreamHandler(io.StringIO())
        formatter = JSONFormatter()
        handler.setFormatter(formatter)
        logger = logging.getLogger("test.redact")
        logger.setLevel(logging.DEBUG)
        logger.handlers = [handler]
        logger.info(message)
        record = logging.LogRecord(
            name="test.redact",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg=message,
            args=(),
            exc_info=exc_info,
        )
        return formatter.format(record)

    def test_json_formatter_redacts_message(self):
        out = self._emit("credentials api_key=sk-1234567890abcdef logged")
        assert "[REDACTED]" in out
        assert "sk-1234567890abcdef" not in out


def test_configure_logging_uses_redacting_formatter():
    configure_logging(environment="production")
    root = logging.getLogger()
    handler = root.handlers[0]
    assert isinstance(handler.formatter, JSONFormatter)
