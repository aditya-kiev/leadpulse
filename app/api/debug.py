import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import authenticate_request, get_optional_tenant_id
from app.models.schemas import DebugStateOut
from app.services.memory import memory_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/state/{session_id}", response_model=DebugStateOut)
async def get_debug_state(
    session_id: str,
    _auth: tuple = Depends(authenticate_request),
    tenant_id: UUID | None = Depends(get_optional_tenant_id),
) -> DebugStateOut:
    state = await memory_service.load_state(session_id, tenant_id=tenant_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    return DebugStateOut(
        session_id=session_id,
        lead_name=state.get("lead_name"),
        company_name=state.get("company_name"),
        industry=state.get("industry"),
        budget=state.get("budget"),
        timeline=state.get("timeline"),
        problem_statement=state.get("problem_statement"),
        lead_status=state.get("lead_status"),
        qualification_score=state.get("qualification_score"),
        missing_fields=[],
        conversation_stage=state.get("conversation_stage", "unknown"),
        next_action=state.get("next_action"),
        current_question=None,
        conversation_history_len=len(state.get("conversation_history", [])),
    )
