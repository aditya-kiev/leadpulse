import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import safe_text
from app.agent.tools.crm import update_crm

logger = logging.getLogger("graph.node.end_conversation")


def create_end_conversation_node(model: ChatGoogleGenerativeAI):
    async def end_conversation_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE end_conversation ENTERED: session=%s tenant=%s lead_status=%s",
                    state.get("session_id"), state.get("tenant_id"), state.get("lead_status"))

        lead_data = {
            k: state.get(k) for k in [
                "lead_name", "company_name", "industry", "budget", "timeline",
                "problem_statement", "qualification_score", "lead_status",
                "meeting_time", "booking_confirmed", "human_escalated",
            ]
        }
        update_crm(state["session_id"], lead_data, tenant_id=state.get("tenant_id"))
        logger.info("NODE end_conversation: CRM updated")

        response = await model.ainvoke([
            SystemMessage(content=get_prompts().END_CONVERSATION_PROMPT.format(
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
                    state.get("session_id"), response_text[:60])

        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "current_node": "end",
            "next_action": None,
            "conversation_stage": "qualified",
        }

    return end_conversation_node
