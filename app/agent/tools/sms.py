import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone

from app.config.settings import settings

logger = logging.getLogger(__name__)

_stub_sms_log: list[dict] = []


def _mask_phone(phone: str) -> str:
    """Return a masked phone number (e.g. ``+1•••2345``) for logs."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def _body_fingerprint(body: str) -> str:
    digest = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]
    return f"len={len(body or '')} sha256={digest}"


async def send_sms(to: str, body: str) -> dict:
    """Send an SMS via Twilio, or fall back to an in-memory stub when credentials
    are absent (local dev).

    Trial Twilio accounts can only deliver to verified numbers (up to 5).
    The blocking Twilio SDK call is offloaded to a thread via ``asyncio.to_thread``.
    """
    now = datetime.now(timezone.utc).isoformat()

    if not settings.twilio_account_sid:
        logger.debug("sms stub: to=%s body=%s", _mask_phone(to), _body_fingerprint(body))
        entry = {
            "sid": f"stub-{len(_stub_sms_log)}",
            "status": "sent",
            "to": to,
            "body": body,
            "sent_at": now,
        }
        _stub_sms_log.append(entry)
        return entry

    def _send():
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        return client.messages.create(
            body=body,
            from_=settings.twilio_from_number,
            to=to,
        )

    try:
        message = await asyncio.to_thread(_send)
        logger.debug("sms sent: sid=%s to=%s status=%s", message.sid, _mask_phone(to), message.status)
        return {
            "sid": message.sid,
            "status": message.status,
            "to": to,
            "body": body,
            "sent_at": now,
        }
    except Exception as e:
        logger.warning("sms failed: %s", e)
        return {
            "sid": None,
            "status": "failed",
            "to": to,
            "body": body,
            "sent_at": now,
            "error": str(e),
        }


def get_stub_sms_log() -> list[dict]:
    return _stub_sms_log.copy()
