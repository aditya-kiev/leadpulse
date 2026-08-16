import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.nodes.helpers import log_fingerprint, parse_budget, safe_text
from app.config.settings import settings as _settings

logger = logging.getLogger("graph.node.info_collection")

_BASE_FIELD_MAP = {
    "Name": "lead_name",
    "Company": "company_name",
    "Industry": "industry",
    "Budget": "budget",
    "Timeline": "timeline",
    "Problem": "problem_statement",
}

_BASE_FIELD_PRIORITY = [
    "lead_name",
    "company_name",
    "industry",
    "problem_statement",
    "budget",
    "timeline",
]


def _field_map_for(lead_type: str | None) -> dict[str, str]:
    if lead_type == "individual":
        return {k: v for k, v in _BASE_FIELD_MAP.items() if v != "company_name"}
    return dict(_BASE_FIELD_MAP)


def _field_priority_for(lead_type: str | None) -> list[str]:
    if lead_type == "individual":
        return [f for f in _BASE_FIELD_PRIORITY if f != "company_name"]
    return list(_BASE_FIELD_PRIORITY)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if json_match:
        text = json_match.group(1).strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _compute_missing(merged: dict, lead_type: str | None = None) -> list[str]:
    priority = _field_priority_for(lead_type)
    missing = []
    for field in priority:
        val = merged.get(field)
        if field == "budget":
            if val is None:
                missing.append(field)
        elif not val:
            missing.append(field)
    return missing


def _parse_combined_response(text: str) -> tuple[dict, str]:
    """Parse ``EXTRACTED: ...`` and ``REPLY: ...`` from the combined response."""
    extracted: dict = {}
    reply = safe_text(text)

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("EXTRACTED:"):
            json_str = stripped.split(":", 1)[1].strip()
            parsed = _extract_json(json_str)
            if parsed is not None:
                extracted = parsed
        elif stripped.upper().startswith("REPLY:"):
            reply = stripped.split(":", 1)[1].strip()

    return extracted, reply


def create_info_collection_node(model: ChatGoogleGenerativeAI):
    async def info_collection_node(state: AgentState) -> dict:
        user_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        raw_last = user_msgs[-1].content if user_msgs else ""
        user_message = safe_text(raw_last)

        lead_name = state.get("lead_name")
        company_name = state.get("company_name")
        industry = state.get("industry")
        budget = state.get("budget")
        timeline = state.get("timeline")
        problem_statement = state.get("problem_statement")
        lead_type = state.get("lead_type")

        logger.info(
            "NODE info_collection ENTERED: session=%s lead_type=%s user_msg=%s",
            state.get("session_id"), lead_type, log_fingerprint(user_message),
        )

        conv_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in state.get("conversation_history", [])
        )

        merged = {
            "lead_name": lead_name,
            "company_name": company_name,
            "industry": industry,
            "budget": budget,
            "timeline": timeline,
            "problem_statement": problem_statement,
        }

        missing_fields = _compute_missing(merged, lead_type)
        field_map = _field_map_for(lead_type)

        if not missing_fields:
            logger.info("NODE info_collection: all fields present, routing to qualification")
            return {
                "missing_fields": [],
                "next_action": "qualify",
                "current_node": "info_collection",
                "conversation_stage": "qualifying",
                "current_question": None,
            }

        first_missing = missing_fields[0]
        logger.info(
            "NODE info_collection: LLM call 1/1 (combined extraction + question about '%s')",
            first_missing,
        )

        prompt_kwargs = {
            "lead_name": merged["lead_name"] or "not provided",
            "company_name": merged["company_name"] or "not provided",
            "industry": merged["industry"] or "not provided",
            "budget": merged["budget"] or "not provided",
            "timeline": merged["timeline"] or "not provided",
            "problem_statement": merged["problem_statement"] or "not provided",
            "messages": conv_text,
            "input": user_message,
        }
        if lead_type == "individual":
            # Don't waste a turn asking for company_name
            missing = [f for f in [first_missing] if f != "company_name"] or ["industry"]
            prompt_kwargs["missing_fields"] = missing
        else:
            prompt_kwargs["missing_fields"] = [first_missing]

        vertical = state.get("vertical") or _settings.vertical
        response = await model.ainvoke([
            SystemMessage(content=get_prompts(vertical).COMBINED_INFO_COLLECTION_PROMPT.format(**prompt_kwargs)),
            HumanMessage(content=user_message),
        ])
        text = safe_text(response.content)
        logger.info("NODE info_collection: response=%s", log_fingerprint(text))

        updates, reply = _parse_combined_response(text)

        updates_dict: dict = {}
        if updates:
            for label, state_field in field_map.items():
                val = updates.get(label) or updates.get(state_field)
                if val is not None:
                    if state_field == "budget":
                        val = parse_budget(val)
                    updates_dict[state_field] = val

        new_merged = {**merged, **updates_dict}
        new_missing = _compute_missing(new_merged, lead_type)

        logger.info(
            "NODE info_collection EXIT: updates_keys=%s missing=%s reply=%s",
            sorted(updates_dict), new_missing, log_fingerprint(reply),
        )

        return {
            **updates_dict,
            "messages": [AIMessage(content=reply)],
            "conversation_history": [{"role": "assistant", "content": reply}],
            "missing_fields": new_missing,
            "current_node": "info_collection",
            "next_action": "collect_info",
            "conversation_stage": "collecting",
            "current_question": first_missing if new_missing else None,
        }

    return info_collection_node
