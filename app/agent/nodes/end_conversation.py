import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, safe_text

logger = logging.getLogger("graph.node.end_conversation")


def create_end_conversation_node(model: ChatGoogleGenerativeAI):
    async def end_conversation_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE end_conversation ENTERED: session=%s lead_status=%s",
                    state.get("session_id"), state.get("lead_status"))

        # CRM sync and hot/booked/handoff alerts are NOT sent from here. This
        # node re-runs on every message after a conversation ends, which used to
        # re-push the lead and re-email the client each time.  They are now
        # dispatched exactly once by app.services.alerts after state is saved.

        response = await model.ainvoke([
            SystemMessage(content=get_prompts(state.get("vertical")).END_CONVERSATION_PROMPT.format(
                lead_name=state.get("lead_name", "there"),
                company_name=state.get("company_name", "your company"),
                lead_status=state.get("lead_status", "new"),
                booking_confirmed=state.get("booking_confirmed", False),
                human_escalated=state.get("human_escalated", False),
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE end_conversation EXIT: session=%s response=%s",
                    state.get("session_id"), log_fingerprint(response_text))

        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "current_node": "end",
            "next_action": None,
            "conversation_stage": "qualified",
        }

    return end_conversation_node
