import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, safe_text
from app.config.settings import settings as _settings

logger = logging.getLogger("graph.node.faq")


def create_faq_node(model: ChatGoogleGenerativeAI):
    async def faq_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE faq ENTERED: user_message=%s", log_fingerprint(user_message))

        response = await model.ainvoke([
            SystemMessage(content=get_prompts().FAQ_SYSTEM_PROMPT.format(
                company_name=_settings.business_name,
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE faq EXIT: response=%s", log_fingerprint(response_text))

        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "current_node": "faq",
            "next_action": "collect_info",
        }

    return faq_node
