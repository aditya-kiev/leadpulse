import hashlib
import re


def safe_text(content):
    """Extract plain text from an AIMessage content that may be str or list[dict]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def log_fingerprint(content) -> str:
    """Return a length + SHA-256 prefix fingerprint instead of raw lead content.

    Never log user/assistant message text in plaintext (lead PII).  This
    helper keeps traceability (length + stable hash) without leaking content.
    """
    text = safe_text(content)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"len={len(text)} sha256={digest}"


_PERIODIC_RE = re.compile(r"\s*(/mo|/month|/yr|/year|per\s*month|per\s*year)\s*$", re.I)

_NUMBER_RE = re.compile(
    r"^(?P<value>[\d.]+)\s*(?P<unit>m|mn|million|M|lakh|L|crore|Cr|cr|k|K)?$"
)
_RANGE_SEP_RE = re.compile(r"\s*(?:-|to)\s*")

_CRORE = 10_000_000
_LAKH = 100_000
_THOUSAND = 1_000
_MILLION = 1_000_000


def _parse_value(value_str: str, unit_str: str | None) -> float | None:
    try:
        value = float(value_str)
    except ValueError:
        return None
    unit = (unit_str or "").lower()
    if unit in ("cr", "crore"):
        return value * _CRORE
    if unit in ("l", "lakh"):
        return value * _LAKH
    if unit in ("k",):
        return value * _THOUSAND
    if unit in ("m", "mn", "million"):
        return value * _MILLION
    return value


def parse_budget(raw: object) -> float | None:
    """Convert a budget value to a plain float (USD).

    Handles ``"650k"``, ``"1.2M"``, ``"$1.2 million"``, ``"80 lakh"``,
    ``"1.2 crore"``, ``"50k"``, ``"$150/month"``, ``"$3,200/mo"``, and
    plain numbers.  Strips ``$``, ``₹``, commas, and whitespace first.

    Strips periodic suffixes (``/mo``, ``/month``, ``/yr``, etc.) but does
    *not* annualize — the parsed number is preserved as-is.

    Handles ranges (e.g. ``"80-100 lakh"``, ``"50k-80k"``, ``"10-20"``)
    by averaging the two endpoints.  Returns *None* for unparseable input.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    cleaned = str(raw).replace("$", "").replace("₹", "").replace(",", "").strip()
    # Strip periodic suffixes before parsing number+unit
    cleaned = _PERIODIC_RE.sub("", cleaned).strip()
    if not cleaned:
        return None

    # --- Range with unit after the right value  (e.g. "80-100 lakh") ---
    m = re.match(
        r"^(?P<left>[\d.]+)\s*(?:-|to)\s*(?P<right>[\d.]+)\s*(?P<unit>m|mn|million|M|lakh|L|crore|Cr|cr|k|K)?$",
        cleaned,
    )
    if m:
        l = _parse_value(m.group("left"), m.group("unit"))
        r = _parse_value(m.group("right"), m.group("unit"))
        if l is not None and r is not None:
            return (l + r) / 2.0
        if l is not None:
            return l
        if r is not None:
            return r

    # --- Range with unit embedded in each number (e.g. "50k-80k") ---
    parts = _RANGE_SEP_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        left = _parse_single(parts[0])
        right = _parse_single(parts[1])
        if left is not None and right is not None:
            return (left + right) / 2.0
        if left is not None:
            return left
        if right is not None:
            return right

    # --- Single value ---
    return _parse_single(cleaned)


def _parse_single(raw: str) -> float | None:
    m = _NUMBER_RE.match(raw)
    if not m:
        return None
    return _parse_value(m.group("value"), m.group("unit"))
