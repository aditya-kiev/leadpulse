"""Decide whether a lead has genuinely confirmed a meeting slot.

The original node confirmed a booking whenever the lead's message contained a
substring such as "book", "sure" or "yes" and then silently fell back to the
first available slot.  That produced false confirmations for messages like
"I don't want to book anything" and "I'm not sure".

Rules implemented here (all deterministic, no LLM call, fully unit-tested):

1. Any refusal / hesitation / question  ->  never confirmed.
2. The lead names a slot (weekday and/or date and/or time) that matches
   exactly one offered slot  ->  confirmed for THAT slot.
3. A bare affirmative ("yes", "that works") confirms only when the assistant's
   previous message pointed at exactly one slot.  Otherwise we ask again.
4. We never default to "the first available slot".

False negatives are cheap (the assistant simply asks again); false positives
put a phantom meeting on the client's calendar, so every ambiguity resolves to
"not confirmed".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

# The assistant is only shown this many slots, so only these can be selected.
OFFERED_SLOT_LIMIT = 9

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_WEEKDAY_RE = re.compile(r"\b(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b")
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d:])", re.I)

# Phrases that LOOK negative but are positive.
_POSITIVE_NEGATIVES_RE = re.compile(r"\b(no problem|no worries|not a problem|no issue|can'?t wait)\b")

_NEGATIVE_RE = re.compile(
    r"\b(no|nope|nah|not|never|don'?t|do not|can'?t|cannot|won'?t|wont|doesn'?t|"
    r"isn'?t|aren'?t|unsure|maybe|perhaps|later|another|different|other time|"
    r"reschedul\w*|cancel\w*|stop|hold on|wait|think about|let me check|i'?ll let you know)\b"
)

_BARE_AFFIRMATIVE_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|alright|sounds good|that works|works for me|"
    r"perfect|great|confirm|confirmed|book it|let'?s do it|do it|please do|go ahead)"
    r"(?:[\s,.!]+(please|thanks|thank you|that works|sounds good|works for me|book it))*[\s.!]*$"
)


@dataclass(frozen=True)
class BookingDecision:
    confirmed: bool
    slot: dict[str, Any] | None
    reason: str  # confirmed | declined | question | no_slot | ambiguous | no_proposal | empty

    @property
    def meeting_time(self) -> str | None:
        return self.slot["datetime"] if self.confirmed and self.slot else None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _slot_dt(slot: dict[str, Any]) -> datetime | None:
    raw = slot.get("datetime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt


def _hour24(hour: int, meridiem: str | None) -> list[int]:
    """Candidate 24h hours for a spoken hour.  Bare 1-7 means afternoon."""
    if hour > 23:
        return []
    if meridiem:
        pm = meridiem.lower().startswith("p")
        if hour == 12:
            return [12 if pm else 0]
        if hour > 12:
            return [hour]
        return [hour + 12 if pm else hour]
    if hour >= 13:
        return [hour]
    if hour == 12:
        return [12]
    if 1 <= hour <= 7:
        return [hour + 12]
    return [hour]  # 8-11 -> morning


def _extract_criteria(text: str) -> dict[str, Any]:
    t = _normalise(text)
    criteria: dict[str, Any] = {"weekdays": set(), "dates": set(), "times": set()}

    for m in _WEEKDAY_RE.finditer(t):
        criteria["weekdays"].add(_WEEKDAYS[m.group(1)])

    for m in _MONTH_DAY_RE.finditer(t):
        criteria["dates"].add((_MONTHS[m.group(1)], int(m.group(2))))
    t_no_dates = _MONTH_DAY_RE.sub(" ", t)
    t_no_dates = re.sub(r"\b\d{1,2}(st|nd|rd|th)\b", " ", t_no_dates)

    for m in _TIME_RE.finditer(t_no_dates):
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        # A bare number with no colon / meridiem is only a time if it follows
        # "at"/"around"/"@", otherwise "2 options" would read as 2pm.
        if not meridiem and m.group(2) is None:
            before = t_no_dates[: m.start()].rstrip()
            if not re.search(r"(at|around|@|by|for)$", before):
                continue
        if minute > 59:
            continue
        for h in _hour24(hour, meridiem):
            criteria["times"].add((h, minute))
    return criteria


def match_slots(text: str, slots: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the slots that satisfy every criterion mentioned in ``text``.

    Returns an empty list when the text mentions no weekday, date or time.
    """
    c = _extract_criteria(text)
    if not (c["weekdays"] or c["dates"] or c["times"]):
        return []
    matches = []
    for slot in slots:
        dt = _slot_dt(slot)
        if dt is None:
            continue
        if c["weekdays"] and dt.weekday() not in c["weekdays"]:
            continue
        if c["dates"] and (dt.month, dt.day) not in c["dates"]:
            continue
        if c["times"] and (dt.hour, dt.minute) not in c["times"]:
            continue
        matches.append(slot)
    return matches


def resolve_booking(
    user_text: str,
    slots: Sequence[dict[str, Any]],
    last_assistant_text: str | None = None,
    offered_limit: int = OFFERED_SLOT_LIMIT,
) -> BookingDecision:
    """Return a :class:`BookingDecision` for the lead's latest message."""
    text = _normalise(user_text)
    if not text:
        return BookingDecision(False, None, "empty")

    offered = list(slots)[:offered_limit]

    # Rule 1: refusals and hesitation always win.
    cleaned = _POSITIVE_NEGATIVES_RE.sub(" ", text)
    if _NEGATIVE_RE.search(cleaned):
        return BookingDecision(False, None, "declined")
    if "?" in text:
        return BookingDecision(False, None, "question")

    # Rule 2: the lead named a slot themselves.
    matches = match_slots(text, offered)
    if len(matches) == 1:
        return BookingDecision(True, matches[0], "confirmed")
    if len(matches) > 1:
        return BookingDecision(False, None, "ambiguous")
    if _extract_criteria(text)["weekdays"] or _extract_criteria(text)["times"] or _extract_criteria(text)["dates"]:
        return BookingDecision(False, None, "no_slot")

    # Rule 3: bare affirmative -> only valid against ONE slot the assistant named.
    if _BARE_AFFIRMATIVE_RE.match(text):
        if not last_assistant_text:
            return BookingDecision(False, None, "no_proposal")
        proposed = match_slots(last_assistant_text, offered)
        if len(proposed) == 1:
            return BookingDecision(True, proposed[0], "confirmed")
        return BookingDecision(False, None, "ambiguous" if proposed else "no_proposal")

    return BookingDecision(False, None, "no_slot")


def booking_status_instruction(decision: BookingDecision, offered_labels: Sequence[str]) -> str:
    """Extra system-prompt text so the reply always matches the real decision."""
    if decision.confirmed and decision.slot:
        return (
            "BOOKING STATUS: CONFIRMED for "
            f"{decision.slot.get('label')}. Confirm this exact time warmly in one or two "
            "sentences and say the team will follow up. Do not offer other times."
        )
    options = "; ".join(list(offered_labels)[:3])
    base = (
        "BOOKING STATUS: NOT CONFIRMED. Do NOT say the meeting is booked, scheduled or "
        "confirmed. "
    )
    if decision.reason == "declined":
        return base + (
            "The lead hesitated or declined. Acknowledge that, ask what would suit them "
            f"better, and offer at most three options: {options}."
        )
    if decision.reason == "ambiguous":
        return base + "More than one time could match. Ask which single time they want."
    return base + (
        f"Offer at most three of these times and ask which one they prefer: {options}."
    )
