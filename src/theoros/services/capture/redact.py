"""Credential pattern redaction for captured text.

Scans content for known secret patterns (API keys, tokens, credit card
numbers, SSNs) and replaces matches with [REDACTED:<pattern_name>].
Runs before any text is written to raw_events — secrets never land on disk.

Patterns covered: anthropic_api_key, openai_api_key, aws_access_key,
stripe_key, jwt_token, credit_card (Luhn-validated), us_ssn.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Luhn check for credit card validation
# ---------------------------------------------------------------------------

def _luhn_check(digits: str) -> bool:
    """Return True if the digit string passes the Luhn algorithm."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Compiled patterns — order matters (more specific before less specific)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_api_key", re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}")),
    ("openai_api_key",    re.compile(r"sk-(?!ant-)(?:proj-)?[a-zA-Z0-9_-]{20,}")),
    ("aws_access_key",    re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("stripe_key",        re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[a-zA-Z0-9]{24,}\b")),
    ("jwt_token",         re.compile(r"\beyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")),
    ("credit_card",       re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("us_ssn",            re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
]


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Scan text for credential patterns and replace matches in-place.

    Returns (redacted_text, applied_patterns) where applied_patterns is a
    deduplicated list of pattern names that matched at least once, in the
    order the patterns were checked.
    """
    applied: list[str] = []

    for name, pattern in _PATTERNS:
        if name == "credit_card":
            # Credit cards need Luhn validation — use a callback.
            matched = False

            def _cc_replacer(m: re.Match[str]) -> str:
                nonlocal matched
                digits = re.sub(r"[ -]", "", m.group())
                if len(digits) < 13 or len(digits) > 19:
                    return m.group()
                if not _luhn_check(digits):
                    return m.group()
                matched = True
                return "[REDACTED:credit_card]"

            text = pattern.sub(_cc_replacer, text)
            if matched:
                applied.append(name)
        else:
            new_text = pattern.sub(f"[REDACTED:{name}]", text)
            if new_text != text:
                applied.append(name)
            text = new_text

    return text, applied
