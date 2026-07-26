from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import add_messages


class AgentState(TypedDict):
    session_id: str
    channel: str
    tenant_id: str | None

    lead_name: str | None
    company_name: str | None
    industry: str | None
    budget: float | None
    timeline: str | None
    problem_statement: str | None

    qualification_score: float | None
    lead_status: str | None
    lead_intent: str | None
    lead_type: str | None

    messages: Annotated[list[dict[str, Any]], add_messages]
    conversation_history: Annotated[list[dict[str, Any]], operator.add]

    current_node: str
    next_action: str | None
    conversation_stage: str
    booking_confirmed: bool
    meeting_time: str | None
    human_escalated: bool
    objection_type: str | None

    missing_fields: list[str]
    confidence: float
    iteration_count: int
    current_question: str | None


def get_initial_state(session_id: str, channel: str = "web", tenant_id: str | None = None) -> AgentState:
    return {
        "session_id": session_id,
        "channel": channel,
        "tenant_id": tenant_id,
        "lead_name": None,
        "company_name": None,
        "industry": None,
        "budget": None,
        "timeline": None,
        "problem_statement": None,
        "qualification_score": None,
        "lead_status": None,
        "lead_intent": None,
        "lead_type": None,
        "messages": [],
        "conversation_history": [],
        "current_node": "greeting",
        "next_action": None,
        "conversation_stage": "greeting",
        "booking_confirmed": False,
        "meeting_time": None,
        "human_escalated": False,
        "objection_type": None,
        "missing_fields": [],
        "confidence": 1.0,
        "iteration_count": 0,
        "current_question": None,
    }
