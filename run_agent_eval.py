"""
run_agent_eval.py
=================
End-to-end answer eval (not just retrieval): runs the full LangGraph
(contextualize -> retrieve -> rerank -> gate -> generate/idk) for each golden
query and checks the GENERATED answer.

Adds:
  - a multi-turn follow-up test (pronoun resolution via contextualize)
  - the adversarial query test (must refuse / say "I don't have that", not hallucinate)

Usage:
  python run_agent_eval.py            # full set
  python run_agent_eval.py --limit 3  # quick smoke test
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from sources import get_golden_queries, get_multi_turn
from city_config import get_city
from config import settings
from agent.graph import get_graph
from agent.state import AgentState
import db

_REFUSAL_MARKERS = [
    "don't have", "do not have", "couldn't find", "could not find", "unable",
    "not have that", "contact the city", "don't currently",
    "no information", "isn't something", "not something", "doesn't regulate",
    "does not regulate", "out of", "not able",
]


def refusal_markers(city_id: str | None = None) -> list[str]:
    """Refusal phrases, plus THIS city's own website host.

    The host used to be the literal "arvadaco.gov", so a Woodinville refusal
    pointing at woodinville.gov scored as a hallucination -- the eval was
    grading every city against Arvada's copy.
    """
    host = (get_city(city_id).website or "").replace("https://", "").replace("http://", "").strip("/")
    return _REFUSAL_MARKERS + ([host.lower()] if host else [])


REFUSAL_MARKERS = refusal_markers()


def _final(result) -> AgentState | dict:
    return result


def _get(result, field: str):
    if isinstance(result, dict):
        return result.get(field)
    return getattr(result, field, None)


async def _run_once(query: str, messages: list[dict] | None = None, lang: str = "en"):
    graph = get_graph()
    state = AgentState(
        city_id=settings.default_city_id,
        session_id=str(uuid.uuid4()),
        query=query,
        messages=messages or [{"role": "user", "content": query}],
        lang=lang,
    )
    result = await graph.ainvoke(state)
    return {
        "answer": _get(result, "answer") or "",
        "citations": _get(result, "citations") or [],
        "low_confidence": _get(result, "low_confidence"),
        "standalone": _get(result, "standalone_query"),
        "reranked": _get(result, "reranked_chunks") or [],
    }


def _check_answer(ans: str, golden: dict) -> tuple[bool, str]:
    ans_l = ans.lower()
    if golden.get("agent_only"):
        # adversarial / jurisdiction: must NOT fabricate -> expect refusal markers
        if any(m in ans_l for m in REFUSAL_MARKERS):
            return True, "correctly declined / redirected"
        return False, "expected a decline/redirect but got a confident answer"

    keywords = [k.lower() for k in golden.get("expected_answer_contains", [])]
    if not keywords:
        return (bool(ans.strip()), "non-empty answer" if ans.strip() else "empty answer")
    found = [k for k in keywords if k in ans_l]
    if len(found) / len(keywords) >= 0.5:
        return True, f"{len(found)}/{len(keywords)} keywords in answer"
    return False, f"only {len(found)}/{len(keywords)} keywords; missing {[k for k in keywords if k not in found]}"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # Golden set for the CITY UNDER TEST. This used to import Arvada's list
    # unconditionally while taking city_id from settings, so pointing the eval at
    # Woodinville graded Arvada's questions against Woodinville's corpus.
    city = get_city(settings.default_city_id)
    all_queries = get_golden_queries(settings.default_city_id)
    queries = all_queries[: args.limit] if args.limit else all_queries

    print(f"\n  {city.assistant_name} — End-to-End Answer Eval — {city.city_name}")
    print(f"    generation fast={settings.generation_model_fast} deep={settings.generation_model_deep}  small={settings.intent_model}")
    print(f"    rerank_skip={settings.rerank_skip_enabled} parallel_retrieve={settings.parallel_intent_retrieve}")
    print(f"    {len(queries)} golden queries  (city_id={city.city_id})\n")
    print(f"{'#':>2}  {'Query':<48} {'Result'}")
    print("-" * 90)

    passed = 0
    details = []
    for i, golden in enumerate(queries, 1):
        q = golden["query"]
        try:
            r = await _run_once(q)
            ok, reason = _check_answer(r["answer"], golden)
        except Exception as exc:  # noqa: BLE001
            ok, reason, r = False, f"ERROR: {exc}", {"answer": "", "citations": []}
        passed += int(ok)
        status = "PASS" if ok else "FAIL"
        print(f"{i:>2}  {q[:48]:<48} {status}  ({reason})")
        details.append((i, q, ok, reason, r))

    # Multi-turn follow-up test (pronoun resolution).
    print("-" * 90)
    print("Multi-turn follow-up test:")
    # Per-city fixture: the setup names a permit that city actually issues.
    # Woodinville has no solar permit programme, so Arvada's fixture tested nothing.
    fixture = get_multi_turn(settings.default_city_id)
    if not fixture:
        print(f"    SKIPPED — no multi-turn fixture for {city.city_id}")
        mt_ok = True
    else:
        mt_messages = [*fixture["setup"], {"role": "user", "content": fixture["follow_up"]}]
        mt = await _run_once(fixture["follow_up"], messages=mt_messages)
        haystack = f"{(mt['standalone'] or '').lower()} {mt['answer'].lower()}"
        mt_ok = any(s.lower() in haystack for s in fixture["expect_any"])
        print(f"    standalone query: {mt['standalone']!r}")
        print(f"    answer: {mt['answer'][:160]!r}")
        print(f"    -> {'PASS' if mt_ok else 'FAIL'} ({fixture['describe']})")

    print("-" * 90)
    score = passed / len(queries) if queries else 0
    print(f"\n    Golden score: {passed}/{len(queries)} ({score:.0%})\n")

    # Show a couple of sample answers
    print("Sample answers:")
    for i, q, ok, reason, r in details[:3]:
        print(f"\n  [{i}] {q}")
        print(f"      {r['answer'][:280]}")
        if r["citations"]:
            print(f"      citations: {[c.get('title') for c in r['citations'][:3]]}")

    await db.close_pool()
    return score


if __name__ == "__main__":
    asyncio.run(main())
