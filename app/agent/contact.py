"""Lead contact capture (email / phone).

Why this module exists
----------------------
A qualified lead the office cannot call back is worth nothing.  The original
agent collected name, budget, timeline, etc. but never a phone number or an
email, so alerts and CRM pushes carried nothing actionable.

Everything here is deterministic (regex + templates, no LLM call):

* ``extract_contact``      - pull an email / phone out of free text
* ``is_contact_decline``   - did the lead refuse to share contact details?
* ``build_contact_request``- the question the agent asks (no LLM, no hallucination)
* ``contact_missing``      - single source of truth for "do we still need it?"
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from app.config.settings import settings

# Substring present in every contact request we send.  Used to recognise, on
# the NEXT turn, that the lead's reply is an answer to our contact question.
CONTACT_MARKER = "phone number or email"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

# 10-15 digits with optional separators / country code / parentheses.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w$])\+?\(?\d[\d\s().\-]{8,20}\d(?![\w])")

_DECLINE_RE = re.compile(
    r"^\s*(no|nope|nah|skip|pass)\b"
    r"|\b(prefer not|rather not|not comfortable|no thanks|not now|skip (it|that)"
    r"|don'?t (want|wish) to (share|give)|do not want to (share|give)|keep (it|that) private)\b",
    re.I,
)


def extract_contact(text: str | None) -> tuple[str | None, str | None]:
    """Return ``(email, phone)`` found in ``text`` (either may be ``None``).

    Emails are lower-cased.  Phones are normalised to digits with an optional
    leading ``+`` and must contain 10-15 digits, so budgets such as
    ``$1,200,000`` or ``650k`` are never mistaken for phone numbers.
    """
    if not text:
        return None, None

    email_match = _EMAIL_RE.search(text)
    email = email_match.group(0).lower() if email_match else None

    # Remove the email first so digits inside it can't be read as a phone.
    scrubbed = _EMAIL_RE.sub(" ", text)
    phone = None
    for m in _PHONE_CANDIDATE_RE.finditer(scrubbed):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 10 <= len(digits) <= 15:
            phone = ("+" if raw.strip().startswith("+") else "") + digits
            break
    return email, phone


def is_contact_decline(text: str | None) -> bool:
    """True if ``text`` reads as a refusal to share contact details."""
    if not text:
        return False
    return bool(_DECLINE_RE.search(text))


def has_contact(state: Mapping[str, Any]) -> bool:
    return bool(state.get("lead_email") or state.get("lead_phone"))


def contact_required(state: Mapping[str, Any]) -> bool:
    """Contact is required unless the feature is off or the lead declined."""
    return bool(settings.require_contact_info) and not state.get("contact_declined")


def contact_missing(state: Mapping[str, Any]) -> bool:
    return contact_required(state) and not has_contact(state)


def last_assistant_asked_for_contact(state: Mapping[str, Any]) -> bool:
    for msg in reversed(state.get("conversation_history") or []):
        if msg.get("role") == "assistant":
            return CONTACT_MARKER in (msg.get("content") or "").lower()
    return False


def build_contact_request(state: Mapping[str, Any], asked_before: bool = False) -> str:
    """The exact question we ask for contact details (deterministic)."""
    business = state.get("business_name") or settings.business_name or "our team"
    first = (state.get("lead_name") or "").strip().split(" ")[0]
    greet = f"Thanks, {first}! " if first else "Thanks! "
    if asked_before:
        return (
            f"I didn't catch a {CONTACT_MARKER} in that. Could you share one so "
            f"{business} can reach you? If you'd rather not, just say \"skip\"."
        )
    return (
        f"{greet}So {business} can follow up with the details, "
        f"what's the best {CONTACT_MARKER} to reach you?"
    )
