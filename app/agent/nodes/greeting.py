import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, safe_text
from app.config.settings import settings as _settings

logger = logging.getLogger("graph.node.greeting")


def _parse_combined_response(text: str, vertical: str = "generic") -> tuple[str, str, str]:
    """Extract intent, lead_type, and reply from the combined prompt response."""
    intent = "unknown"
    lead_type = "individual" if vertical in ("real_estate", "insurance") else "company"
    reply = text.strip()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("INTENT:"):
            raw = stripped.split(":", 1)[1].strip().lower()
            for candidate in ["purchase", "sell", "information", "support", "partnership"]:
                if candidate in raw:
                    intent = candidate
                    break
        elif stripped.upper().startswith("LEAD_TYPE:"):
            raw = stripped.split(":", 1)[1].strip().lower()
            if "individual" in raw:
                lead_type = "individual"
            elif "company" in raw or "business" in raw:
                lead_type = "company"
        elif stripped.upper().startswith("REPLY:"):
            reply = stripped.split(":", 1)[1].strip()

    return intent, lead_type, reply


def create_greeting_node(model: ChatGoogleGenerativeAI):
    async def greeting_node(state: AgentState) -> dict:
        # Returning user guard: skip if mid-conversation
        if any(m.get("role") == "assistant" for m in state.get("conversation_history") or []):
            logger.debug("greeting_node: returning user, skipping")
            return {}

        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)
        logger.info("NODE greeting ENTERED: session=%s user_msg=%s",
                    state.get("session_id"), log_fingerprint(user_message))

        known_info = {
            k: v for k, v in {
                "name": state.get("lead_name"),
                "company": state.get("company_name"),
                "industry": state.get("industry"),
            }.items() if v
        }

        business_name = state.get("business_name") or _settings.business_name
        vertical = state.get("vertical") or _settings.vertical

        logger.info("NODE greeting: LLM call 1/1 (combined intent + generation)")
        response = await model.ainvoke([
            SystemMessage(content=get_prompts(vertical).COMBINED_GREETING_PROMPT.format(
                company_name=business_name,
                lead_status=state.get("lead_status", "new"),
                known_info=str(known_info) if known_info else "none yet",
                input=user_message,
            )),
            HumanMessage(content=user_message),
        ])
        text = safe_text(response.content)
        logger.info("NODE greeting: response=%s", log_fingerprint(text))

        lead_intent, lead_type, reply = _parse_combined_response(text, vertical)

        logger.info("NODE greeting EXIT: session=%s lead_intent=%s lead_type=%s",
                    state.get("session_id"), lead_intent, lead_type)
        return {
            "messages": [AIMessage(content=reply)],
            "conversation_history": [
                {"role": "assistant", "content": reply},
            ],
            "lead_intent": lead_intent,
            "lead_type": lead_type,
            "current_node": "greeting",
            "next_action": "collect_info",
            "conversation_stage": "collecting",
        }

    return greeting_node
