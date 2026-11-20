import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.booking_intent import (
    OFFERED_SLOT_LIMIT,
    BookingDecision,
    booking_status_instruction,
    resolve_booking,
)
from app.agent.state import AgentState
from app.agent.tools.calendar import get_available_slots
from app.agent.nodes.helpers import log_fingerprint, safe_text
from app.config.settings import settings

logger = logging.getLogger("graph.node.meeting_booking")


def _last_assistant_text(state: AgentState) -> str | None:
    for msg in reversed(state.get("conversation_history") or []):
        if msg.get("role") == "assistant":
            return msg.get("content")
    return None


def create_meeting_booking_node(model: ChatGoogleGenerativeAI):
    async def meeting_booking_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE meeting_booking ENTERED: user_message=%s", log_fingerprint(user_message))

        slots = await get_available_slots(settings.calendar_availability_days)
        offered = slots[:OFFERED_SLOT_LIMIT]
        slot_labels = "\n".join(s["label"] for s in offered)

        # Decide FIRST (deterministically), then let the model write a reply that
        # matches the decision.  Slots can only be confirmed once we've actually
        # offered some, i.e. on a turn after we first entered this node.
        if state.get("current_node") == "meeting_booking":
            decision = resolve_booking(
                user_message,
                offered,
                last_assistant_text=_last_assistant_text(state),
            )
        else:
            decision = BookingDecision(False, None, "no_proposal")

        system_prompt = get_prompts(state.get("vertical")).MEETING_BOOKING_SYSTEM_PROMPT.format(
            lead_name=state.get("lead_name", "there"),
            company_name=state.get("company_name", "your company"),
            lead_status=state.get("lead_status", "new"),
            days=settings.calendar_availability_days,
            available_slots=slot_labels,
            input=user_message,
        ) + "\n\n" + booking_status_instruction(decision, [s["label"] for s in offered])

        response = await model.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE meeting_booking EXIT: response=%s", log_fingerprint(response_text))

        confirmed = decision.confirmed
        meeting_time = decision.meeting_time

        logger.info("NODE meeting_booking: confirmed=%s meeting_time=%s", confirmed, meeting_time)
        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "booking_confirmed": confirmed,
            "meeting_time": meeting_time,
            "current_node": "meeting_booking",
            "next_action": "end" if confirmed else "meeting_booking",
        }

    return meeting_booking_node
