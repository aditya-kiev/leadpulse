"""Unit tests for the deterministic booking-intent resolver."""
import pytest

from app.agent.booking_intent import match_slots, resolve_booking

SLOTS = [
    {"label": "Tuesday, October 27 at 09:00 AM UTC", "datetime": "2026-10-27T09:00:00+00:00"},
    {"label": "Tuesday, October 27 at 10:00 AM UTC", "datetime": "2026-10-27T10:00:00+00:00"},
    {"label": "Tuesday, October 27 at 02:00 PM UTC", "datetime": "2026-10-27T14:00:00+00:00"},
    {"label": "Wednesday, October 28 at 09:00 AM UTC", "datetime": "2026-10-28T09:00:00+00:00"},
    {"label": "Wednesday, October 28 at 03:00 PM UTC", "datetime": "2026-10-28T15:00:00+00:00"},
]


@pytest.mark.parametrize("text,expected_dt", [
    ("Tuesday at 10", "2026-10-27T10:00:00+00:00"),
    ("tuesday 10am please", "2026-10-27T10:00:00+00:00"),
    ("Let's do Wednesday at 3pm", "2026-10-28T15:00:00+00:00"),
    ("October 28 at 9", "2026-10-28T09:00:00+00:00"),
    ("Tuesday at 2", "2026-10-27T14:00:00+00:00"),  # bare 1-7 means afternoon
    ("wed 15:00", "2026-10-28T15:00:00+00:00"),
])
def test_explicit_selection_confirms_exactly_that_slot(text, expected_dt):
    d = resolve_booking(text, SLOTS)
    assert d.confirmed, d
    assert d.meeting_time == expected_dt


@pytest.mark.parametrize("text", [
    "I don't want to book anything",
    "I'm not sure",
    "not sure about Tuesday at 10",
    "no thanks",
    "maybe Tuesday at 10",
    "Tuesday doesn't work",
    "can we do another day",
    "let me think about it",
    "Does Tuesday at 10 work?",
    "",
])
def test_refusals_hesitation_and_questions_never_confirm(text):
    assert resolve_booking(text, SLOTS, last_assistant_text="Tuesday at 10am?").confirmed is False


def test_positive_negative_phrases_are_not_refusals():
    d = resolve_booking("no problem, Tuesday at 10 works", SLOTS)
    assert d.confirmed and d.meeting_time == "2026-10-27T10:00:00+00:00"


def test_ambiguous_selection_is_not_confirmed():
    # Two Tuesday slots in the afternoon/morning range both match "Tuesday"
    d = resolve_booking("Tuesday", SLOTS)
    assert not d.confirmed and d.reason == "ambiguous"


def test_unknown_time_is_not_confirmed():
    d = resolve_booking("Tuesday at 4", SLOTS)
    assert not d.confirmed and d.reason == "no_slot"


def test_bare_yes_confirms_only_a_single_proposed_slot():
    d = resolve_booking("yes", SLOTS, last_assistant_text="Does Wednesday at 3pm work for you?")
    assert d.confirmed and d.meeting_time == "2026-10-28T15:00:00+00:00"


def test_bare_yes_after_multiple_options_is_ambiguous():
    d = resolve_booking("sounds good", SLOTS, last_assistant_text="I have Tuesday at 9am or Wednesday at 3pm.")
    assert not d.confirmed


def test_bare_yes_with_no_proposal_never_defaults_to_first_slot():
    d = resolve_booking("yes", SLOTS, last_assistant_text="Great, tell me a bit about your goals.")
    assert not d.confirmed and d.slot is None
    assert resolve_booking("yes", SLOTS).confirmed is False


def test_only_offered_slots_can_be_selected():
    many = [{"label": f"s{i}", "datetime": f"2026-10-27T{9 + i:02d}:00:00+00:00"} for i in range(12)]
    # 20:00 is the 12th slot; only the first 9 were shown to the assistant.
    assert resolve_booking("Tuesday at 8pm", many).confirmed is False


def test_match_slots_ignores_text_without_time_criteria():
    assert match_slots("I have a 3 bedroom house", SLOTS) == []
