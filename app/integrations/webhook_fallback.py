import logging
from uuid import UUID

from app.integrations.base import CRMIntegration, CRMConfig, PushResult

logger = logging.getLogger(__name__)


class WebhookFallbackIntegration(CRMIntegration):
    integration_type = "webhook"

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def refresh_token(self) -> bool:
        return True

    async def push_lead(self, lead_data: dict) -> PushResult:
        logger.info(
            "webhook fallback push tenant=%s session=%s data_keys=%s",
            self.tenant_id, lead_data.get("session_id"), list(lead_data.keys()),
        )
        return PushResult(
            success=True,
            external_id=lead_data.get("session_id"),
            status="logged",
            raw_response={"note": "webhook fallback — no external CRM configured"},
        )

    async def pull_status(self, external_id: str) -> dict | None:
        return {"external_id": external_id, "status": "unknown"}
