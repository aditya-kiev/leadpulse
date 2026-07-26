import logging
from uuid import UUID

from app.database.crud import log_crm_push
from app.integrations.registry import resolve_integration
from app.integrations.retry import retry_with_backoff

logger = logging.getLogger(__name__)


async def update_crm(session_id: str, lead_data: dict, tenant_id: UUID | None = None) -> dict:
    if tenant_id is None:
        logger.info("CRM push skipped: no tenant_id for session=%s", session_id)
        return {"status": "skipped", "reason": "no_tenant"}

    integration = await resolve_integration(tenant_id)

    try:
        result = await retry_with_backoff(
            integration.push_lead,
            lead_data,
            max_retries=3,
            base_delay=1.0,
        )
        logger.info(
            "CRM push %s tenant=%s session=%s integration=%s external_id=%s",
            "success" if result.success else "failed",
            tenant_id, session_id, integration.integration_type, result.external_id,
        )
    except Exception as e:
        logger.error(
            "CRM push failed after retries tenant=%s session=%s integration=%s: %s",
            tenant_id, session_id, integration.integration_type, e,
        )
        result = None

    # Log push attempt
    try:
        await log_crm_push(
            organization_id=tenant_id,
            integration_type=integration.integration_type,
            session_id=session_id,
            status=result.status if result else "failed",
            attempt=1,
            lead_data=lead_data,
            response_data=result.raw_response if result else None,
            error_message=result.error_message if result else "All retries exhausted",
        )
    except Exception as log_err:
        logger.warning("Failed to log CRM push for session=%s: %s", session_id, log_err)

    if result and result.success:
        return {"status": result.status, "external_id": result.external_id}
    return {"status": "failed", "error": result.error_message if result else "push_error"}


async def get_crm_lead(session_id: str, tenant_id: UUID | None = None) -> dict | None:
    if tenant_id is None:
        return None
    integration = await resolve_integration(tenant_id)
    result = await integration.pull_status(session_id)
    return result
