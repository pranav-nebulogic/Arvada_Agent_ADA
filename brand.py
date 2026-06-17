"""
brand.py
========
Scrub competitor product names from every user-facing string.

We never surface the incumbent permitting portal by name. All DB read paths
(article pages, search, chat retrieval context, citations) route user-facing
text through ``scrub()`` so the only portal ever named is our product,
Nebulogic's **SMART License & Permits**. Scrubbing the retrieval context also
means the LLM never even sees the competitor name, so it can't echo it.

To suppress another name later, add it to ``_PATTERNS``.
"""
from __future__ import annotations

import re
from typing import Any

PRODUCT_NAME = "SMART License & Permits"
PRODUCT_FULL = "Nebulogic's SMART License & Permits"
PORTAL_URL = "https://aca-prod.accela.com/ARVADA"  # our portal — replaces incumbent links

# (compiled pattern -> replacement). Case-insensitive. Order matters: rewrite
# whole incumbent-portal URLs first, THEN any remaining bare name mentions.
# Matches eTRAKiT / eTRAKIT / e-TRAKiT / etrakit and the versioned "eTRAKiT3"
# (the trailing digit defeats a \b word boundary, so match \d* explicitly).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 1) Any URL pointing at the incumbent portal → our portal URL.
    (re.compile(r"https?://[^\s)\"'<>]*e[-\s]?trakit[^\s)\"'<>]*", re.IGNORECASE), PORTAL_URL),
    # 2) Bare product-name mentions (incl. "eTRAKiT3").
    (re.compile(r"\be[-\s]?trakit\d*\b", re.IGNORECASE), PRODUCT_NAME),
    # 3) The parent platform name on its own ("TRAKiT" / "TRAKiT3"). The \b
    #    boundary means this never fires inside "eTRAKiT" (handled above).
    (re.compile(r"\btrakit\d*\b", re.IGNORECASE), PRODUCT_NAME),
]


def scrub(text: Any) -> Any:
    """Replace competitor mentions in a single string (pass-through otherwise)."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_obj(obj: Any) -> Any:
    """Recursively scrub every string inside a str/list/dict structure."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj
