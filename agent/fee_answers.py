"""
fee_answers
===========
Turns what the citizen said ("4 bedrooms, detached, no ADU") into the engine's
fee-driving answer codes.

The vocabulary is NOT ours: `enumValues`, `dataType` and `label` all come from
the tenant's own `/fields` config, so this stays portable -- a new city's drivers
work with no change here.

Two-stage on purpose:

  1. an LLM extracts candidate values against the field definitions, and
  2. `validate()` -- pure, deterministic, unit-tested -- throws away anything
     that is not a legal value for its field.

Stage 2 is what makes this safe. An enum value the model invented is DROPPED
rather than guessed at, because a wrong `housing_type` silently moves the school
impact fee by thousands of dollars, and a wrong number is far worse than a
missing one (a missing one is simply asked for).
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import settings
from agent import llm
from agent.obs import log

_TRUE = {"yes", "true", "y", "1"}
_FALSE = {"no", "false", "n", "0"}


def _coerce(field: dict, raw: Any) -> Any | None:
    """One raw value -> a legal value for this field, or None to drop it."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    dtype = (field.get("dataType") or "").lower()
    options = field.get("enumValues") or []

    if dtype == "enum" or options:
        if not options:
            return None
        # Exact first, then case-insensitive, then a contained-word match so
        # "detached" resolves to "Detached single-family". Never a fuzzy guess.
        for opt in options:
            if text == opt:
                return opt
        low = text.lower()
        for opt in options:
            if low == str(opt).lower():
                return opt
        hits = [opt for opt in options if low in str(opt).lower()
                or str(opt).lower().startswith(low)]
        return hits[0] if len(hits) == 1 else None

    if dtype == "boolean":
        low = text.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        return None

    if dtype in ("int", "integer"):
        m = re.search(r"-?\d+", text.replace(",", ""))
        return m.group(0) if m else None

    if dtype in ("numeric", "number", "decimal"):
        m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        return m.group(0) if m else None

    return text


def validate(drivers: list[dict], candidate: dict) -> dict:
    """Keep only values that are legal for a KNOWN fee-driving field.

    Pure and deterministic -- this is the guard, not the LLM.
    """
    by_code = {d.get("code"): d for d in (drivers or []) if d.get("code")}
    out: dict[str, Any] = {}
    for code, raw in (candidate or {}).items():
        field = by_code.get(code)
        if not field:
            continue                       # not a fee driver for this type
        value = _coerce(field, raw)
        if value is not None:
            out[code] = value
    return out


def apply_defaults(drivers: list[dict], answers: dict) -> tuple[dict, list[str]]:
    """Fill unanswered drivers from their configured `defaultValue`.

    Mirrors the portal's calculator, which opens with each field's default. A
    field with no default stays unanswered and is still asked for.

    -> (answers, labels that were defaulted) so the answer can DISCLOSE them.
    Silently defaulting `housing_type` would pick a school-impact tier worth
    thousands of dollars on the citizen's behalf without saying so.
    """
    out = dict(answers or {})
    defaulted: list[str] = []
    for d in drivers or []:
        code = d.get("code")
        if not code or code in out:
            continue
        raw = d.get("defaultValue")
        if raw in (None, ""):
            continue
        value = _coerce(d, raw)
        if value is not None:
            out[code] = value
            defaulted.append(d.get("label") or code)
    return out, defaulted


def _schema_block(drivers: list[dict]) -> str:
    lines = []
    for d in drivers:
        bits = [f'- "{d.get("code")}" ({d.get("dataType")})']
        if d.get("label"):
            bits.append(f'— {d["label"]}')
        if d.get("enumValues"):
            bits.append(f'— one of {json.dumps(d["enumValues"])}')
        lines.append(" ".join(bits))
    return "\n".join(lines)


EXTRACT_SYSTEM = """You extract fee-driving values from a resident's message for a city permit \
estimate.

Return ONLY a JSON object mapping field code -> value, using EXACTLY the field codes listed. \
Rules:
- Include a field ONLY if the message actually states it. Never infer, never default, never guess.
- For an enum, copy one of the listed values VERBATIM.
- For a number, return digits only.
- If nothing was stated, return {}."""


async def extract(drivers: list[dict], text: str) -> dict:
    """Values the user actually stated, validated against the field definitions."""
    if not drivers or not (text or "").strip():
        return {}
    try:
        raw = await llm.complete_json(
            model=settings.intent_model,
            system=EXTRACT_SYSTEM,
            user=(f"Fields:\n{_schema_block(drivers)}\n\n"
                  f"Resident message:\n{text}\n\nJSON:"),
            reasoning_effort=settings.small_model_reasoning_effort,
        )
    except Exception as exc:  # noqa: BLE001 -- no answers just means we ask for them
        log.warning("fee_answer_extract_failed", error=str(exc))
        return {}
    if not isinstance(raw, dict):
        return {}
    kept = validate(drivers, raw)
    dropped = sorted(set(raw) - set(kept))
    if dropped:
        # Visible on purpose: a driver the model kept inventing is a config or
        # prompt problem, and silently dropping it would hide that.
        log.info("fee_answers_dropped", dropped=dropped, kept=sorted(kept))
    return kept
