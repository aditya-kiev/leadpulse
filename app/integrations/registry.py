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
    result = await session.execute(
        select(CRMConfigModel).where(
            CRMConfigModel.organization_id == tenant_id,
            CRMConfigModel.is_active == True,
        )
    )
    row: CRMConfigModel | None = result.scalar_one_or_none()

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
