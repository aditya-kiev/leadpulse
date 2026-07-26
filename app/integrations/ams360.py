"""
AMS360 (Vertafore) CRM integration — STUB / NOT YET FUNCTIONAL.

Verification date: 2026-07-26
Source: https://help.vertafore.com/AMS360/content/contextsensitive/download-integration/cswebserviceapisetup.htm
        + Vertafore Orange Partner Program documentation
        + https://affwsapi.ams360.com/v2/service.asmx

DISCREPANCIES vs. real AMS360 API (code below is a placeholder stub):

1. API type mismatch
   Real AMS360 exposes a SOAP/WCF service at:
     https://affwsapi.ams360.com/v2/service.asmx
   The current code assumes REST/JSON at:
     https://api.ams360.com/v1
   These are incompatible protocols. A SOAP client (zeep, suds, or raw
   XML Envelope requests) is required, not httpx JSON POSTs.

2. Authentication mismatch
   Real auth uses a WSAPI Login ID + Password configured inside the
   agency's AMS360 Administration Center (per-tenant credentials, not
   global). The current code sends apiKey/secret to a non-existent
   /authenticate endpoint. For partner-level integrations Vertafore
   requires OAuth2 via the Orange Partner Program (partner_client_id +
   partner_client_secret, password grant, Bearer token cached per
   agency).

3. Commercial prerequisites (not code, but blocking)
   - WSAPI SDK: must be purchased from Vertafore Sales (separate
     license agreement). This is required even to see the WSDL.
   - Vertafore Orange Partner Program: required for third-party
     partner-level OAuth2 credentials. Application-based approval.
   - Test environment: available at additional cost from Vertafore.

4. Data format
   Real API uses SOAP/XML (WSDL-defined contracts) for all entity
   operations (customers, policies, claims, etc.). The current code
   sends JSON payloads that would be rejected.

Action needed before this integration can be used:
  a) Purchase/sign the WSAPI SDK agreement with Vertafore.
  b) Apply for the Orange Partner Program (if partner-level auth is
     desired).
  c) Rewrite the integration using a SOAP client library (e.g. zeep),
     matching the WSDL definitions after SDK access is granted.
  d) Update _AMS360_API_BASE, authentication flow, and payload shape
     to match the real SOAP service.

Until at least (a) is complete, this file cannot be tested against a
real AMS360 instance. The code below exists as a structural placeholder
matching the CRMIntegration base class interface.
"""

import logging
from uuid import UUID

import httpx

from app.config.settings import settings
from app.integrations.base import CRMIntegration, CRMConfig, PushResult
from app.integrations.registry import register_integration

logger = logging.getLogger(__name__)

_AMS360_API_BASE = "https://api.ams360.com/v1"


class AMS360Integration(CRMIntegration):
    integration_type = "ams360"

    def __init__(self, tenant_id: UUID, config: CRMConfig):
        super().__init__(tenant_id, config)
        self._api_key = config.credentials.get("api_key", "")
        self._api_secret = config.credentials.get("api_secret", "")
        self._agency_id = config.credentials.get("agency_id", "")
        self._token = None

    async def connect(self) -> bool:
        if not self._api_key or not self._api_secret:
            logger.error("AMS360 connect failed: missing credentials for tenant=%s", self.tenant_id)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_AMS360_API_BASE}/authenticate",
                    json={"apiKey": self._api_key, "secret": self._api_secret},
                )
                resp.raise_for_status()
                self._token = resp.json().get("token")
                self._connected = True
                return True
        except Exception as e:
            logger.warning("AMS360 connect failed tenant=%s: %s", self.tenant_id, e)
            return False

    async def refresh_token(self) -> bool:
        return await self.connect()

    async def push_lead(self, lead_data: dict) -> PushResult:
        if not self._connected and not await self.connect():
            return PushResult(success=False, status="connect_failed", error_message="Could not connect to AMS360")

        payload = self._build_payload(lead_data)
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if self._agency_id:
                    url = f"{_AMS360_API_BASE}/agencies/{self._agency_id}/clients"
                else:
                    url = f"{_AMS360_API_BASE}/clients"
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                client_id = data.get("id") or data.get("clientId")
                logger.info("AMS360 push success tenant=%s client_id=%s", self.tenant_id, client_id)
                return PushResult(success=True, external_id=str(client_id), status="pushed", raw_response=data)
        except httpx.HTTPStatusError as e:
            logger.error("AMS360 push failed tenant=%s status=%s", self.tenant_id, e.response.status_code)
            return PushResult(success=False, status=f"http_{e.response.status_code}", error_message=e.response.text[:500])
        except Exception as e:
            logger.error("AMS360 push exception tenant=%s: %s", self.tenant_id, e)
            return PushResult(success=False, status="error", error_message=str(e))

    async def pull_status(self, external_id: str) -> dict | None:
        if not self._connected and not await self.connect():
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_AMS360_API_BASE}/clients/{external_id}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("AMS360 pull_status failed tenant=%s: %s", self.tenant_id, e)
            return None

    def _build_payload(self, lead_data: dict) -> dict:
        mapped = self._apply_field_mapping(lead_data)
        full_name = (mapped.get("lead_name") or "").strip()
        parts = full_name.split(" ", 1)
        return {
            "client": {
                "firstName": parts[0] if parts else "",
                "lastName": parts[1] if len(parts) > 1 else "",
                "companyName": mapped.get("company_name", ""),
                "leadSource": "LeadAgent",
                "type": mapped.get("lead_type", "individual"),
                "notes": mapped.get("problem_statement", ""),
                "customFields": {
                    "industry": mapped.get("industry", ""),
                    "budget": mapped.get("budget"),
                    "timeline": mapped.get("timeline"),
                    "qualificationScore": mapped.get("qualification_score"),
                    "leadStatus": mapped.get("lead_status", "new"),
                    "meetingBooked": mapped.get("booking_confirmed", False),
                },
            }
        }


register_integration("ams360", AMS360Integration)
