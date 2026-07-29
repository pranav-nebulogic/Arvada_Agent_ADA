"""
config.py
=========
Central settings for the Arvada agent, loaded from .env via pydantic-settings.
Import the singleton `settings` everywhere instead of reading os.getenv directly.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── OpenAI ──────────────────────────────────────────────────────────────
    openai_api_key: str = ""

    # ── Postgres ────────────────────────────────────────────────────────────
    postgres_url: str = ""

    # ── Redis ───────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"

    # ── Models ──────────────────────────────────────────────────────────────
    # Tiered generation: fast model for everyday answers, deep (flagship) for
    # complex/legal queries. See agent/nodes/generate.py:pick_generation_model.
    generation_model_fast: str = "gpt-5.4"
    generation_model_deep: str = "gpt-5.5"
    generation_reasoning_effort_fast: str = "none"
    generation_reasoning_effort_deep: str = "low"
    intent_model: str = "gpt-5.4-nano"
    rerank_model: str = "gpt-5.4-nano"
    judge_model: str = "gpt-5.4-nano"
    embedding_model: str = "text-embedding-3-small"
    # gpt-5.4-nano supports none|low|medium|high|xhigh (NOT 'minimal').
    small_model_reasoning_effort: str = "none"

    # Debug: when true, the SSE `done` event carries per-node timing in ms.
    expose_timing: bool = False

    @property
    def generation_model(self) -> str:
        """Back-compat alias (eval/tools); defaults to the deep model."""
        return self.generation_model_deep

    # ── Retrieval ───────────────────────────────────────────────────────────
    retrieval_top_k: int = 10      # candidates from hybrid_search
    rerank_top_k: int = 5          # chunks kept after reranking
    # Minimum rerank relevance (0-10 scale) for a chunk to count as "relevant".
    rerank_min_score: float = 3.0
    # If no chunk clears this bar, the confidence gate routes to "I don't know".
    # Skip the LLM rerank when hybrid search already separated the winners: the
    # call is an extra round trip in front of the FIRST TOKEN on every grounded
    # answer (measured TTFT 4-8s). Fires when (top - first_dropped) >= gap * top,
    # i.e. the best candidate beats the best REJECTED one by this fraction.
    # Raise it to rerank more often (safer, slower); 0 disables the skip.
    #
    # Tuned against production retrieval, not guessed. RRF scores are inherently
    # bunched (~0.0164 == 1/61, i.e. rank 1 in ONE list; ~0.032 == rank 1 in
    # BOTH the FTS and vector lists). At 0.35 the skip fired on 3 of 8 sample
    # queries -- and precisely the ones where both retrievers agreed, which is
    # the signal we actually want. Anything less certain still pays for the LLM.
    rerank_skip_enabled: bool = True
    rerank_skip_score_gap: float = 0.35
    # Minimum top RRF score to skip the LLM at all. Skipping also skips the
    # rerank_min_score relevance check that drives `low_confidence` and the
    # "I don't know" route, so a weak-but-lopsided match must NOT take the fast
    # path. With RRF k=60: ~0.0164 == rank 1 in one list only; ~0.032 == rank 1
    # in both the FTS and vector lists. 0.025 sits between, so the skip requires
    # agreement from both retrievers.
    rerank_skip_min_top_score: float = 0.025

    # Run an UNFILTERED hybrid search concurrently with intent classification,
    # instead of waiting for intent and then searching. Measured: intent ~1.3s
    # and retrieve ~1.5s run back-to-back today, and only 3 of 12 sample queries
    # produce a permit_type at all.
    #
    # Speculative, never lossy: the prefetch is USED only when intent reports no
    # permit_type. When it does report one we discard the prefetch and run the
    # same filtered query as today, because the filter is not a subset of the
    # unfiltered ranking -- measured, the filtered top-10 for "what permits do I
    # need for a deck" shares 0 of 10 chunks with the unfiltered top-100, so
    # over-fetching cannot substitute for it.
    #
    # Net: ~75% of queries save an LLM call's worth of wall clock; ~25% pay one
    # wasted (concurrent) DB query.
    #
    # DEFAULT OFF: correct, but not worth it in production.
    #
    # Correctness is settled -- with the intent output held FIXED, 12/12 queries
    # returned byte-identical chunk sets and the prefetch was reused on 8 of 12.
    # (Holding intent fixed matters: a first attempt let the classifier re-run
    # per path, and its own run-to-run variance looked like a regression.)
    #
    # But an A/B on the deployed container showed NO win: median TTFT 9.37s with
    # this on vs 8.55s with it off, over 4 queries x 3 runs. The reason is that
    # the per-node numbers that motivated it were measured from a developer
    # laptop, where every DB round trip crosses a continent -- retrieve looked
    # like ~1.5s. In-datacenter the app and Postgres are in the same region, so
    # retrieve is a small fraction of that and overlapping it with a ~1.3s
    # classifier saves almost nothing, while still costing a speculative query.
    #
    # Kept because it is tested and lossless: flip to true (or set
    # PARALLEL_INTENT_RETRIEVE=true) if in-container timing ever shows retrieve
    # is actually expensive. Do not enable on laptop measurements again.
    parallel_intent_retrieve: bool = False

    # ── Cache / memory ──────────────────────────────────────────────────────
    # Hard ceiling on any single Redis call. The cache is the FIRST node in the
    # graph, so an unresponsive Redis (socket accepted, no reply) used to hang
    # every turn forever -- a refused connection was always handled, a wedged one
    # was not. Applied both as client socket timeouts and as an asyncio.wait_for.
    redis_timeout_seconds: float = 2.0
    semantic_cache_threshold: float = 0.92
    semantic_cache_ttl_seconds: int = 86400
    conversation_history_turns: int = 6

    # ── Server ──────────────────────────────────────────────────────────────
    agent_host: str = "0.0.0.0"
    agent_port: int = 8001
    log_level: str = "info"
    rate_limit_per_minute: int = 30   # per session_id (fallback: client IP)
    max_request_messages: int = 40    # reject oversized conversation threads

    # ── Auth / tenancy ──────────────────────────────────────────────────────
    internal_service_token: str = "local-dev-token-change-in-prod"
    default_city_id: str = "arvada-co"

    # ── Portal (request-type deep-links) ─────────────────────────────────────
    # Tenant hostname that serves BOTH surfaces, routed by path (ADR-0035): the
    # citizen apply flow at the root (/permits/apply/<code>) and the agent
    # workbench at /agent/*. When a message maps to a known request type, Ada
    # deep-links to the surface the requester belongs to — citizen vs staff (see
    # request_types.surface_for). Empty -> emit a relative path for the UI to
    # resolve. Ada guides; it never creates the application (AI out of the write
    # path). e.g. https://arvada.smartlp-pilot.nebulogic.com
    portal_base_url: str = ""

    # ── Engine integration (STATUS_LOOKUP) ──────────────────────────────────
    # Empty until the Spring proxy + s2s auth is wired (Phase 3); the
    # status_lookup tool gracefully falls back to the portal/phone when unset.
    engine_case_api_base: str = ""   # e.g. http://localhost:8080
    engine_s2s_token: str = ""
    engine_timeout_seconds: float = 4.0

    # ── Langfuse (optional) ──────────────────────────────────────────────────
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def langfuse_enabled(self) -> bool:
        return (
            self.langfuse_public_key.startswith("pk-lf-")
            and not self.langfuse_public_key.endswith("...")
            and self.langfuse_secret_key.startswith("sk-lf-")
            and not self.langfuse_secret_key.endswith("...")
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
