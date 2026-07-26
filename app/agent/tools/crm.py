_CRM_LEADS: dict[str, dict] = {}


def _crm_key(session_id: str, tenant_id: str | None = None) -> str:
    return f"{tenant_id}:{session_id}" if tenant_id else session_id


def update_crm(session_id: str, lead_data: dict, tenant_id: str | None = None) -> dict:
    key = _crm_key(session_id, tenant_id)
    _CRM_LEADS[key] = {
        **_CRM_LEADS.get(key, {}),
        **lead_data,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "updated_at": __import__("datetime").datetime.utcnow().isoformat(),
    }
    return _CRM_LEADS[key]


def get_crm_lead(session_id: str, tenant_id: str | None = None) -> dict | None:
    return _CRM_LEADS.get(_crm_key(session_id, tenant_id))
