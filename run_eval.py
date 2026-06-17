"""
run_eval.py
===========
Evaluates retrieval quality against the 20 golden queries defined in sources.py.

For each query:
  1. Embed the question with text-embedding-3-small
  2. Call hybrid_search() in Postgres (RRF of vector + FTS)
  3. Check whether the expected permit_type / section_type / answer keywords
     appear in the top-5 results

Prints a pass/fail table and an overall score.

Run:
  python run_eval.py
"""

import asyncio
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

from sources import GOLDEN_QUERIES

CITY_ID       = "arvada-co"
EMBED_MODEL   = "text-embedding-3-small"
TOP_K         = 5
PASS_MARK     = 0.70   # fraction of queries that must pass to consider KB healthy

client = AsyncOpenAI()


async def embed(text: str) -> list[float]:
    r = await client.embeddings.create(model=EMBED_MODEL, input=text)
    return r.data[0].embedding


async def search(conn, query: str, permit_type: str | None, embedding: list[float]):
    # asyncpg doesn't know the vector type natively — cast via SQL
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    rows = await conn.fetch(
        "SELECT chunk_id, article_id, rrf_score, content_text, section_type, metadata "
        "FROM hybrid_search($1, $2::vector, $3, $4, $5)",
        query,
        vec_str,
        CITY_ID,
        permit_type,
        TOP_K,
    )
    return rows


def _check(rows, golden: dict) -> tuple[bool, list[str]]:
    """Return (passed, reasons).

    A query PASSES if:
      - The expected permit_type appears in the returned metadata  (hard requirement)
      - At least half of the expected keywords appear in the text  (soft requirement)

    Agent-only checks (adversarial/jurisdiction/escalation) skip keyword matching
    since those depend on the LLM layer, not raw retrieval.
    """
    reasons = []
    combined_text = " ".join((r["content_text"] or "").lower() for r in rows)
    combined_meta = " ".join(str(r["metadata"]) for r in rows).lower()
    is_agent_only = golden.get("agent_only", False)

    # 1. Retrieval: expected permit_type must appear in metadata
    expected_pt = golden.get("expected_permit_type")
    if expected_pt:
        if expected_pt in combined_meta:
            reasons.append(f"✅ permit_type '{expected_pt}' found")
        else:
            reasons.append(f"❌ permit_type '{expected_pt}' NOT in top-{TOP_K}")
            return False, reasons

    # 2. Section type (informational only — not a hard pass/fail)
    expected_sec = golden.get("expected_section")
    if expected_sec:
        sections_returned = [r["section_type"] for r in rows]
        if expected_sec in sections_returned:
            reasons.append(f"✅ section '{expected_sec}' present")
        else:
            reasons.append(f"⚠️  section '{expected_sec}' not in top-{TOP_K} "
                           f"(got: {sorted(set(sections_returned))})")

    # 3. Keyword check — skip for agent-only queries
    keywords = golden.get("expected_answer_contains", [])
    if keywords and not is_agent_only:
        found = [kw for kw in keywords if kw.lower() in combined_text]
        missing = [kw for kw in keywords if kw.lower() not in combined_text]
        hit_rate = len(found) / len(keywords)
        if hit_rate >= 0.5:
            reasons.append(f"✅ {len(found)}/{len(keywords)} keywords found"
                           + (f" (missing: {missing})" if missing else ""))
        else:
            reasons.append(f"❌ only {len(found)}/{len(keywords)} keywords found"
                           f" — missing: {missing}")
            return False, reasons

    # 4. Escalation (agent-layer check — pass if contact info surfaced)
    if golden.get("expected_intent") == "ESCALATE":
        if any(kw in combined_text for kw in ["720-898", "building services", "contact"]):
            reasons.append("✅ escalation signals found in results")
        else:
            reasons.append("⚠️  escalation signals not in top-5 (agent must handle)")

    return True, reasons


async def run_eval():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        print("ERROR: POSTGRES_URL not set in .env")
        sys.exit(1)

    pool = await asyncpg.create_pool(
        postgres_url, min_size=1, max_size=3,
        server_settings={"search_path": "arvada_agent, public"},
    )

    print("\n🔍  Arvada Agent — Retrieval Eval")
    print(f"    {len(GOLDEN_QUERIES)} golden queries | top-{TOP_K} | city={CITY_ID}\n")
    print(f"{'#':>2}  {'Query':<52} {'Result'}")
    print("-" * 80)

    passed = 0
    results = []

    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, false)", CITY_ID
        )
        for i, golden in enumerate(GOLDEN_QUERIES, 1):
            query = golden["query"]
            permit_type = golden.get("expected_permit_type")
            embedding = await embed(query)
            rows = await search(conn, query, permit_type, embedding)

            ok, reasons = _check(rows, golden)
            passed += int(ok)
            status = "✅ PASS" if ok else "❌ FAIL"
            results.append((i, query, ok, reasons, rows))
            print(f"{i:>2}  {query[:52]:<52} {status}")

    print("-" * 80)
    score = passed / len(GOLDEN_QUERIES)
    verdict = "✅ HEALTHY" if score >= PASS_MARK else "❌ NEEDS WORK"
    print(f"\n    Score: {passed}/{len(GOLDEN_QUERIES)} ({score:.0%})  →  {verdict}\n")

    # Print details for failures
    failures = [(i, q, r, rows) for i, q, ok, r, rows in results if not ok]
    if failures:
        print("─" * 80)
        print("FAILURE DETAILS\n")
        for i, query, reasons, rows in failures:
            print(f"  [{i}] {query}")
            for r in reasons:
                print(f"       {r}")
            if rows:
                print(f"       Top result: {(rows[0]['content_text'] or '')[:120]!r}")
            print()

    try:
        await pool.close()
    except OSError:
        pass

    return score


if __name__ == "__main__":
    score = asyncio.run(run_eval())
    sys.exit(0 if score >= PASS_MARK else 1)
