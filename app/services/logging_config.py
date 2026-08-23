import json
import logging
import re
import sys
from datetime import datetime, timezone


# Any identifier SEGMENT naming a secret. Matching the whole surrounding
# identifier (prefix/infix/suffix, snake_case or dashed) rather than the bare
# word is what makes GEMINI_API_KEY / JWT_SECRET_KEY / STRIPE_WEBHOOK_SECRET /
# DEMO_TOKEN_SECRET / CRM_ENCRYPTION_KEY redact -- a plain r"\b(...)\b" never
# fires when the keyword is a suffix (underscore is a word char, so \b does
# not exist between "_" and the keyword) or embedded mid-identifier.
_SECRET_KEYWORD = r"(?:api[_-]?key|secret|token|password|passwd|key)"

_REDACT_PATTERNS = [
    # key=value / key: value, where the LHS is any identifier containing a
    # secret keyword (covers NAME=value env-style leaks):
    re.compile(
        rf"(?i)[a-z0-9_\-]*{_SECRET_KEYWORD}[a-z0-9_\-]*\s*[=:]\s*"
        r"['\"]?[^\s'\"&,;]+['\"]?"
    ),
    # dict / JSON-repr style: 'secret_keyword': 'value' and "key": "value"
    # (the quotes around the key break the [=:] adjacency above):
    re.compile(
        rf"(?i)['\"](?:[a-z0-9_\-]*{_SECRET_KEYWORD}[a-z0-9_\-]*)['\"]"
        r"\s*:\s*['\"][^'\"]*['\"]"
    ),
    re.compile(r"(?i)\b(authorization|auth)\s*[=:]\s*['\"]?[^\s'\"&,;]+['\"]?"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(://[^:@/\s]*:)([^@\s]+)(@)"),
]

_REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Mask API keys, JWTs, secrets, and URL-embedded credentials in a log string."""
    if not text:
        return text
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Base formatter that redacts secrets from the formatted message."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        return redact(super().formatMessage(record))


class JSONFormatter(RedactingFormatter):
    """Structured JSON log formatter with tenant_id/request_id/conversation_id."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if hasattr(record, "tenant_id"):
            log_entry["tenant_id"] = record.tenant_id
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if hasattr(record, "conversation_id"):
            log_entry["conversation_id"] = record.conversation_id
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(log_entry, default=str)


class ExtraLogAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges extra fields (tenant_id, request_id, etc.)
    into every log call's ``extra`` dict so the JSONFormatter can pick them up."""

    def process(self, msg, kwargs):
        kwargs.setdefault("extra", {}).update(self.extra)
        return msg, kwargs


def configure_logging(environment: str = "development", debug: bool = False):
    handler = logging.StreamHandler(sys.stdout)
    if environment == "production":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(handler)
