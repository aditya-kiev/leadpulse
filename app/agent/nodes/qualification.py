import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts.templates import get_prompts
from app.agent.state import AgentState
from app.agent.tools.lead_scoring import compute_lead_score
from app.agent.nodes.helpers import safe_text
from app.config.settings import settings
from app.models.schemas import IntentType, LeadScoreIn

logger = logging.getLogger("graph.node.qualification")

_BASE_REQUIRED_FIELDS = [
    "lead_name",
    "company_name",
    "industry",
    "budget",
    "timeline",
    "problem_statement",
]


def _required_fields_for(lead_type: str | None) -> list[str]:
    if lead_type == "individual":
        return [f for f in _BASE_REQUIRED_FIELDS if f != "company_name"]
    return list(_BASE_REQUIRED_FIELDS)


def _all_fields_populated(state: AgentState) -> bool:
    required = _required_fields_for(state.get("lead_type"))
    for field in required:
        val = state.get(field)
        if field == "budget":
            if val is None:
                return False
        elif not val:
            return False
    return True


def create_qualification_node(model: ChatGoogleGenerativeAI):
    async def qualification_node(state: AgentState) -> dict:
        logger.debug("qualification_node session=%s", state.get("session_id"))

        if state.get("lead_status") is not None:
            return {
                "current_node": "qualification",
                "conversation_stage": "qualified",
                "next_action": "handle_next",
            }

        if not _all_fields_populated(state):
            required = _required_fields_for(state.get("lead_type"))
            missing = [f for f in required if not state.get(f)]
            return {
                "missing_fields": missing,
                "next_action": "collect_info",
                "current_node": "qualification",
                "conversation_stage": "collecting",
            }

        vertical = state.get("vertical") or settings.vertical
        score_data = LeadScoreIn(
            budget=state.get("budget"),
            timeline=state.get("timeline"),
            industry=state.get("industry"),
            problem_statement=state.get("problem_statement"),
            intent=IntentType(state.get("lead_intent") or "unknown"),
            lead_type=state.get("lead_type"),
            vertical=vertical,
        )

        score_result = compute_lead_score(score_data)
        logger.debug("qualification_node score=%s status=%s", score_result.score, score_result.status.value)

        context = "\n".join(
            f"{m['role']}: {m['content']}" for m in state.get("conversation_history", [])
        ) if state.get("conversation_history") else ""

        response = await model.ainvoke([
            SystemMessage(content=get_prompts(vertical).QUALIFICATION_SYSTEM_PROMPT.format(
                lead_name=state.get("lead_name", "Unknown"),
                company_name=state.get("company_name", "Unknown"),
                industry=state.get("industry", "Unknown"),
                budget=state.get("budget", "Unknown"),
                timeline=state.get("timeline", "Unknown"),
                problem_statement=state.get("problem_statement", "Unknown"),
                lead_intent=state.get("lead_intent") or "unknown",
            )),
            HumanMessage(content=f"Lead context:\n{context}\n\nGenerate a qualification summary for this lead."),
        ])
        response_text = safe_text(response.content)

        return {
            "qualification_score": score_result.score,
            "lead_status": score_result.status.value,
            "messages": [AIMessage(content=response.content)],
            "current_node": "qualification",
            "next_action": "handle_next",
            "conversation_stage": "qualified",
        }

    return qualification_node
