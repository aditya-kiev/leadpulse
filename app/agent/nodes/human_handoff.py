import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, safe_text
from app.config.settings import settings

logger = logging.getLogger("graph.node.human_handoff")


def create_human_handoff_node(model: ChatGoogleGenerativeAI):
    async def human_handoff_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE human_handoff ENTERED: user_message=%s", log_fingerprint(user_message))

        response = await model.ainvoke([
            SystemMessage(content=get_prompts().HUMAN_HANDOFF_SYSTEM_PROMPT.format(
                confidence=state.get("confidence", 0.0),
                threshold=settings.human_handoff_confidence,
                lead_name=state.get("lead_name", "Unknown"),
                company_name=state.get("company_name", "Unknown"),
                industry=state.get("industry", "Unknown"),
                lead_status=state.get("lead_status", "Unknown"),
                qualification_score=state.get("qualification_score", "N/A"),
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE human_handoff EXIT: response=%s", log_fingerprint(response_text))

        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "human_escalated": True,
            "current_node": "human_handoff",
            "next_action": "end",
        }

    return human_handoff_node
