"""Contact capture: extraction, decline handling, gating and persistence."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agent.contact import (
    CONTACT_MARKER,
    build_contact_request,
    contact_missing,
    extract_contact,
    is_contact_decline,
    last_assistant_asked_for_contact,
)


@pytest.mark.parametrize("text,email,phone", [
    ("my email is Jane.Doe@Example.com thanks", "jane.doe@example.com", None),
    ("call me on (415) 555-0123", None, "4155550123"),
    ("+1 415-555-0123 or jane@x.io", "jane@x.io", "+14155550123"),
    ("+91 98765 43210", None, "+919876543210"),
    ("my budget is $1,200,000", None, None),
    ("budget 650000 in 3 months", None, None),
    ("in 2026-10-27", None, None),
    ("", None, None),
])
def test_extract_contact(text, email, phone):
    assert extract_contact(text) == (email, phone)


@pytest.mark.parametrize("text", ["no", "No thanks", "I'd rather not", "skip", "prefer not to say"])
def test_declines(text):
    assert is_contact_decline(text)


@pytest.mark.parametrize("text", ["yes", "sure, 415 555 0123", "my email is a@b.co", "tomorrow"])
def test_non_declines(text):
    assert not is_contact_decline(text)


def test_contact_missing_respects_flag_and_decline():
    with patch("app.config.settings.settings.require_contact_info", True):
        assert contact_missing({}) is True
        assert contact_missing({"lead_phone": "123"}) is False
        assert contact_missing({"lead_email": "a@b.co"}) is False
        assert contact_missing({"contact_declined": True}) is False
    with patch("app.config.settings.settings.require_contact_info", False):
        assert contact_missing({}) is False


def test_contact_request_wording_and_marker():
    first = build_contact_request({"lead_name": "Alice Smith", "business_name": "Acme Realty"})
    assert "Alice" in first and "Acme Realty" in first and CONTACT_MARKER in first
    again = build_contact_request({"lead_name": "Alice"}, asked_before=True)
    assert CONTACT_MARKER in again and "skip" in again
    assert last_assistant_asked_for_contact({"conversation_history": [
        {"role": "assistant", "content": first}, {"role": "user", "content": "hm"}]})
    assert not last_assistant_asked_for_contact({"conversation_history": [
        {"role": "assistant", "content": "What's your budget?"}]})


def _full_state(**kw):
    s = {
        "lead_type": "company", "lead_name": "Alice", "company_name": "Acme",
        "industry": "real estate", "budget": 50000.0, "timeline": "3 months",
        "problem_statement": "Need a CRM", "business_name": "Acme Realty",
        "conversation_history": [], "messages": [HumanMessage(content="hi")],
    }
    s.update(kw)
    return s


@pytest.mark.asyncio
async def test_info_collection_asks_for_contact_without_llm_call():
    from app.agent.nodes.info_collection import create_info_collection_node

    model = MagicMock()
    model.ainvoke = AsyncMock()
    with patch("app.config.settings.settings.require_contact_info", True):
        out = await create_info_collection_node(model)(_full_state())
    model.ainvoke.assert_not_called()
    assert out["missing_fields"] == ["contact_info"]
    assert CONTACT_MARKER in out["messages"][0].content


@pytest.mark.asyncio
async def test_info_collection_proceeds_once_contact_present_or_declined():
    from app.agent.nodes.info_collection import create_info_collection_node

    model = MagicMock()
    model.ainvoke = AsyncMock()
    with patch("app.config.settings.settings.require_contact_info", True):
        for extra in ({"lead_phone": "4155550123"}, {"lead_email": "a@b.co"}, {"contact_declined": True}):
            out = await create_info_collection_node(model)(_full_state(**extra))
            assert out["missing_fields"] == []


@pytest.mark.asyncio
async def test_qualification_does_not_score_without_contact():
    from app.agent.nodes.qualification import _all_fields_populated

    with patch("app.config.settings.settings.require_contact_info", True):
        assert _all_fields_populated(_full_state()) is False
        assert _all_fields_populated(_full_state(lead_email="a@b.co")) is True


@pytest.mark.asyncio
async def test_run_agent_captures_contact_and_decline_deterministically():
    """run_agent extracts a volunteered phone/email and records a decline."""
    from app.agent import graph as graph_module

    captured = {}

    async def fake_ainvoke(turn_input, config):
        captured.update(turn_input)
        return dict(turn_input)

    fake_graph = MagicMock()
    fake_graph.ainvoke = fake_ainvoke
    fake_graph.checkpointer = None

    async def run(message, persisted):
        captured.clear()
        with patch.object(graph_module, "get_graph", return_value=fake_graph), \
             patch.object(graph_module, "resolve_tenant_gemini_key", AsyncMock(return_value=None)), \
             patch.object(graph_module, "resolve_tenant_config", AsyncMock(return_value={})), \
             patch.object(graph_module.memory_service, "load_state", AsyncMock(return_value=persisted)), \
             patch.object(graph_module, "get_last_usage", return_value=None):
            await graph_module.run_agent("s-1", message)
        return dict(captured)

    got = await run("sure, it's jane@x.io / 415-555-0123", None)
    assert got["lead_email"] == "jane@x.io" and got["lead_phone"] == "4155550123"

    asked = {"conversation_history": [{"role": "assistant", "content": build_contact_request({})}]}
    assert (await run("I'd rather not", asked))["contact_declined"] is True
    # A decline to some OTHER question must not set the flag.
    other = {"conversation_history": [{"role": "assistant", "content": "What's your budget?"}]}
    assert (await run("no", other))["contact_declined"] is False


# ── End-to-end through the real graph + real Postgres ─────────────────────

import os
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test"
)


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(_TEST_DB)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_flow_asks_for_contact_then_scores_then_books_chosen_slot(pg_session_factory):
    from fastapi import BackgroundTasks

    from app.api.webhook import handle_message
    from app.database.crud import get_conversation
    from app.models.schemas import MessageIn

    sid = f"e2e-contact-{uuid4()}"
    slots = [
        {"label": "Wednesday, August 19 at 09:00 AM UTC", "datetime": "2026-08-19T09:00:00+00:00"},
        {"label": "Wednesday, August 19 at 10:00 AM UTC", "datetime": "2026-08-19T10:00:00+00:00"},
    ]

    class Scripted:
        async def ainvoke(self, messages, **kw):
            sys_content = ""
            for m in messages:
                if getattr(m, "type", None) == "system":
                    sys_content = m.content
                    break
            r = MagicMock()
            if "friendly AI sales assistant" in sys_content:
                r.content = "INTENT: purchase\nLEAD_TYPE: company\nREPLY: Hello! How can I help?"
            elif "collecting information from a potential customer" in sys_content:
                r.content = (
                    'EXTRACTED: {"lead_name": "Alice", "company_name": "Acme Inc", "industry": "insurance", '
                    '"budget": 50000, "timeline": "3 months", "problem_statement": "bundle auto and home"}'
                    "\nREPLY: Thanks Alice!"
                )
            elif "lead qualification expert" in sys_content:
                r.content = "Score: 0.85 Hot lead."
            elif "scheduling assistant" in sys_content:
                r.content = "Would Wednesday at 9am or 10am suit you?"
            else:
                r.content = "none"
            return r

    async def send(text):
        return await handle_message(
            SimpleNamespace(state=SimpleNamespace(tenant_id=None)),
            MessageIn(session_id=sid, message=text),
            BackgroundTasks(),
        )

    with patch("app.config.settings.settings.require_contact_info", True), \
         patch("app.services.memory.async_session_factory", pg_session_factory), \
         patch("app.agent.graph.ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.agent.graph.RetryingGeminiModel", return_value=Scripted()), \
         patch("app.agent.graph._agent_graph", None), \
         patch("app.agent.nodes.meeting_booking.get_available_slots", new=AsyncMock(return_value=slots)), \
         patch("app.agent.graph.get_last_usage", return_value=None):
        from app.agent.graph import get_graph
        get_graph()

        # Turn 1: all qualifying fields collected -> we must ask for contact, NOT score yet.
        r1 = await send("Hello, I need insurance")
        assert r1.state_persisted and r1.lead_status is None
        reply1 = r1.reply.lower()
        assert "phone number or email" in reply1

        # Turn 2: lead gives a phone number -> scored + offered slots.
        r2 = await send("sure, call me on 415-555-0123")
        assert r2.state_persisted and r2.lead_status in ("hot", "warm")
        assert r2.booking_confirmed is False

        # Turn 3: lead picks a specific slot.
        r3 = await send("Wednesday at 10am please")
        assert r3.booking_confirmed is True and "10:00:00" in str(r3.meeting_time)

    async with pg_session_factory() as db:
        row = await get_conversation(db, sid)
    assert row.lead_phone == "4155550123"
    assert row.booking_confirmed is True
    assert row.contact_declined is False
