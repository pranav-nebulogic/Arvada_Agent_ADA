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

    # ── Cache / memory ──────────────────────────────────────────────────────
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
