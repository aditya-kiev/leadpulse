import logging
from uuid import UUID

import httpx

from app.config.settings import settings
from app.integrations.base import CRMIntegration, CRMConfig, PushResult
from app.integrations.registry import register_integration

logger = logging.getLogger(__name__)

_KVCORE_API_BASE = "https://api.kvcore.com/v2"


class KVCoreIntegration(CRMIntegration):
    integration_type = "kvcore"

    def __init__(self, tenant_id: UUID, config: CRMConfig):
        super().__init__(tenant_id, config)
        self._api_key = config.credentials.get("api_key", "")
        self._api_secret = config.credentials.get("api_secret", "")
        self._access_token = config.credentials.get("access_token")
        self._refresh_token_val = config.credentials.get("refresh_token")
        self._company_id = config.credentials.get("company_id", "")

    async def connect(self) -> bool:
        # kvCORE uses OAuth2 client-credentials or API key
        if self._access_token:
            self._connected = True
            return True
        if self._api_key and self._api_secret:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{_KVCORE_API_BASE}/oauth/token",
                        json={
                            "grant_type": "client_credentials",
                            "client_id": self._api_key,
                            "client_secret": self._api_secret,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self._access_token = data.get("access_token")
                        self._refresh_token_val = data.get("refresh_token")
                        self._connected = True
                        return True
                    logger.warning("kvCORE OAuth failed tenant=%s status=%s", self.tenant_id, resp.status_code)
                    return False
            except Exception as e:
                logger.warning("kvCORE connect failed tenant=%s: %s", self.tenant_id, e)
                return False
        logger.warning("kvCORE no credentials for tenant=%s", self.tenant_id)
        return False

    async def refresh_token(self) -> bool:
        if not self._refresh_token_val:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_KVCORE_API_BASE}/oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token_val,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token_val = data.get("refresh_token")
                    self._connected = True
                    return True
                return False
        except Exception:
            return False

    async def push_lead(self, lead_data: dict) -> PushResult:
        if not self._connected:
            ok = await self.connect()
            if not ok:
                return PushResult(success=False, status="connect_failed", error_message="Could not connect to kvCORE")

        if not self._company_id:
            return PushResult(
                success=False, status="no_company_id",
                error_message="kvCORE company_id not configured. "
                "Your kvCORE plan may not include API access — "
                "use the webhook integration as a fallback.",
            )

        payload = self._build_payload(lead_data)
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_KVCORE_API_BASE}/companies/{self._company_id}/leads",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 401:
                    refreshed = await self.refresh_token()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {self._access_token}"
                        resp = await client.post(
                            f"{_KVCORE_API_BASE}/companies/{self._company_id}/leads",
                            json=payload,
                            headers=headers,
                        )
                resp.raise_for_status()
                data = resp.json()
                lead_id = data.get("id") or data.get("data", {}).get("id")
                logger.info("kvCORE push success tenant=%s lead_id=%s", self.tenant_id, lead_id)
                return PushResult(success=True, external_id=str(lead_id), status="pushed", raw_response=data)
        except httpx.HTTPStatusError as e:
            logger.error("kvCORE push failed tenant=%s status=%s", self.tenant_id, e.response.status_code)
            return PushResult(success=False, status=f"http_{e.response.status_code}", error_message=e.response.text[:500])
        except Exception as e:
            logger.error("kvCORE push exception tenant=%s: %s", self.tenant_id, e)
            return PushResult(success=False, status="error", error_message=str(e))

    async def pull_status(self, external_id: str) -> dict | None:
        if not self._connected and not await self.connect():
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_KVCORE_API_BASE}/leads/{external_id}",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("kvCORE pull_status failed tenant=%s: %s", self.tenant_id, e)
            return None

    def _build_payload(self, lead_data: dict) -> dict:
        mapped = self._apply_field_mapping(lead_data)
        return {
            "lead": {
                "firstName": (mapped.get("lead_name") or "").split(" ")[0],
                "lastName": " ".join((mapped.get("lead_name") or "").split(" ")[1:]),
                "company": mapped.get("company_name"),
                "source": "LeadAgent",
                "tags": ["lead-agent", mapped.get("lead_status", "new")],
                "customFields": {
                    "industry": mapped.get("industry"),
                    "budget": mapped.get("budget"),
                    "timeline": mapped.get("timeline"),
                    "notes": mapped.get("problem_statement"),
                    "score": mapped.get("qualification_score"),
                },
            }
        }


register_integration("kvcore", KVCoreIntegration)
