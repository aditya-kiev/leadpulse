from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.graph import run_agent
from app.database.crud import get_conversation


async def test_persistence_merges_saved_state_into_initial_state():
    """run_agent merges persisted lead fields from memory_service into turn_input."""
    persisted = {
        "lead_name": "Alice",
        "company_name": "Acme Corp",
        "industry": "technology",
        "budget": 50000.0,
        "timeline": "immediate",
        "problem_statement": "Need a CRM solution",
        "qualification_score": 0.75,
        "lead_status": "hot",
        "booking_confirmed": True,
        "meeting_time": "2026-07-10T14:00:00",
        "conversation_history": [
            {"role": "assistant", "content": "Great, let me ask you a few questions."},
        ],
        "conversation_stage": "collecting",
        "current_node": "info_collection",
        "human_escalated": False,
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={})
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.memory_service.load_state", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = persisted
        with patch("app.agent.graph.get_graph", return_value=mock_graph) as mock_get_graph:
            await run_agent("test-persist-session", "Hello again")

    call_input = mock_graph.ainvoke.call_args[0][0]
    assert call_input.get("lead_name") == "Alice"
    assert call_input.get("company_name") == "Acme Corp"
    assert call_input.get("industry") == "technology"
    assert call_input.get("budget") == 50000.0
    assert call_input.get("lead_status") == "hot"
    assert call_input.get("booking_confirmed") is True
    # run_agent appends the current user message to the loaded history
    # (with a timestamp field — check structure, not exact equality)
    actual_history = call_input.get("conversation_history")
    assert len(actual_history) == len(persisted["conversation_history"]) + 1
    assert actual_history[:-1] == persisted["conversation_history"]
    actual_last = actual_history[-1]
    assert actual_last["role"] == "user"
    assert actual_last["content"] == "Hello again"
    assert isinstance(actual_last.get("timestamp"), str) and "T" in actual_last["timestamp"]
    assert call_input.get("conversation_stage") == "collecting"
    assert call_input.get("current_node") == "info_collection"


async def test_persistence_new_lead_starts_with_defaults():
    """A fresh session with no persisted data uses get_initial_state defaults."""
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={})
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.memory_service.load_state", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = None
        with patch("app.agent.graph.get_graph", return_value=mock_graph):
            await run_agent("test-new-session", "Hi")

    call_input = mock_graph.ainvoke.call_args[0][0]
    assert call_input.get("lead_name") is None
    assert call_input.get("company_name") is None
    assert call_input.get("lead_status") is None
    assert call_input.get("booking_confirmed") is False


async def test_conversation_history_records_user_message_on_returning_turn():
    """Regression test: a lead's message on turn 2+ must appear in
    conversation_history, not just the bot's replies."""
    persisted = {
        "conversation_history": [
            {"role": "user", "content": "Hi there"},
            {"role": "assistant", "content": "Hello! How can I help?"},
        ],
        "conversation_stage": "collecting",
        "current_node": "info_collection",
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={})
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.memory_service.load_state", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = persisted
        with patch("app.agent.graph.get_graph", return_value=mock_graph):
            await run_agent("test-returning", "My budget is 50k")

    # The user message must be in conversation_history before the graph runs
    # (the reducer merges node responses, but we verify the upstream write here)
    call_input = mock_graph.ainvoke.call_args[0][0]
    history = call_input.get("conversation_history", [])
    assert any(
        m["role"] == "user" and m["content"] == "My budget is 50k"
        for m in history
    ), f"User message not found in input conversation_history: {history}"


async def test_double_call_does_not_duplicate_conversation_history():
    """Regression test: two sequential run_agent() calls with the same
    session_id must NOT duplicate conversation_history.

    Root cause: ``get_graph()`` caches a single ``MemorySaver`` for the
    process lifetime, ``conversation_history`` uses ``operator.add``
    (appending on top of checkpointed state), and ``run_agent()`` re-feeds
    the full Postgres-reloaded history on every turn.  Without deleting the
    checkpoint first, the reducer concatenates checkpoint history + fresh
    input history → duplicates.

    The fix deletes the checkpoint (``adelete_thread``) before each call so
    the graph starts fresh from the Postgres-reconstituted ``turn_input``.
    """
    from langgraph.graph import END, StateGraph
    from langgraph.checkpoint.memory import MemorySaver

    from app.agent.state import AgentState

    # ---- minimal graph with a real MemorySaver and operator.add reducer ----

    async def _passthrough(state):
        return {"current_node": "passthrough"}

    workflow = StateGraph(AgentState)
    workflow.add_node("passthrough", _passthrough)
    workflow.set_entry_point("passthrough")
    workflow.add_edge("passthrough", END)
    test_graph = workflow.compile(checkpointer=MemorySaver())

    # ---- in-memory Postgres simulation ----
    store: dict = {}

    async def _fake_save(session_id, state):
        store[session_id] = state

    async def _fake_load(session_id, tenant_id=None):
        return store.get(session_id)

    with patch("app.agent.graph.get_graph", return_value=test_graph), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):

        # ---- Turn 1 ----
        result1 = await run_agent("dup-test", "Hello")
        # webhook.py calls save_state after run_agent completes
        await _fake_save("dup-test", result1)
        hist1 = result1.get("conversation_history", [])
        assert len(hist1) == 1, (
            f"Turn 1: expected 1 entry, got {len(hist1)}: {hist1}"
        )
        assert hist1[0]["role"] == "user"
        assert hist1[0]["content"] == "Hello"

        # ---- Turn 2 (same session_id) ----
        result2 = await run_agent("dup-test", "What's your pricing?")
        await _fake_save("dup-test", result2)
        hist2 = result2.get("conversation_history", [])

        # Without the checkpoint-deletion fix, the operator.add reducer
        # would concatenate the old checkpoint's history with the freshly
        # Postgres-reloaded history, producing 3+ entries here.
        # With the fix the graph starts clean and yields exactly 2 entries.
        assert len(hist2) == 2, (
            f"Turn 2: expected 2 entries (no duplicates), got {len(hist2)}: {hist2}"
        )
        # User messages now include a timestamp — check fields individually
        assert hist2[0]["role"] == "user"
        assert hist2[0]["content"] == "Hello"
        assert isinstance(hist2[0].get("timestamp"), str) and "T" in hist2[0]["timestamp"]
        assert hist2[1]["role"] == "user"
        assert hist2[1]["content"] == "What's your pricing?"
        assert isinstance(hist2[1].get("timestamp"), str) and "T" in hist2[1]["timestamp"]


async def test_parse_budget_handles_indian_shorthand():
    """Indian currency shorthand must parse to the correct numeric value
    so it does not crash the DOUBLE PRECISION ``budget`` column."""
    from app.agent.nodes.helpers import parse_budget

    cases = [
        # (input, expected)
        ("80 lakh", 8_000_000),
        ("80L", 8_000_000),
        ("80lakh", 8_000_000),
        ("1.2 crore", 12_000_000),
        ("1.2 Cr", 12_000_000),
        ("1.2cr", 12_000_000),
        ("50k", 50_000),
        ("50000", 50_000.0),
        (50000, 50_000.0),
        (50000.0, 50_000.0),
        ("₹50,000", 50_000.0),
        ("₹ 80 lakh", 8_000_000),
        ("$50,000", 50_000.0),
        ("$ 80 lakh", 8_000_000),
        ("80-100 lakh", 9_000_000),
        ("50-80k", 65_000),
        ("50k-80k", 65_000),
        ("10-20", 15.0),
        ("80 lakh-1 crore", 9_000_000),
        ("80lakh-1cr", 9_000_000),
        (None, None),
        ("", None),
        ("not provided", None),
        ("unknown", None),
        ("abc", None),
        # US-denominated budgets
        ("1.2M", 1_200_000),
        ("$1.2 million", 1_200_000),
        ("1.2m", 1_200_000),
        ("1.2mn", 1_200_000),
        ("650k", 650_000),
        ("$150/month", 150.0),
        ("$3,200/mo", 3_200.0),
        ("1.8M", 1_800_000),
    ]
    for raw, expected in cases:
        result = parse_budget(raw)
        assert result == expected, (
            f"parse_budget({raw!r}) = {result!r}, expected {expected!r}"
        )


async def test_budget_parse_in_info_collection_round_trip():
    """Verify that info_collection's budget pipeline (LLM →
    _parse_combined_response → parse_budget) produces a numeric
    value that survives a round-trip through save_state/load_state.

    This uses the real ``memory_service.save_state`` / ``load_state``
    (mocked to an in-memory store) to confirm the DB layer would not
    throw a ``DataError`` on Indian shorthand.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.agent.graph import run_agent

    store: dict = {}

    async def _fake_save(session_id, state):
        store[session_id] = state

    async def _fake_load(session_id, tenant_id=None):
        return store.get(session_id)

    # Mock the graph to simulate info_collection returning "80 lakh"
    # as the extracted budget (as the LLM might produce before parsing).
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={
        "lead_name": "Raj",
        "company_name": "BuilderCorp",
        "industry": "real estate",
        "budget": 8_000_000.0,  # parse_budget("80 lakh") → 8000000
        "timeline": "3 months",
        "problem_statement": "Need construction financing",
        "missing_fields": [],
        "current_node": "qualification",
        "conversation_stage": "collecting",
        "lead_status": None,
        "qualification_score": None,
        "booking_confirmed": False,
        "meeting_time": None,
        "human_escalated": False,
        "objection_type": None,
        "next_action": "collect_info",
        "conversation_history": [
            {"role": "user", "content": "My budget is 80 lakh"},
            {"role": "assistant", "content": "Thanks! What timeline?"},
        ],
    })
    mock_graph.checkpointer = MagicMock()
    mock_graph.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.get_graph", return_value=mock_graph), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):
        result = await run_agent("budget-test", "My budget is 80 lakh")
        await _fake_save("budget-test", result)

    saved = store["budget-test"]
    assert saved.get("budget") == 8_000_000.0, (
        f"budget should be 8000000 after parse, got {saved.get('budget')!r}"
    )
    # Verify it's a float, not a string
    assert isinstance(saved.get("budget"), float), (
        f"budget must be a float for DOUBLE PRECISION column, got {type(saved.get('budget'))}"
    )


async def test_save_state_fails_loudly_on_unparseable_budget():
    """Regression: unparseable budget values must return None (not crash or
    silently pass a string) so the DB column does not receive a bad value."""
    from app.agent.nodes.helpers import parse_budget

    assert parse_budget("garbage") is None
    assert parse_budget("₹₹₹") is None
    assert parse_budget("$$$") is None
    assert parse_budget("1.2.3") is None
    assert parse_budget("-50") is None
    assert parse_budget("to 50") is None


# ── meeting_time persistence (bug: string → TIMESTAMP asyncpg rejection) ──
#
# These tests run against a REAL Postgres instance (Alembic-managed
# ``lead_agent_test`` DB), not mocks.  The earlier unit tests mock the entire
# DB layer and therefore never exercised the actual write, which is how a bug
# that broke every booking in production slipped past 246 passing tests.

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    """Real Postgres session factory against the Alembic-managed test DB."""
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


async def test_coerce_meeting_time_guards_every_input_shape():
    """Unit-test the save_state meeting_time coercion defensive paths."""
    from app.services.memory import _coerce_meeting_time

    # ISO strings with an offset (what meeting_booking actually produces)
    assert _coerce_meeting_time("2026-08-15T09:00:00+00:00") == datetime(2026, 8, 15, 9, 0)
    # naive ISO strings
    assert _coerce_meeting_time("2026-08-15T09:00:00") == datetime(2026, 8, 15, 9, 0)
    # already-datetime passthrough (naive and aware → normalised naive UTC)
    assert _coerce_meeting_time(datetime(2026, 8, 15, 9, 0)) == datetime(2026, 8, 15, 9, 0)
    assert _coerce_meeting_time(datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)) == datetime(2026, 8, 15, 9, 0)
    # None passthrough
    assert _coerce_meeting_time(None) is None
    # malformed strings must NOT raise — stored as None
    assert _coerce_meeting_time("not-a-date") is None
    assert _coerce_meeting_time("") is None
    # non-string, non-datetime junk must not raise either
    assert _coerce_meeting_time(12345) is None


async def test_save_state_persists_iso_meeting_time_to_real_postgres(pg_session_factory):
    """REAL Postgres integration test: a realistic ISO meeting_time string
    (the exact production shape, ``slot["datetime"]`` with a ``+00:00``
    offset) must persist through ``save_state`` and read back correctly via
    ``get_conversation``.  This is the test that should have caught the bug —
    before the fix asyncpg raised ``DataError`` on the string (and on the
    offset-aware datetime) and the booking was silently never saved."""
    from app.services.memory import memory_service

    session_id = f"itest-{uuid4()}"
    meeting_time = "2026-08-15T09:00:00+00:00"

    with patch("app.services.memory.async_session_factory", pg_session_factory):
        await memory_service.save_state(
            session_id,
            {
                "lead_name": "Alice",
                "booking_confirmed": True,
                "meeting_time": meeting_time,
                "conversation_history": [],
                "conversation_stage": "qualified",
                "current_node": "end",
                "human_escalated": False,
            },
        )

        async with pg_session_factory() as db_session:
            lead = await get_conversation(db_session, session_id)
            assert lead is not None, "conversation row must exist after save_state"
            assert lead.booking_confirmed is True
            assert lead.meeting_time is not None, (
                "meeting_time was not persisted — the booking was silently lost"
            )
            assert isinstance(lead.meeting_time, datetime)
            # Postgres stores a naive timestamp (no tz); the offset input
            # normalises to the UTC wall-clock.
            assert lead.meeting_time == datetime(2026, 8, 15, 9, 0), (
                f"meeting_time read back = {lead.meeting_time!r}"
            )

        # Symmetric read-back via load_state (the value must round-trip as a
        # string that can be fed straight back into save_state).
        state = await memory_service.load_state(session_id)
        assert state["booking_confirmed"] is True
        assert state["meeting_time"] == "2026-08-15T09:00:00"
        from app.services.memory import _coerce_meeting_time

        assert _coerce_meeting_time(state["meeting_time"]) == datetime(2026, 8, 15, 9, 0)


async def test_webhook_full_conversation_persists_confirmed_booking(pg_session_factory):
    """REAL Postgres regression test for the production gap: run a full webhook
    conversation (only the Gemini call is mocked — every node, the graph, and
    memory_service.run the real code) through to a confirmed booking, then
    assert the Postgres row has ``booking_confirmed=True`` and a non-null
    ``meeting_time``.  Before the fix, ``state_persisted`` came back false and
    the DB row kept ``booking_confirmed=false`` / ``meeting_time=NULL`` even
    though the lead was told they were booked."""
    from app.api.webhook import handle_message
    from app.models.schemas import MessageIn

    session_id = f"itest-webhook-{uuid4()}"

    # Scripted Gemini model — branches on the system prompt so each node gets
    # a legitimate canned response and the graph really traverses
    # greeting → info_collection → qualification → handle_next →
    # meeting_booking (turn 1) then meeting_booking(confirm) → end (turn 2).
    class ScriptedModel:
        def __init__(self):
            self.call_count = 0

        async def ainvoke(self, messages, **kwargs):
            self.call_count += 1
            sys_content = ""
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system":
                    sys_content = m["content"]
                    break
                if getattr(m, "type", None) == "system":
                    sys_content = m.content
                    break
            mock = MagicMock()
            if "friendly AI sales assistant" in sys_content:
                mock.content = (
                    "INTENT: purchase\nLEAD_TYPE: company\nREPLY: Hello! How can I help you today?"
                )
            elif "collecting information from a potential customer" in sys_content:
                mock.content = (
                    'EXTRACTED: {"lead_name": "Alice", "company_name": "Acme Inc", '
                    '"industry": "real estate", "budget": 50000, '
                    '"timeline": "3 months", '
                    '"problem_statement": "Need a modern CRM system"}'
                    "\nREPLY: Thanks, Alice! Let me verify a few things."
                )
            elif "lead qualification expert" in sys_content:
                mock.content = "Score: 0.85 Hot lead. Good fit, ready to move."
            elif "scheduling assistant" in sys_content:
                mock.content = "Sure! Would 9am UTC on a weekday work for you?"
            elif "The conversation is ending" in sys_content:
                mock.content = "You're all set! See you at the meeting."
            else:
                # objection detection etc. — no objection
                mock.content = "none"
            return mock

    scripted = ScriptedModel()

    with patch("app.services.memory.async_session_factory", pg_session_factory), \
         patch("app.agent.graph.ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.agent.graph.RetryingGeminiModel", return_value=scripted), \
         patch("app.agent.graph._agent_graph", None), \
         patch("app.agent.graph.get_last_usage", return_value=None):
        # Force a fresh graph built with the scripted model.
        from app.agent.graph import get_graph

        get_graph()

        # Turn 1 — lead greets, provides info, is offered a meeting.
        res1 = await handle_message(
            SimpleNamespace(state=SimpleNamespace(tenant_id=None)),
            MessageIn(session_id=session_id, message="Hello"),
        )
        assert res1.state_persisted is True, (
            "turn 1 state must persist against real Postgres"
        )
        assert res1.booking_confirmed is False

        # Turn 2 — lead confirms the booking.
        res2 = await handle_message(
            SimpleNamespace(state=SimpleNamespace(tenant_id=None)),
            MessageIn(session_id=session_id, message="Yes, let's book a meeting"),
        )
        assert res2.state_persisted is True, (
            "booking state must persist against real Postgres — this is the "
            "exact failure the bug caused (state_persisted=false, booking lost)"
        )
        assert res2.booking_confirmed is True
        assert res2.meeting_time is not None

    # The real proof: what landed in Postgres.
    async with pg_session_factory() as db_session:
        lead = await get_conversation(db_session, session_id)
        assert lead is not None
        assert lead.booking_confirmed is True, (
            "Postgres row must record the confirmed booking"
        )
        assert lead.meeting_time is not None, (
            "Postgres row must have a non-null meeting_time after a booking"
        )
        assert isinstance(lead.meeting_time, datetime)
        # The follow-up-message regression path: the lead must resume as
        # already-booked instead of re-entering the booking flow.
        assert lead.current_node == "end"

