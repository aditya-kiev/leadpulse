import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.state import AgentState, get_initial_state
from app.agent.gemini import RetryingGeminiModel, gemini_call_counter, get_last_usage, estimate_gemini_cost
from app.agent.nodes.greeting import create_greeting_node
from app.agent.nodes.info_collection import create_info_collection_node
from app.agent.nodes.qualification import create_qualification_node
from app.agent.nodes.faq import create_faq_node
from app.agent.nodes.objection_handling import create_objection_handling_node
from app.agent.nodes.meeting_booking import create_meeting_booking_node
from app.agent.nodes.human_handoff import create_human_handoff_node
from app.agent.nodes.end_conversation import create_end_conversation_node
from app.agent.tools.objection_detection import detect_objection
from app.config.settings import settings
from app.services.memory import memory_service

_ALL_NODES = frozenset({
    "greeting", "info_collection", "qualification", "faq",
    "objection_handling", "meeting_booking", "human_handoff",
    "end", "handle_next",
})

logger = logging.getLogger(__name__)
_node_logger = logging.getLogger("graph.node")


def route_after_greeting(state: AgentState) -> str:
    intent = state.get("lead_intent", "unknown")
    _node_logger.debug("route_after_greeting: intent=%s", intent)
    if intent == "support":
        return "faq"
    return "info_collection"


def route_after_info_collection(state: AgentState) -> str:
    missing = state.get("missing_fields", [])
    _node_logger.debug("route_after_info_collection: missing=%s", missing)
    if missing:
        return END
    return "qualification"


def route_after_qualification(state: AgentState) -> str:
    _node_logger.debug(
        "route_after_qualification: lead_status=%s", state.get("lead_status")
    )
    return "handle_next"


def route_next_action(state: AgentState) -> str:
    confidence = state.get("confidence", 1.0)
    _node_logger.debug(
        "route_next_action: confidence=%s booking=%s escalated=%s next=%s status=%s objection=%s",
        confidence,
        state.get("booking_confirmed"),
        state.get("human_escalated"),
        state.get("next_action"),
        state.get("lead_status"),
        state.get("objection_type"),
    )

    if confidence < settings.human_handoff_confidence:
        return "human_handoff"

    if state.get("human_escalated"):
        return "end"

    booking = state.get("booking_confirmed", False)
    next_action = state.get("next_action", "")

    if next_action == "end" or booking:
        return "end"

    if state.get("objection_type"):
        return "objection_handling"

    if next_action == "meeting_booking":
        return "meeting_booking"

    lead_status = state.get("lead_status", "")
    if lead_status in ("hot", "warm"):
        return "meeting_booking"
    if lead_status == "cold":
        return "end"
    return END


def route_after_objection(state: AgentState) -> str:
    _node_logger.debug(
        "route_after_objection: escalated=%s booking=%s status=%s",
        state.get("human_escalated"),
        state.get("booking_confirmed"),
        state.get("lead_status"),
    )
    if state.get("human_escalated"):
        return "human_handoff"
    if state.get("booking_confirmed"):
        return "end"
    lead_status = state.get("lead_status", "")
    if lead_status in ("hot", "warm"):
        return "meeting_booking"
    if lead_status == "cold":
        return "end"
    return END


def route_after_meeting(state: AgentState) -> str:
    _node_logger.debug(
        "route_after_meeting: booking=%s", state.get("booking_confirmed")
    )
    if state.get("booking_confirmed"):
        return "end"
    return END


def create_handle_next_node(model: ChatGoogleGenerativeAI):
    async def handle_next_node(state: AgentState) -> dict:
        user_msgs = [
            m for m in state.get("conversation_history", []) if m.get("role") == "user"
        ]
        last_user_msg = user_msgs[-1]["content"] if user_msgs else ""
        result = await detect_objection(last_user_msg, model)
        if result.has_objection:
            return {"objection_type": result.objection_type}
        return {"objection_type": None}

    return handle_next_node


def get_entry_point(state: AgentState) -> str:
    """Resume from the last active node on subsequent turns."""
    current_node = state.get("current_node")
    if current_node in _ALL_NODES:
        return current_node
    return "greeting"


def build_graph() -> CompiledStateGraph:
    logger.debug("building graph with model=%s", settings.gemini_model)

    model = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=settings.gemini_temperature,
        api_key=settings.gemini_api_key,
        timeout=settings.gemini_timeout,
    )
    model = RetryingGeminiModel(model)

    workflow = StateGraph(AgentState)

    workflow.add_node("greeting", create_greeting_node(model))
    workflow.add_node("info_collection", create_info_collection_node(model))
    workflow.add_node("qualification", create_qualification_node(model))
    workflow.add_node("faq", create_faq_node(model))
    workflow.add_node("objection_handling", create_objection_handling_node(model))
    workflow.add_node("meeting_booking", create_meeting_booking_node(model))
    workflow.add_node("human_handoff", create_human_handoff_node(model))
    workflow.add_node("end", create_end_conversation_node(model))
    workflow.add_node("handle_next", create_handle_next_node(model))

    workflow.set_conditional_entry_point(get_entry_point)

    workflow.add_conditional_edges(
        "greeting",
        route_after_greeting,
        {"info_collection": "info_collection", "faq": "faq"},
    )

    workflow.add_conditional_edges(
        "info_collection",
        route_after_info_collection,
        {END: END, "qualification": "qualification"},
    )

    workflow.add_conditional_edges(
        "qualification",
        route_after_qualification,
        {"handle_next": "handle_next"},
    )

    workflow.add_conditional_edges(
        "handle_next",
        route_next_action,
        {
            END: END,
            "objection_handling": "objection_handling",
            "meeting_booking": "meeting_booking",
            "human_handoff": "human_handoff",
            "faq": "faq",
            "end": "end",
        },
    )

    workflow.add_conditional_edges(
        "faq",
        route_after_info_collection,
        {END: END, "qualification": "qualification"},
    )

    workflow.add_conditional_edges(
        "objection_handling",
        route_after_objection,
        {
            END: END,
            "meeting_booking": "meeting_booking",
            "human_handoff": "human_handoff",
            "end": "end",
        },
    )

    workflow.add_conditional_edges(
        "meeting_booking",
        route_after_meeting,
        {"end": "end", END: END},
    )

    workflow.add_edge("human_handoff", "end")
    workflow.add_edge("end", END)

    memory = MemorySaver()

    graph = workflow.compile(checkpointer=memory)
    logger.debug("graph compiled OK")
    return graph


_agent_graph: CompiledStateGraph | None = None


def get_graph() -> CompiledStateGraph:
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_graph()
    return _agent_graph


async def run_agent(
    session_id: str,
    message: str,
    channel: str = "web",
    tenant_id: UUID | None = None,
) -> dict:
    logger.debug("run_agent session=%s channel=%s tenant_id=%s", session_id, channel, tenant_id)

    # Tenant-qualified thread_id prevents cross-tenant checkpoint leakage in
    # the in-memory MemorySaver (used within a single ainvoke for reducer
    # consistency).  The Postgres-backed memory_service is the actual source
    # of truth for turn-to-turn state resumption.
    thread_key = f"{tenant_id}:{session_id}" if tenant_id else session_id
    config: Any = {"configurable": {"thread_id": thread_key}}

    # Source of truth for turn-to-turn state resumption: Postgres via memory_service.
    # On every incoming message we load persisted state (lead fields, conversation_stage,
    # current_node, etc.) and merge it into the initial state dict. This survives process
    # restarts and multiple workers (e.g. Railway deployment). The LangGraph MemorySaver
    # checkpointer is NOT relied upon across HTTP requests — it only provides within-
    # ainvoke consistency for the additive reducers (operator.add on conversation_history).
    #
    # BUG PREVENTION: the process-wide MemorySaver holds a checkpoint keyed by
    # thread_id=session_id.  On a subsequent turn we reload the full conversation_history
    # from Postgres and feed it into turn_input.  If we keep the old checkpoint, the
    # operator.add reducer concatenates the checkpoint's history (already accumulated
    # from the previous ainvoke) with the freshly Postgres-loaded history, producing
    # duplicate entries that compound every turn.
    #
    # Fix: delete the checkpoint for this thread BEFORE every call.  The graph starts
    # fresh from our Postgres-reconstituted turn_input, and within ainvoke the
    # MemorySaver still provides consistency across nodes in the same turn.
    graph = get_graph()
    if graph.checkpointer is not None:
        await graph.checkpointer.adelete_thread(thread_key)

    tenant_id_str = str(tenant_id) if tenant_id else None
    turn_input = get_initial_state(session_id, channel, tenant_id=tenant_id_str)
    turn_input["messages"] = [{"role": "user", "content": message}]

    persisted = await memory_service.load_state(session_id, tenant_id=tenant_id)
    if persisted:
        logger.debug(
            "merged %d persisted keys for session %s tenant=%s", len(persisted), session_id, tenant_id
        )
        for key, value in persisted.items():
            if value is not None and key in turn_input:
                turn_input[key] = value

    now = datetime.now(timezone.utc).isoformat()
    turn_input["conversation_history"] = (
        turn_input.get("conversation_history") or []
    ) + [{"role": "user", "content": message, "timestamp": now}]

    result = await graph.ainvoke(turn_input, config)
    result["tenant_id"] = tenant_id_str
    gemini_calls = gemini_call_counter.get()
    usage = get_last_usage()
    if usage:
        prompt_tokens = usage.get("prompt_token_count") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("candidates_token_count") or usage.get("output_tokens") or 0
        total_tokens = usage.get("total_token_count") or usage.get("total_tokens") or 0
        estimated_cost = estimate_gemini_cost(prompt_tokens, completion_tokens)
        try:
            from app.database.crud import log_usage
            await log_usage(
                organization_id=tenant_id,
                session_id=session_id,
                model=settings.gemini_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost=estimated_cost,
            )
        except Exception as log_err:
            logger.warning("usage logging failed session=%s: %s", session_id, log_err)

    logger.debug(
        "run_agent complete: lead_status=%s stage=%s gemini_calls=%s",
        result.get("lead_status"),
        result.get("conversation_stage"),
        gemini_calls,
    )
    return result
