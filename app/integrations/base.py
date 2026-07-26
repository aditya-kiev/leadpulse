from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


@dataclass
class PushResult:
    success: bool
    external_id: str | None = None
    status: str = "unknown"
    error_message: str | None = None
    raw_response: dict | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CRMConfig:
    integration_type: str
    credentials: dict  # decrypted
    field_mapping: dict | None = None
    is_active: bool = True


class CRMIntegration(ABC):
    integration_type: str = ""

    def __init__(self, tenant_id: UUID, config: CRMConfig):
        self.tenant_id = tenant_id
        self.config = config
        self._connected = False

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def refresh_token(self) -> bool:
        ...

    @abstractmethod
    async def push_lead(self, lead_data: dict) -> PushResult:
        ...

    @abstractmethod
    async def pull_status(self, external_id: str) -> dict | None:
        ...

    async def disconnect(self) -> bool:
        self._connected = False
        return True

    def _apply_field_mapping(self, lead_data: dict) -> dict:
        if not self.config.field_mapping:
            return lead_data
        mapped = {}
        for our_key, crm_key in self.config.field_mapping.items():
            if our_key in lead_data:
                mapped[crm_key] = lead_data[our_key]
        return mapped
