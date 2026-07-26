import pytest

from langgraph.graph import END

from app.agent.graph import get_entry_point, route_after_greeting, route_after_info_collection, route_after_meeting, route_after_objection, route_after_qualification, route_next_action
from app.agent.state import get_initial_state


def test_parse_combined_response_defaults_to_individual_for_real_estate():
    """Regression: a malformed/missing LEAD_TYPE: line must default to
    'individual' for real_estate and insurance verticals (not 'company')."""
    from app.agent.nodes.greeting import _parse_combined_response

    # No LEAD_TYPE line at all
    intent, lead_type, _ = _parse_combined_response(
        "INTENT: purchase\nREPLY: Hi there!", vertical="real_estate"
    )
    assert lead_type == "individual", f"real_estate default should be individual, got {lead_type!r}"

    intent, lead_type, _ = _parse_combined_response(
        "INTENT: purchase\nREPLY: Hi there!", vertical="insurance"
    )
    assert lead_type == "individual", f"insurance default should be individual, got {lead_type!r}"

    # Generic vertical still defaults to company
    intent, lead_type, _ = _parse_combined_response(
        "INTENT: purchase\nREPLY: Hi there!", vertical="generic"
    )
    assert lead_type == "company", f"generic default should be company, got {lead_type!r}"

    # Unclear LEAD_TYPE value
    intent, lead_type, _ = _parse_combined_response(
        "INTENT: purchase\nLEAD_TYPE: unclear\nREPLY: Hi!", vertical="real_estate"
    )
    assert lead_type == "individual", f"unclear LEAD_TYPE should default to individual for real_estate, got {lead_type!r}"

    # Explicit "company" still works
    intent, lead_type, _ = _parse_combined_response(
        "INTENT: purchase\nLEAD_TYPE: company\nREPLY: Hi!", vertical="real_estate"
    )
    assert lead_type == "company", f"explicit 'company' should be respected, got {lead_type!r}"


def test_state_initialization():
    state = get_initial_state("test-123")
    assert state["session_id"] == "test-123"
    assert state["lead_name"] is None
    assert state["company_name"] is None
    assert state["qualification_score"] is None
    assert state["booking_confirmed"] is False
    assert state["human_escalated"] is False
    assert state["current_node"] == "greeting"
    assert state["conversation_stage"] == "greeting"


def test_route_after_greeting():
    state = get_initial_state("test-1")
    assert route_after_greeting(state) == "info_collection"

    state["lead_intent"] = "support"
    assert route_after_greeting(state) == "faq"


def test_route_after_info_collection():
    state = get_initial_state("test-1")
    state["missing_fields"] = ["lead_name", "budget"]
    assert route_after_info_collection(state) == END

    state["missing_fields"] = []
    assert route_after_info_collection(state) == "qualification"


def test_route_after_qualification():
    state = get_initial_state("test-1")
    assert route_after_qualification(state) == "handle_next"


def test_route_next_action_hot_lead():
    state = get_initial_state("test-1")
    state["lead_status"] = "hot"
    assert route_next_action(state) == "meeting_booking"


def test_route_next_action_booking():
    state = get_initial_state("test-1")
    state["booking_confirmed"] = True
    assert route_next_action(state) == "end"


def test_route_next_action_human_escalated():
    state = get_initial_state("test-1")
    state["human_escalated"] = True
    assert route_next_action(state) == "end"


def test_route_next_action_low_confidence():
    state = get_initial_state("test-1")
    state["confidence"] = 0.1
    assert route_next_action(state) == "human_handoff"


def test_route_next_action_objection_handling():
    state = get_initial_state("test-1")
    state["objection_type"] = "pricing"
    assert route_next_action(state) == "objection_handling"

    state["objection_type"] = "timing"
    assert route_next_action(state) == "objection_handling"


def test_route_next_action_objection_takes_precedence_over_hot():
    """Objection routing should take precedence over hot-lead routing."""
    state = get_initial_state("test-1")
    state["objection_type"] = "trust"
    state["lead_status"] = "hot"
    assert route_next_action(state) == "objection_handling"


async def test_greeting_node_skips_for_returning_user():
    """The greeting node must not re-enter for returning users mid-conversation."""
    state = get_initial_state("test-1")
    state["conversation_history"] = [{"role": "assistant", "content": "Hello!"}]
    state["conversation_stage"] = "collecting"
    state["current_node"] = "info_collection"

    from unittest.mock import MagicMock, AsyncMock
    from app.agent.nodes.greeting import create_greeting_node

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(
        side_effect=Exception("should not be called for returning user")
    )
    node_fn = create_greeting_node(mock_model)
    result = await node_fn(state)

    assert result == {}, "Greeting node must return empty dict for returning users"


def test_state_default_values():
    state = get_initial_state("session-001", "email")
    assert state["channel"] == "email"
    assert state["session_id"] == "session-001"
    assert state["lead_intent"] is None
    assert state["iteration_count"] == 0
    assert state["confidence"] == 1.0
    assert state["current_question"] is None


def test_get_entry_point_resumes_from_current_node():
    """Entry point must use current_node from persisted state, not hardcoded greeting."""
    state = get_initial_state("test-1")
    # First turn: current_node defaults to "greeting"
    assert get_entry_point(state) == "greeting"

    # Subsequent turn: resume from whatever the last active node was
    state["current_node"] = "info_collection"
    assert get_entry_point(state) == "info_collection"

    state["current_node"] = "meeting_booking"
    assert get_entry_point(state) == "meeting_booking"


def test_get_entry_point_falls_back_to_greeting_for_unknown_node():
    """Unknown current_node should fall back to greeting."""
    state = get_initial_state("test-1")
    state["current_node"] = "nonexistent"
    assert get_entry_point(state) == "greeting"


def test_info_collection_ends_when_missing_fields():
    """Partial info must complete in one step and route to END, not loop."""
    state = get_initial_state("test-1")
    state["missing_fields"] = ["lead_name"]
    # The graph run should end here, not loop back to info_collection
    assert route_after_info_collection(state) == END


def test_route_after_meeting_ends_when_not_confirmed():
    """Unconfirmed booking must route to END, not self-loop."""
    state = get_initial_state("test-1")
    state["booking_confirmed"] = False
    assert route_after_meeting(state) == END

    state["booking_confirmed"] = True
    assert route_after_meeting(state) == "end"


def test_route_next_action_cold_lead_goes_to_end():
    """Cold lead must route to 'end' node so end_conversation_node produces a
    real closing message, rather than bare END which silently recycles the
    previous assistant reply forever."""
    state = get_initial_state("test-1")
    state["confidence"] = 1.0
    state["lead_status"] = "cold"
    assert route_next_action(state) == "end"


def test_route_after_objection_cold_lead_goes_to_end():
    """Cold lead after objection handling must also go to 'end' node."""
    state = get_initial_state("test-1")
    state["lead_status"] = "cold"
    assert route_after_objection(state) == "end"


async def test_info_collection_does_not_loop_on_partial_data():
    """Simulate a full graph run with partial info — must complete in one step."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.content = '{"Name": "Alice"}'

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_response)

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={
        "missing_fields": ["company_name", "budget", "timeline", "problem_statement", "industry"],
        "conversation_history": [{"role": "assistant", "content": "What's your company name?"}],
        "current_node": "info_collection",
    })
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.ChatGoogleGenerativeAI", return_value=mock_model), \
         patch("app.agent.graph.get_entry_point", return_value="info_collection"), \
         patch("app.agent.graph.memory_service.load_state", new_callable=AsyncMock, return_value=None), \
         patch("app.agent.graph.get_graph", return_value=mock_graph):

        from app.agent.graph import run_agent
        result = await run_agent("test-partial", "My name is Alice")

    # Must have exactly one assistant reply (follow-up question), not a loop
    history = result.get("conversation_history", [])
    assistant_msgs = [m for m in history if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1, f"Expected 1 assistant reply, got {len(assistant_msgs)}: {history}"


async def test_cold_lead_gets_new_replies_on_every_turn():
    """Regression: a lead scored 'cold' on an earlier turn must receive a
    new assistant reply on every subsequent message, not the same stale
    message forever.

    The bug: qualification_node's returning-user short-circuit (no
    messages/conversation_history emitted) combined with route_next_action
    returning bare END for cold leads means no node ever runs to generate a
    reply.  webhook.py's last_message loop scans backward for the most recent
    assistant entry — which is always the same one from the scoring turn.

    Fix: route cold leads to the 'end' node (end_conversation_node) which
    produces a fresh closing message every time it runs.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from langgraph.graph import END, StateGraph
    from langgraph.checkpoint.memory import MemorySaver

    from app.agent.state import AgentState
    from app.agent.graph import route_next_action, route_after_objection

    # ---- minimal graph that routes through the real route_next_action ----

    async def _identity(state):
        return {
            "lead_status": "cold",
            "current_node": "end",
            "conversation_history": [
                {"role": "assistant", "content": f"Thank you for your interest. At this time..."}
            ],
        }

    async def _end_node(state):
        c = f"Goodbye from turn {len(state.get('conversation_history', []))}"
        return {
            "conversation_history": [{"role": "assistant", "content": c}],
            "current_node": "end",
            "next_action": None,
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("handle_next", _identity)
    workflow.add_node("end", _end_node)
    workflow.set_entry_point("handle_next")
    workflow.add_conditional_edges(
        "handle_next",
        route_next_action,
        {"end": "end", END: END},
    )
    workflow.add_edge("end", END)
    test_graph = workflow.compile(checkpointer=MemorySaver())

    store: dict = {}

    async def _fake_save(session_id, state):
        store[session_id] = state

    async def _fake_load(session_id, tenant_id=None):
        return store.get(session_id)

    with patch("app.agent.graph.get_graph", return_value=test_graph), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):

        from app.agent.graph import run_agent

        # Turn 1: qualification scores the lead as cold → end node runs
        r1 = await run_agent("cold-multi", "I'm interested in your services")
        await _fake_save("cold-multi", r1)
        hist1 = [m for m in r1.get("conversation_history", []) if m.get("role") == "assistant"]
        assert len(hist1) >= 1, f"Turn 1: expected at least 1 assistant msg, got {len(hist1)}"

        # Turn 2: returning user, short-circuits qualification, routes cold → end
        r2 = await run_agent("cold-multi", "What about discounts?")
        await _fake_save("cold-multi", r2)
        hist2 = [m for m in r2.get("conversation_history", []) if m.get("role") == "assistant"]

        # MUST have a NEW assistant entry — not the same one from turn 1
        assert len(hist2) > len(hist1), (
            f"Turn 2: expected more assistant msgs than turn 1 "
            f"({len(hist1)}), got {len(hist2)}"
        )
        assert hist2[-1]["content"] != hist1[-1]["content"], (
            "Turn 2: assistant message must differ from turn 1 (stale reply)"
        )

        # Turn 3: another follow-up → must also get a fresh reply
        r3 = await run_agent("cold-multi", "Can I speak to a manager?")
        await _fake_save("cold-multi", r3)
        hist3 = [m for m in r3.get("conversation_history", []) if m.get("role") == "assistant"]
        assert len(hist3) > len(hist2), (
            f"Turn 3: expected more assistant msgs than turn 2 "
            f"({len(hist2)}), got {len(hist3)}"
        )
        assert hist3[-1]["content"] != hist2[-1]["content"], (
            "Turn 3: assistant message must differ from turn 2 (stale reply)"
        )


async def test_individual_buyer_skips_company_name():
    """An individual consumer (lead_type='individual') must reach
    qualification without ever being asked for a company name.

    The greeting node classifies the lead as individual → info_collection
    drops company_name from its field priority → qualification drops it
    from its required fields.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.agent.graph import run_agent
    from app.agent.state import get_initial_state
    from app.agent.nodes.qualification import _required_fields_for
    from app.agent.nodes.info_collection import _field_priority_for, _field_map_for

    # --- Unit checks ---
    assert "company_name" not in _required_fields_for("individual")
    assert "company_name" in _required_fields_for("company")
    assert "company_name" not in _field_priority_for("individual")
    assert "company_name" not in _field_map_for("individual")

    # --- Integration: info_collection with lead_type='individual' ---
    # Mock the greeting to skip (returning user) and inject lead_type
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={
        "lead_name": "Priya",
        "lead_type": "individual",
        "industry": "real estate",
        "budget": 5000000.0,
        "timeline": "3 months",
        "problem_statement": "Looking for a 2BHK apartment",
        "company_name": None,
        "missing_fields": [],
        "current_node": "qualification",
        "next_action": "qualify",
        "conversation_stage": "qualified",
        "lead_status": "hot",
        "qualification_score": 0.85,
        "booking_confirmed": False,
        "meeting_time": None,
        "human_escalated": False,
        "objection_type": None,
        "conversation_history": [
            {"role": "user", "content": "Hi, I'm looking for an apartment"},
            {"role": "assistant", "content": "Great, what's your name?"},
        ],
    })
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.get_graph", return_value=mock_graph), \
         patch("app.agent.graph.memory_service.load_state", new_callable=AsyncMock, return_value=None):
        result = await run_agent("individual-test", "Hi, I'm looking for an apartment")

    # The individual lead must reach qualification with company_name=None
    # and no missing_fields for company_name
    assert result.get("lead_type") == "individual"
    missing = result.get("missing_fields", [])
    assert "company_name" not in missing, (
        f"Individual lead should not be asked for company_name, "
        f"got missing_fields={missing}"
    )


@pytest.mark.asyncio
async def test_meeting_booking_does_not_self_confirm_from_earlier_ai_message():
    """Regression: meeting_booking_node must NOT set booking_confirmed=True
    when the last HumanMessage contains no confirmation language, even if
    earlier AIMessages in state['messages'] contain trigger words like
    'schedule' or 'book' (e.g. from qualification node's internal output)."""
    from unittest.mock import AsyncMock, MagicMock
    from langchain_core.messages import HumanMessage, AIMessage
    from app.agent.nodes.meeting_booking import create_meeting_booking_node

    state = {
        "lead_name": "Sarah",
        "company_name": None,
        "lead_status": "hot",
        "messages": [
            HumanMessage(content="Hi, my name is Sarah, budget is 650k, moving in 3 months."),
            AIMessage(content="Score: 0.85. Status: hot. Reasoning: strong fit. "
                               "Recommended next action: schedule a viewing this week."),
            HumanMessage(content="What times do you have available?"),
        ],
        "conversation_history": [],
        "booking_confirmed": False,
        "meeting_time": None,
        "session_id": "test-self-confirm",
    }

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(
        content="I have slots on Tuesday at 10am, Wednesday at 2pm, or Friday at 11am. Which works best for you?",
    ))

    node = create_meeting_booking_node(mock_model)
    result = await node(state)

    # The human message "What times do you have available?" does NOT contain
    # "yes", "confirm", "book", "schedule" etc. → booking must NOT be confirmed.
    assert result.get("booking_confirmed") is False, (
        f"booking_confirmed should be False but got {result.get('booking_confirmed')!r}"
    )
    assert result.get("meeting_time") is None, (
        f"meeting_time should be None but got {result.get('meeting_time')!r}"
    )


@pytest.mark.asyncio
async def test_meeting_booking_first_entry_no_fallback_on_stray_keyword():
    """When current_node is NOT 'meeting_booking' on entry, a stray
    affirmative keyword with no exact slot match must NOT confirm a booking.
    The fallback-to-slots[0] is only safe on re-entry where slots were
    explicitly shown on a prior turn."""
    from unittest.mock import AsyncMock, MagicMock
    from langchain_core.messages import HumanMessage
    from app.agent.nodes.meeting_booking import create_meeting_booking_node

    state = {
        "lead_name": "Tom",
        "company_name": None,
        "lead_status": "hot",
        "messages": [
            HumanMessage(content="Sure, what kind of meetings do you offer?"),
        ],
        "conversation_history": [],
        "booking_confirmed": False,
        "meeting_time": None,
        "session_id": "test-first-entry",
        "current_node": "qualification",  # NOT meeting_booking → first entry
    }

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(
        content="I'd be happy to walk you through the scheduling process. "
                "We offer 30-minute introductory calls. What time works best?",
    ))

    node = create_meeting_booking_node(mock_model)
    result = await node(state)

    assert result.get("booking_confirmed") is False, (
        f"First entry: stray 'sure' without slot match must NOT confirm, "
        f"got booking_confirmed={result.get('booking_confirmed')!r}"
    )
    assert result.get("meeting_time") is None, (
        f"First entry: meeting_time must be None, "
        f"got {result.get('meeting_time')!r}"
    )


@pytest.mark.asyncio
async def test_meeting_booking_reentry_confirms_with_generic_keyword():
    """When current_node WAS already 'meeting_booking' (slots proposed on a
    prior turn), a generic affirmative reply without an exact slot label
    should fall back to slots[0] and confirm the booking."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain_core.messages import HumanMessage
    from app.agent.nodes.meeting_booking import create_meeting_booking_node

    state = {
        "lead_name": "Jane",
        "company_name": None,
        "lead_status": "hot",
        "messages": [
            HumanMessage(content="Sure, that works."),
        ],
        "conversation_history": [
            {"role": "assistant", "content": "I have Tuesday 10am, Wednesday 2pm, or Friday 11am."},
        ],
        "booking_confirmed": False,
        "meeting_time": None,
        "session_id": "test-reentry",
        "current_node": "meeting_booking",  # re-entry: slots were shown prior turn
    }

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(
        content="Perfect! I've booked you for Tuesday at 10am. You'll receive "
                "a calendar invite shortly.",
    ))

    with patch("app.agent.nodes.meeting_booking.get_available_slots",
               new_callable=AsyncMock) as mock_slots:
        mock_slots.return_value = [
            {"label": "Tuesday, Jul 21 at 10:00 AM", "datetime": "2026-07-21T10:00:00"},
            {"label": "Wednesday, Jul 22 at 2:00 PM", "datetime": "2026-07-22T14:00:00"},
            {"label": "Friday, Jul 24 at 11:00 AM", "datetime": "2026-07-24T11:00:00"},
        ]

        node = create_meeting_booking_node(mock_model)
        result = await node(state)

    assert result.get("booking_confirmed") is True, (
        f"Re-entry with generic 'sure, that works' must confirm, "
        f"got booking_confirmed={result.get('booking_confirmed')!r}"
    )
    assert result.get("meeting_time") == "2026-07-21T10:00:00", (
        f"meeting_time should default to first slot, "
        f"got {result.get('meeting_time')!r}"
    )


@pytest.mark.asyncio
async def test_meeting_booking_reentry_question_blocks_confirmation():
    """When current_node is 'meeting_booking' but the user's message contains
    a '?' character, the keyword fallback must NOT confirm — a question is
    not a pure confirmation."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain_core.messages import HumanMessage
    from app.agent.nodes.meeting_booking import create_meeting_booking_node

    state = {
        "lead_name": "Carlos",
        "company_name": None,
        "lead_status": "hot",
        "messages": [
            HumanMessage(content="Sure, but can you also tell me if you handle new construction homes?"),
        ],
        "conversation_history": [
            {"role": "assistant", "content": "I have Tuesday 10am, Wednesday 2pm, or Friday 11am."},
        ],
        "booking_confirmed": False,
        "meeting_time": None,
        "session_id": "test-question-entry",
        "current_node": "meeting_booking",
    }

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(
        content="Great question! Yes, we do handle new construction homes. "
                "Would you like to book a call to discuss further?",
    ))

    with patch("app.agent.nodes.meeting_booking.get_available_slots",
               new_callable=AsyncMock) as mock_slots:
        mock_slots.return_value = [
            {"label": "Tuesday, Jul 21 at 10:00 AM", "datetime": "2026-07-21T10:00:00"},
        ]

        node = create_meeting_booking_node(mock_model)
        result = await node(state)

    assert result.get("booking_confirmed") is False, (
        f"Re-entry with question must NOT confirm, "
        f"got booking_confirmed={result.get('booking_confirmed')!r}"
    )
    assert result.get("meeting_time") is None, (
        f"meeting_time must be None, got {result.get('meeting_time')!r}"
    )


def test_gemini_timeout_is_configured():
    """ChatGoogleGenerativeAI must be constructed with a timeout so hanging
    Gemini calls raise an exception instead of blocking forever."""
    from unittest.mock import MagicMock, patch
    with patch("app.agent.graph.ChatGoogleGenerativeAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        from app.agent.graph import build_graph
        build_graph()
    _, kwargs = mock_cls.call_args
    assert "timeout" in kwargs, "ChatGoogleGenerativeAI must have a timeout kwarg"
    assert kwargs["timeout"] > 0, "timeout must be a positive number (seconds)"
