"""Shared utilities for PDF/CSV statement parsing."""

import logging
import re

logger = logging.getLogger(__name__)


def clean_amount(s):
    """Clean a dollar amount string and return a float.

    Handles: $1,234.56  -$1,234.56  ($1,234.56)  1234.56  -1234.56
    Parenthesized values are treated as negative.
    Returns 0.0 for unparseable input.
    """
    if not s:
        return 0.0
    s = s.strip()
    is_negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.replace(",", "").replace("$", "").replace("(", "").replace(")", "").replace("-", "")
    # Remove trailing letters (e.g. Fidelity FIFO marker "f")
    s = re.sub(r"[a-zA-Z]+$", "", s).strip()
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if is_negative else val


def clean_amount_unsigned(s):
    """Clean a dollar amount string, always returning a positive float.

    Use this when sign logic is handled separately by the caller
    (e.g., checking if line is in PAYMENTS section).
    """
    return abs(clean_amount(s))
