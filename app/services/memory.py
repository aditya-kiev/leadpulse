import logging
from uuid import UUID

from app.database.crud import get_conversation, update_conversation, create_conversation
from app.database.session import async_session_factory

logger = logging.getLogger(__name__)


class ConversationMemory:
    async def save_state(self, session_id: str, state: dict, tenant_id: UUID | None = None) -> None:
        logger.debug("save_state session=%s tenant_id=%s", session_id, tenant_id)
        try:
            async with async_session_factory() as db_session:
                existing = await get_conversation(db_session, session_id, tenant_id=tenant_id)
                if not existing:
                    await create_conversation(db_session, session_id, tenant_id=tenant_id)

                await update_conversation(
                    db_session,
                    session_id,
                    tenant_id=tenant_id,
                    lead_name=state.get("lead_name"),
                    company_name=state.get("company_name"),
                    industry=state.get("industry"),
                    budget=state.get("budget"),
                    timeline=state.get("timeline"),
                    problem_statement=state.get("problem_statement"),
                    qualification_score=state.get("qualification_score"),
                    lead_status=state.get("lead_status"),
                    lead_intent=state.get("lead_intent"),
                    lead_type=state.get("lead_type"),
                    booking_confirmed=state.get("booking_confirmed", False),
                    meeting_time=state.get("meeting_time"),
                    conversation_history=state.get("conversation_history"),
                    conversation_stage=state.get("conversation_stage"),
                    current_node=state.get("current_node"),
                    human_escalated=state.get("human_escalated", False),
                )
                logger.debug("save_state OK session=%s", session_id)
        except Exception as e:
            logger.warning("save_state failed session=%s: %s", session_id, str(e))
            raise

    async def load_state(self, session_id: str, tenant_id: UUID | None = None) -> dict | None:
        logger.debug("load_state session=%s tenant_id=%s", session_id, tenant_id)
        try:
            async with async_session_factory() as db_session:
                lead = await get_conversation(db_session, session_id, tenant_id=tenant_id)
                if not lead:
                    logger.debug("load_state: not found session=%s", session_id)
                    return None
                return {
                    "lead_name": lead.lead_name,
                    "company_name": lead.company_name,
                    "industry": lead.industry,
                    "budget": lead.budget,
                    "timeline": lead.timeline,
                    "problem_statement": lead.problem_statement,
                    "qualification_score": lead.qualification_score,
                    "lead_status": lead.lead_status,
                    "lead_intent": lead.lead_intent,
                    "lead_type": lead.lead_type,
                    "booking_confirmed": lead.booking_confirmed,
                    "meeting_time": lead.meeting_time.isoformat() if lead.meeting_time else None,
                    "conversation_history": lead.conversation_history or [],
                    "conversation_stage": lead.conversation_stage,
                    "current_node": lead.current_node,
                    "human_escalated": lead.human_escalated,
                }
        except Exception as e:
            logger.warning("load_state failed session=%s: %s", session_id, str(e))
            raise


memory_service = ConversationMemory()
