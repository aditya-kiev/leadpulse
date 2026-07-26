from app.integrations.base import CRMIntegration, CRMConfig, PushResult
from app.integrations.registry import register_integration, get_integration_class, resolve_integration
from app.integrations.retry import retry_with_backoff
from app.integrations.webhook_fallback import WebhookFallbackIntegration

# Register built-in CRM integrations (import triggers registration)
from app.integrations import fub, kvcore, ams360  # noqa: F401

__all__ = [
    "CRMIntegration",
    "CRMConfig",
    "PushResult",
    "register_integration",
    "get_integration_class",
    "resolve_integration",
    "retry_with_backoff",
    "WebhookFallbackIntegration",
]
