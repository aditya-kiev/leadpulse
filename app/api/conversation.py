import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import authenticate_request, get_current_user
from app.database.crud import get_conversation, get_conversations_by_tenant
from app.database.session import async_session_factory
from app.models.schemas import ConversationHistoryOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversation", tags=["conversation"])


@router.get("/{session_id}", response_model=ConversationHistoryOut)
async def get_conversation_history(
    request: Request,
    session_id: str,
    _auth: tuple = Depends(authenticate_request),
) -> ConversationHistoryOut:
    tenant_id: UUID | None = getattr(request.state, "tenant_id", None)
    async with async_session_factory() as db_session:
        lead = await get_conversation(db_session, session_id, tenant_id=tenant_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return ConversationHistoryOut(
            session_id=lead.session_id,
            lead_name=lead.lead_name,
            company_name=lead.company_name,
            industry=lead.industry,
            budget=lead.budget,
            timeline=lead.timeline,
            problem_statement=lead.problem_statement,
            qualification_score=lead.qualification_score,
            lead_status=lead.lead_status,
            booking_confirmed=lead.booking_confirmed,
            meeting_time=lead.meeting_time,
            human_escalated=lead.human_escalated,
            conversation_history=lead.conversation_history,
            created_at=lead.created_at,
            updated_at=lead.updated_at,
        )


@router.get("/", response_model=list[ConversationHistoryOut])
async def list_conversations(
    request: Request,
    _auth: tuple = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0,
) -> list[ConversationHistoryOut]:
    tenant_id: UUID | None = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")

    async with async_session_factory() as db_session:
        leads = await get_conversations_by_tenant(db_session, tenant_id, limit=limit, offset=offset)
        return [
            ConversationHistoryOut(
                session_id=lead.session_id,
                lead_name=lead.lead_name,
                company_name=lead.company_name,
                industry=lead.industry,
                budget=lead.budget,
                timeline=lead.timeline,
                problem_statement=lead.problem_statement,
                qualification_score=lead.qualification_score,
                lead_status=lead.lead_status,
                booking_confirmed=lead.booking_confirmed,
                meeting_time=lead.meeting_time,
                human_escalated=lead.human_escalated,
                conversation_history=lead.conversation_history,
                created_at=lead.created_at,
                updated_at=lead.updated_at,
            )
            for lead in leads
        ]
