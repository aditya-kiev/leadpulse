"""Stripe billing webhook.

Mounted only when ``STRIPE_ENABLED=true`` (see app/main.py). When disabled the
route is not registered, so ``/billing/webhook`` returns 404 — it never errors
at startup. Every request is verified with ``stripe.Webhook.construct_event``
using ``STRIPE_WEBHOOK_SECRET``; unverified payloads are rejected with 400 and
never touch the database.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.session import get_session
from app.services.billing import (
    get_organization_by_provider_customer_id,
    mark_org_paid,
    mark_org_past_due,
    suspend_org,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

STRIPE_SIGNATURE_HEADER = "Stripe-Signature"


def _verify_stripe_event(payload: bytes, signature_header: str) -> dict:
    """Verify a Stripe webhook signature and return the parsed event."""
    import stripe

    stripe.api_key = settings.stripe_api_key
    event = stripe.Webhook.construct_event(
        payload,
        signature_header,
        settings.stripe_webhook_secret,
    )
    return event.to_dict()


async def _resolve_org(session: AsyncSession, event: dict) -> object | None:
    """Find the organization the event belongs to.

    Priority: ``client_reference_id`` set to the org UUID at Checkout; then the
    Stripe ``customer`` id mapped via ``billing_provider_customer_id``.
    """
    data = event.get("data", {}).get("object", {})
    client_ref = data.get("client_reference_id")
    if client_ref:
        try:
            org_id = UUID(str(client_ref))
        except (ValueError, TypeError):
            org_id = None
        if org_id is not None:
            from app.database.crud import get_organization_by_id

            org = await get_organization_by_id(session, org_id)
            if org is not None:
                return org
    customer_id = data.get("customer")
    if customer_id:
        org = await get_organization_by_provider_customer_id(session, str(customer_id))
        if org is not None:
            return org
    return None


@router.post("/webhook", status_code=200)
async def billing_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    stripe_signature: str | None = Header(None, alias=STRIPE_SIGNATURE_HEADER),
) -> dict:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=404, detail="Billing webhook is not enabled")
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Billing webhook is not configured")

    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not stripe_signature:
        raise HTTPException(status_code=400, detail=f"Missing {STRIPE_SIGNATURE_HEADER} header")

    try:
        event = _verify_stripe_event(payload, stripe_signature)
    except Exception as e:
        logger.warning("Billing webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    org = await _resolve_org(session, event)
    if org is None:
        # Unknown org — acknowledge without action so Stripe stops retrying.
        logger.warning("Billing webhook %s matched no organization (customer=%s)",
                       event_type, data.get("customer"))
        return {"received": True, "handled": False, "reason": "unknown_org"}

    try:
        if event_type == "checkout.session.completed":
            await mark_org_paid(session, org, provider_customer_id=data.get("customer"))
        elif event_type == "invoice.payment_failed":
            await mark_org_past_due(session, org)
        elif event_type == "customer.subscription.deleted":
            await suspend_org(session, org)
        else:
            # Other events (e.g. payment_intent.succeeded) are acked, not acted on.
            return {"received": True, "handled": False, "reason": "unhandled_type"}
        await session.commit()
    except Exception as e:
        logger.exception("Billing webhook %s failed for org %s", event_type, org.id)
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return {"received": True, "handled": True, "event": event_type}
