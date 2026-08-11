import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, safe_text

logger = logging.getLogger("graph.node.objection_handling")

_OBJECTION_TYPES = frozenset({
    "pricing", "timing", "trust", "competition", "need", "authority",
})


def create_objection_handling_node(model: ChatGoogleGenerativeAI):
    async def objection_handling_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE objection_handling ENTERED: user_message=%s", log_fingerprint(user_message))

        # Read objection_type already computed by handle_next (keyword pre-filter
        # or LLM) — no need to re-detect from scratch.
        objection_type = state.get("objection_type")
        if not objection_type or objection_type not in _OBJECTION_TYPES:
            logger.info("NODE objection_handling: no valid objection_type in state")
            return {
                "objection_type": None,
                "next_action": "collect_info",
                "current_node": "objection_handling",
            }

        response = await model.ainvoke([
            SystemMessage(content=get_prompts().OBJECTION_HANDLING_SYSTEM_PROMPT.format(
                objection_type=objection_type,
                lead_name=state.get("lead_name", "there"),
                company_name=state.get("company_name", "your company"),
                industry=state.get("industry", "your industry"),
                budget=state.get("budget", "not specified"),
                timeline=state.get("timeline", "not specified"),
                problem_statement=state.get("problem_statement", "not specified"),
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        response_text = safe_text(response.content)
        logger.info("NODE objection_handling EXIT: type=%s response=%s", objection_type, log_fingerprint(response_text))

        return {
            "messages": [AIMessage(content=response.content)],
            "conversation_history": [{"role": "assistant", "content": response_text}],
            "objection_type": objection_type,
            "current_node": "objection_handling",
            "next_action": "collect_info",
            "confidence": state.get("confidence", 1.0) - 0.05,
        }

    return objection_handling_node
