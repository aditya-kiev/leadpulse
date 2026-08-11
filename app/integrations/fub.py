import logging
from uuid import UUID

import httpx

from app.config.settings import settings
from app.integrations.base import CRMIntegration, CRMConfig, PushResult
from app.integrations.registry import register_integration

logger = logging.getLogger(__name__)

_FUB_API_BASE = "https://api.followupboss.com/v1"


class FollowUpBossIntegration(CRMIntegration):
    integration_type = "fub"

    def __init__(self, tenant_id: UUID, config: CRMConfig):
        super().__init__(tenant_id, config)
        self._api_key = config.credentials.get("api_key", "")
        self._source = config.credentials.get("source", "LeadAgent")

    async def connect(self) -> bool:
        if not self._api_key:
            logger.error("FUB connect failed: no api_key for tenant=%s", self.tenant_id)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_FUB_API_BASE}/settings",
                    auth=httpx.BasicAuth(self._api_key, ""),
                )
                resp.raise_for_status()
                self._connected = True
                return True
        except Exception as e:
            logger.warning("FUB connect failed tenant=%s: %s", self.tenant_id, e)
            return False

    async def refresh_token(self) -> bool:
        return self._connected

    async def push_lead(self, lead_data: dict) -> PushResult:
        if not self._connected:
            ok = await self.connect()
            if not ok:
                return PushResult(success=False, status="connect_failed", error_message="Could not connect to FUB")

        payload = self._build_payload(lead_data)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_FUB_API_BASE}/people",
                    json=payload,
                    auth=httpx.BasicAuth(self._api_key, ""),
                )
                resp.raise_for_status()
                data = resp.json()
                logger.info("FUB push success tenant=%s person_id=%s", self.tenant_id, data.get("id"))
                return PushResult(
                    success=True,
                    external_id=str(data.get("id")),
                    status="pushed",
                    raw_response=data,
                )
        except httpx.HTTPStatusError as e:
            logger.error("FUB push failed tenant=%s status=%s", self.tenant_id, e.response.status_code)
            return PushResult(success=False, status=f"http_{e.response.status_code}", error_message=e.response.text[:500])
        except Exception as e:
            logger.error("FUB push exception tenant=%s: %s", self.tenant_id, e)
            return PushResult(success=False, status="error", error_message=str(e))

    async def pull_status(self, external_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_FUB_API_BASE}/people/{external_id}",
                    auth=httpx.BasicAuth(self._api_key, ""),
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("FUB pull_status failed tenant=%s id=%s: %s", self.tenant_id, external_id, e)
            return None

    def _build_payload(self, lead_data: dict) -> dict:
        mapped = self._apply_field_mapping(lead_data)
        person = {
            "firstName": mapped.get("lead_name", "").split(" ")[0] if mapped.get("lead_name") else "",
            "lastName": " ".join(mapped.get("lead_name", "").split(" ")[1:]) if mapped.get("lead_name") and len(mapped.get("lead_name", "").split(" ")) > 1 else "",
            "company": mapped.get("company_name", ""),
            "source": self._source,
            "tags": ["lead-agent", "qualified", mapped.get("lead_status", "new")],
            "customFields": {
                "industry": mapped.get("industry", ""),
                "budget": str(mapped.get("budget", "")),
                "timeline": mapped.get("timeline", ""),
                "problem_statement": mapped.get("problem_statement", ""),
                "qualification_score": str(mapped.get("qualification_score", "")),
                "meeting_booked": str(mapped.get("booking_confirmed", False)),
            },
        }
        if mapped.get("lead_type") == "individual":
            person.pop("company", None)
        return person


register_integration("fub", FollowUpBossIntegration)
