import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CRMConfig as CRMConfigModel
from app.database.session import async_session_factory
from app.integrations.base import CRMIntegration, CRMConfig
from app.integrations.encryption import decrypt_json
from app.integrations.webhook_fallback import WebhookFallbackIntegration

logger = logging.getLogger(__name__)

_INTEGRATION_MAP: dict[str, type[CRMIntegration]] = {}


def register_integration(integration_type: str, cls: type[CRMIntegration]):
    _INTEGRATION_MAP[integration_type] = cls
    logger.debug("registered CRM integration: %s", integration_type)


def get_integration_class(integration_type: str) -> type[CRMIntegration] | None:
    return _INTEGRATION_MAP.get(integration_type)


async def resolve_integration(
    tenant_id: UUID, session: AsyncSession | None = None,
) -> CRMIntegration:
    """Resolve the active CRM integration for a tenant.

    If no active CRM config is found, returns the WebhookFallbackIntegration.
    """
    if session is None:
        async with async_session_factory() as s:
            return await _resolve(s, tenant_id)

    return await _resolve(session, tenant_id)


async def _resolve(session: AsyncSession, tenant_id: UUID) -> CRMIntegration:
    # Exclude the "gemini" credentials row — it stores the per-tenant API key,
    # not a CRM connector. Every onboarded tenant has one, so including it
    # makes scalar_one_or_none() raise MultipleResultsFound the moment the
    # tenant also connects a real CRM (fub/kvcore/ams360/webhook).
    result = await session.execute(
        select(CRMConfigModel)
        .where(
            CRMConfigModel.organization_id == tenant_id,
            CRMConfigModel.is_active == True,
            CRMConfigModel.integration_type != "gemini",
        )
        .order_by(CRMConfigModel.created_at.desc())
    )
    rows: list[CRMConfigModel] = list(result.scalars().all())

    if len(rows) > 1:
        logger.warning(
            "tenant=%s has %d active CRM rows (%s) — using most recent; "
            "duplicate rows should be cleaned up",
            tenant_id,
            len(rows),
            [r.integration_type for r in rows],
        )
    row: CRMConfigModel | None = rows[0] if rows else None

    if row is None:
        logger.debug("no CRM config for tenant=%s — using webhook fallback", tenant_id)
        return WebhookFallbackIntegration(
            tenant_id=tenant_id,
            config=CRMConfig(integration_type="webhook", credentials={}),
        )

    cls = get_integration_class(row.integration_type)
    if cls is None:
        logger.warning(
            "unknown integration_type=%s for tenant=%s — using webhook fallback",
            row.integration_type, tenant_id,
        )
        return WebhookFallbackIntegration(
            tenant_id=tenant_id,
            config=CRMConfig(integration_type="webhook", credentials={}),
        )

    credentials = decrypt_json(row.config, tenant_id) if row.config else {}
    field_mapping = credentials.pop("_field_mapping", None)

    return cls(
        tenant_id=tenant_id,
        config=CRMConfig(
            integration_type=row.integration_type,
            credentials=credentials,
            field_mapping=field_mapping,
            is_active=row.is_active,
        ),
    )
