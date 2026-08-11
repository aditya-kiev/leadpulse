import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.tools.calendar import get_available_slots
from app.agent.nodes.helpers import log_fingerprint, safe_text
from app.config.settings import settings

logger = logging.getLogger("graph.node.meeting_booking")


def create_meeting_booking_node(model: ChatGoogleGenerativeAI):
    async def meeting_booking_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE meeting_booking ENTERED: user_message=%s", log_fingerprint(user_message))

        slots = await get_available_slots(settings.calendar_availability_days)
        slot_labels = "\n".join(s["label"] for s in slots[:9])

        response = await model.ainvoke([
            SystemMessage(content=get_prompts().MEETING_BOOKING_SYSTEM_PROMPT.format(
                lead_name=state.get("lead_name", "there"),
                company_name=state.get("company_name", "your company"),
                lead_status=state.get("lead_status", "new"),
                days=settings.calendar_availability_days,
                available_slots=slot_labels,
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE meeting_booking EXIT: response=%s", log_fingerprint(response_text))

        confirmed = False
        meeting_time = None
        user_text = str(user_message).lower()
        if any(word in user_text for word in ["yes", "confirm", "book", "schedule", "sure", "sounds good", "that works"]):
            for slot in slots:
                if slot["label"].lower() in user_text or slot["datetime"][:10] in user_text:
                    confirmed = True
                    meeting_time = slot["datetime"]
                    break
            if not confirmed and state.get("current_node") == "meeting_booking" and "?" not in user_text:
                confirmed = True
                meeting_time = slots[0]["datetime"] if slots else None

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
