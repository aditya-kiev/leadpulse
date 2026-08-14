import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from app.config.settings import settings

logger = logging.getLogger(__name__)

_EMAIL_LOG: list[dict] = []

_RESEND_ENDPOINT = "https://api.resend.com/emails"


def _body_fingerprint(body: str) -> str:
    digest = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]
    return f"len={len(body or '')} sha256={digest}"


async def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email via Resend, or fall back to an in-memory stub when no
    API key is configured (local dev).

    ``RESEND_API_KEY`` is required for real delivery; ``RESEND_FROM_EMAIL``
    is the verified sender address. The blocking HTTP call is offloaded to a
    thread via ``asyncio.to_thread``.
    """
    now = datetime.now(timezone.utc).isoformat()

    if not settings.resend_api_key:
        logger.debug("email stub: to=%s subject=%s body=%s", to, subject, _body_fingerprint(body))
        entry = {
            "id": f"stub-{len(_EMAIL_LOG)}",
            "status": "sent",
            "to": to,
            "subject": subject,
            "body": body,
            "sent_at": now,
        }
        _EMAIL_LOG.append(entry)
        return entry

    def _send():
        import httpx
        resp = httpx.post(
            _RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.resend_from_email,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        result = await asyncio.to_thread(_send)
        message_id = result.get("id")
        logger.debug("email sent: id=%s to=%s", message_id, to)
        return {
            "id": message_id,
            "status": "sent",
            "to": to,
            "subject": subject,
            "body": body,
            "sent_at": now,
        }
    except Exception as e:
        logger.warning("email failed: %s", e)
        return {
            "id": None,
            "status": "failed",
            "to": to,
            "subject": subject,
            "body": body,
            "sent_at": now,
            "error": str(e),
        }


def get_email_log() -> list[dict]:
    return list(_EMAIL_LOG)
