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


async def resolve_tenant_gemini_key(tenant_id: UUID | None) -> str | None:
    """Return the tenant's own Gemini API key from crm_configs, or None.

    Falls back to ``settings.gemini_api_key`` (the platform key) when the
    tenant has no ``gemini`` crm_configs row, the row can't be decrypted, or
    no tenant context is available.  Never raises — a resolution failure
    degrades to the platform key so the agent keeps working.
    """
    cfg = await resolve_tenant_config(tenant_id)
    return (cfg or {}).get("api_key") or settings.gemini_api_key


async def resolve_tenant_config(tenant_id: UUID | None) -> dict:
    """Return the tenant's decrypted ``gemini`` crm_configs row, or {}.

    onboard_client stores the per-tenant Gemini key together with the
    tenant's ``vertical`` and ``business_name`` in this row.  Previously only
    ``api_key`` was ever read, so prompts and lead scoring ran on the global
    ``settings.vertical`` / ``settings.business_name`` for every tenant in the
    process — the per-tenant vertical/business_name were dead data.

    Returns the full decrypted config dict (``api_key``, ``vertical``,
    ``business_name``).  Never raises — failures degrade to {}.
    """
    if tenant_id is None:
        return {}

    try:
        from app.database.crud import get_crm_config
        from app.database.session import async_session_factory
        from app.integrations.encryption import decrypt_json

        async with async_session_factory() as session:
            row = await get_crm_config(session, tenant_id, integration_type="gemini")
            if row is None or not row.config:
                return {}
            cfg = decrypt_json(row.config, tenant_id=tenant_id) or {}
            return cfg
    except Exception:
        logger.warning(
            "Failed to resolve tenant config for tenant=%s — using global settings",
            tenant_id,
            exc_info=True,
        )
        return {}


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


def build_graph(
    api_key: str | None = None, tenant_id: UUID | None = None
) -> CompiledStateGraph:
    logger.debug("building graph with model=%s", settings.gemini_model)

    model = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=settings.gemini_temperature,
        api_key=api_key or settings.gemini_api_key,
        timeout=settings.gemini_timeout,
    )
    model = RetryingGeminiModel(model, tenant_id=tenant_id)

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
_tenant_graphs: dict[str, CompiledStateGraph] = {}
_tenant_graph_keys: dict[str, str | None] = {}


def get_graph(tenant_id: UUID | None = None, api_key: str | None = None) -> CompiledStateGraph:
    """Return the compiled graph for the given tenant.

    Tenants with a stored Gemini key (onboard_client writes it to
    ``crm_configs``) get a graph built with their own key, cached per tenant.
    Tenants without a stored key share the single platform graph built with
    ``settings.gemini_api_key`` (the legacy ``_agent_graph`` singleton, so
    existing tests that reset it keep working).  Pass the already-resolved
    ``api_key`` to avoid an extra DB round-trip.
    """
    global _agent_graph
    if tenant_id is None:
        if _agent_graph is None:
            _agent_graph = build_graph(api_key=api_key, tenant_id=None)
        return _agent_graph

    key = str(tenant_id)
    cached_key = _tenant_graph_keys.get(key)
    if key in _tenant_graphs and cached_key == api_key:
        return _tenant_graphs[key]
    logger.debug("building per-tenant graph for tenant=%s (has own key=%s)", tenant_id, bool(api_key))
    graph = build_graph(api_key=api_key, tenant_id=tenant_id)
    _tenant_graphs[key] = graph
    _tenant_graph_keys[key] = api_key
    return graph


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
    tenant_api_key = await resolve_tenant_gemini_key(tenant_id)
    graph = get_graph(tenant_id, api_key=tenant_api_key)
    if graph.checkpointer is not None:
        await graph.checkpointer.adelete_thread(thread_key)

    tenant_id_str = str(tenant_id) if tenant_id else None
    turn_input = get_initial_state(session_id, channel, tenant_id=tenant_id_str)
    turn_input["messages"] = [{"role": "user", "content": message}]

    # Per-tenant vertical/business_name (from the gemini crm_configs row, set
    # at onboarding) drive prompt selection and lead scoring.  Never raises —
    # missing/undecryptable configs fall back to {} and the nodes use global
    # settings as their default.
    tenant_cfg = await resolve_tenant_config(tenant_id)
    if tenant_cfg.get("vertical"):
        turn_input["vertical"] = tenant_cfg["vertical"]
    if tenant_cfg.get("business_name"):
        turn_input["business_name"] = tenant_cfg["business_name"]

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
