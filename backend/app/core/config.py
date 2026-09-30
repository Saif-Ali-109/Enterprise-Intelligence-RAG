"""Environment-driven configuration.

Constitution Principle VII: every behavioural knob is configuration, not a
constant in source. There is no second place a value can be set, and no accessor
that hands a secret back to a caller who might display it.

The two rules that shape this module:

1. **Secrets are `SecretStr`.** Not a naming convention — a type. A
   `SecretStr` renders as ``**********`` in every f-string, in every log record,
   and in every ``repr``. The type is what makes "never log a secret" a property
   of the system rather than a rule someone has to remember. `has_secret()` is
   the only accessor, and it returns a bool.

2. **There is exactly one instance.** `get_settings()` is cached, so the test
   suite and the running application observe the same values and a mid-request
   re-read cannot disagree with startup. `reset_settings()` exists for tests
   that need to change the environment, and clears the cache.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ============================================================================
# Domain vocabulary
# ============================================================================
# These are the values that appear in the flat `product` and `category` metadata
# fields, and in the classification prompt. They live here rather than in the
# prompt or the filter builder because three modules have to agree on them, and
# two copies of a domain list is one copy too many.


class ProductDomain(StrEnum):
    """The documentation domains the corpus is drawn from (data-model.md §5)."""

    JIRA = "jira"
    CONFLUENCE = "confluence"
    JSM = "jsm"
    DEVELOPER = "developer"


class QuestionIntent(StrEnum):
    """What the asker wants. Drives generation, not retrieval (FR-009)."""

    FACTUAL = "factual"
    HOW_TO = "how_to"
    TROUBLESHOOT = "troubleshoot"
    COMPARISON = "comparison"
    EXPLAIN_CONCEPT = "explain_concept"
    UNKNOWN = "unknown"


class PageType(StrEnum):
    """The filterable page classification (FR-024)."""

    GUIDE = "guide"
    REFERENCE = "reference"
    TUTORIAL = "tutorial"
    FAQ = "faq"
    TROUBLESHOOTING = "troubleshooting"
    API = "api"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    """The three-way grounding verdict (FR-006)."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"


# ============================================================================
# Settings
# ============================================================================


class Settings(BaseSettings):
    """All runtime configuration, resolved from the environment.

    Defaults below are the values in quickstart.md §2. They are chosen, not
    arbitrary: the two score floors are deliberately **absent** rather than
    defaulted, because an invented threshold makes the refusal path fire at an
    arbitrary point and turns the quality gates into decoration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Left at the default (case-insensitive), which is what makes
        # `groq_api_key` read from `GROQ_API_KEY`. Setting `case_sensitive=True`
        # would match the environment variable name LITERALLY against the field
        # name, so only a variable spelled `groq_api_key` would ever be found
        # and the two required keys would appear to be unset with no explanation
        # for why.
        #
        # `extra="ignore"`: a variable present in the environment but not
        # declared here is not this class's business to reject. Requiring every
        # variable on the host to be known to the application is how a
        # deployment starts failing on an unrelated change.
        extra="ignore",
    )

    # -- secrets (the only two required) ------------------------------------
    groq_api_key: SecretStr = Field(
        ...,
        description="Groq API key. Required; the process refuses to start without it.",
    )
    pinecone_api_key: SecretStr = Field(
        ...,
        description="Pinecone API key. Required; the process refuses to start without it.",
    )

    # -- vector store -------------------------------------------------------
    pinecone_index_name: str = "enterprise-knowledge"
    pinecone_namespace: str = "atlassian-public"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    # R-001: a 512-token embedder would silently truncate 600-1000 token chunks.
    pinecone_embed_model: str = "llama-text-embed-v2"
    pinecone_rerank_model: str = "bge-reranker-v2-m3"

    # -- language model -----------------------------------------------------
    # R-006: llama-3.3-70b-versatile is Enterprise-only and 401s elsewhere.
    groq_model: str = "openai/gpt-oss-120b"
    groq_classification_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = 30.0
    # Owned by the application, not compounded by the SDK (R-008).
    groq_max_retries: int = 1

    # -- database -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://knowledge:knowledge@localhost:5432/knowledge"
    database_pool_size: int = 5
    database_max_overflow: int = 5

    # -- crawler ------------------------------------------------------------
    allowed_domains: list[str] = Field(
        default_factory=lambda: [
            "support.atlassian.com",
            "developer.atlassian.com",
            "confluence.atlassian.com",
        ],
    )
    crawl_max_pages: int = 100
    crawl_max_depth: int = 2
    crawl_delay_seconds: float = 1.0
    crawl_max_bytes: int = 5_242_880
    crawl_timeout_seconds: float = 20.0
    crawl_user_agent: str = (
        "EnterpriseKnowledgeRAG/1.0 "
        "(+https://github.com/Saif-Ali-109/Enterprise-Intelligence-RAG; "
        "independent demonstration; not affiliated with any documentation publisher)"
    )

    # -- ingestion: chunk sizing -------------------------------------------
    chunk_target_min_tokens: int = 600
    chunk_target_max_tokens: int = 1000
    chunk_overlap_tokens: int = 100
    chunk_min_useful_tokens: int = 50

    # **The load-bearing size bound, measured 2026-09-30.** T027 wrote to a live
    # index and read this off a rejection:
    #   `Metadata size is 76009 bytes, which exceeds the limit of 40960 bytes
    #    per vector`
    # It is a *byte* limit, the record's text counts against it, and exceeding it
    # **rejects the write** — so an over-large chunk is a hard ingest error, not
    # a silently incomplete embedding.
    #
    # This is a service constant, not a preference, and it is the one bound that
    # can refuse a record. `embed_max_record_bytes` below is what the chunker
    # aims at, leaving room for the metadata that travels with the text.
    embed_bytes_per_vector: int = 40960

    # The fraction of the limit one record's *text* may occupy. The remainder
    # carries `heading_path`, `source_url`, `document_id` and the rest of
    # data-model.md §5, plus JSON overhead in the request. Measured: prose is
    # ~5,140 bytes per 1,000 tokens, so half the limit leaves ~4x for all of it.
    embed_text_byte_fraction: float = 0.5

    # R-013's token ceiling, retained as defence in depth and **no longer the
    # binding bound**. T027 showed the service does not truncate at the model's
    # 2,048-token limit at all — a marker in the final bytes of a 38,350-byte
    # record is still findable — so this guards a limit that does not bind. Kept
    # because a hosted service can change behaviour without notice, and one extra
    # comparison is cheaper than discovering that.
    embed_hard_token_limit: int = 2048
    embed_safety_factor: float = 0.75

    # -- retrieval budget: 12 -> 6 -> 3-6 -----------------------------------
    # Rerank top-N is 6, not 5, and the reason is arithmetic rather than
    # preference: evidence is selected from the reranked set, so a 5-wide
    # rerank cannot yield the 6 evidence units FR-020 asks for. The budget is
    # 12 candidates -> 6 reranked -> 3 to 6 selected. The validator below turns
    # a violation of that ordering into a startup error.
    retrieval_candidate_pool: int = 12
    retrieval_rerank_top_n: int = 6
    evidence_min_units: int = 3
    evidence_max_units: int = 6

    # Deliberately optional. `None` means "no floor configured", and the
    # evidence selector treats that as "do not refuse on score alone" rather
    # than defaulting it to 0 or to some plausible-looking number.
    min_rerank_score: float | None = None
    min_evidence_score: float | None = None

    # -- classification and filtering ---------------------------------------
    classification_confidence_threshold: float = 0.6
    filters_enabled: bool = True

    # -- generation ---------------------------------------------------------
    # A hard cap, not a loop bound. Regeneration stops at this many attempts
    # and the last answer is verified and reported as found (FR-011).
    generation_max_attempts: int = 2
    question_max_length: int = 2000

    # A cost control, not the FR-034 control, and the distinction is measured
    # rather than assumed. R-014 amendment: the service accepts only `low`,
    # `medium` and `high`. `"none"` — the value one would reach for to switch
    # the channel off — is rejected with HTTP 400 `invalid_request_error`, and
    # both configured models return a populated `message.reasoning` at every
    # effort that is accepted. So the parameter cannot deliver FR-034; the
    # adapter's discard of everything but `message.content` can, and the
    # post-condition in `provider.py` is what makes that verifiable. What is
    # left for the parameter is not emitting tokens that are thrown away, and
    # `"low"` is the cheapest way not to.
    #
    # `Literal` rather than `str` because the service is the authority on the
    # permitted set and this is the only place to state it. A typo here would
    # otherwise become a 400 on the first real query rather than a startup
    # error naming the variable.
    reasoning_effort: Literal["low", "medium", "high"] = "low"

    # -- API ----------------------------------------------------------------
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_general_per_minute: int = 60
    rate_limit_ask_per_minute: int = 20
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    query_log_retention_days: int = 30

    # -- evaluation gates (FR-065) -----------------------------------------
    # Configuration, not judgement. A run records the thresholds it was judged
    # against in evaluation_runs.thresholds, so two runs are never compared
    # across different gates. A threshold may never be derived from the run it
    # is judging, or it stops being a gate.
    eval_gate_recall_at_5: float = 0.80
    eval_gate_precision_at_5: float = 0.60
    eval_gate_mrr: float = 0.75
    eval_gate_cross_product_coverage: float = 0.80
    eval_gate_citation_validity: float = 0.98
    eval_gate_claim_coverage: float = 0.85
    eval_gate_faithfulness: float = 0.90
    eval_gate_unsupported_refusal_rate: float = 1.00
    eval_gate_p95_latency_ms: int = 8000

    # ---------------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------------

    @field_validator("allowed_domains", "cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: Any) -> Any:
        """Accept `A,B` as well as a JSON array.

        A comma-separated list is what an operator writes in a `.env` file. A
        JSON array is what a structured deployer writes. Both are legitimate;
        rejecting one of them pushes people toward hand-editing a compose file,
        which is how credentials end up in version control.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value  # a JSON string; pydantic-settings will parse it
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: list[str]) -> list[str]:
        """Reject `*` outright.

        A wildcard CORS origin combined with a credentialed origin is a
        configuration that appears benign and is not. The system has no
        cookie-based session, so there is no reason to allow one.
        """
        if "*" in value:
            raise ValueError(
                "cors_origins must not contain '*'. List the origins explicitly. "
                "This system has no cookie session, so there is no reason to allow "
                "any origin, and a wildcard is how a credentialed CORS "
                "misconfiguration starts."
            )
        return value

    @field_validator("allowed_domains")
    @classmethod
    def _normalise_domains(cls, value: list[str]) -> list[str]:
        """Lower-case, and reject a wildcard or a path.

        `*.atlassian.com` would silently widen the crawl far past the
        documentation, and `atlassian.com/path` is not a host. Both are refused
        at configuration time rather than producing confusing crawl behaviour
        later.
        """
        normalised: list[str] = []
        for domain in value:
            candidate = domain.strip().lower()
            if not candidate:
                continue
            if candidate.startswith("*"):
                raise ValueError(
                    f"allowed_domains must not contain a wildcard: {domain!r}. "
                    "List each host explicitly; a wildcard would widen the crawl "
                    "past the documentation and past the robots.txt check."
                )
            if "/" in candidate:
                raise ValueError(
                    f"allowed_domains entry must be a bare host, not a URL: {domain!r}"
                )
            normalised.append(candidate)
        if not normalised:
            raise ValueError("allowed_domains must not be empty; the crawler would be unbounded")
        return normalised

    @model_validator(mode="after")
    def _check_retrieval_budget(self) -> Settings:
        """The 12 -> 5 -> 3-6 budget must narrow at every stage.

        These are not independent settings. A pool of 4 with a rerank top-N of
        5 silently caps at 4, and a rerank top-N of 8 with an evidence maximum
        of 6 throws away rerank work that was paid for. Catching it at startup
        turns a silent quality regression into a startup error.
        """
        if self.retrieval_candidate_pool <= 0:
            raise ValueError("retrieval_candidate_pool must be positive")
        if not 1 <= self.retrieval_rerank_top_n <= self.retrieval_candidate_pool:
            raise ValueError(
                f"retrieval_rerank_top_n ({self.retrieval_rerank_top_n}) must be between "
                f"1 and retrieval_candidate_pool ({self.retrieval_candidate_pool}); "
                "the budget must narrow at the rerank stage, not widen"
            )
        if self.evidence_max_units > self.retrieval_rerank_top_n:
            raise ValueError(
                f"evidence_max_units ({self.evidence_max_units}) must not exceed "
                f"retrieval_rerank_top_n ({self.retrieval_rerank_top_n}); evidence is "
                "selected from the reranked set, so it cannot be larger than it"
            )
        if self.evidence_min_units > self.evidence_max_units:
            raise ValueError(
                f"evidence_min_units ({self.evidence_min_units}) must not exceed "
                f"evidence_max_units ({self.evidence_max_units})"
            )
        return self

    @model_validator(mode="after")
    def _check_chunk_sizing(self) -> Settings:
        """Chunk targets must fit the embedder with headroom.

        R-001's whole point is that a chunk which does not fit the embedder is
        silently truncated, and the tail then becomes evidence that was never
        encoded. Verifying the arithmetic here means a bad `EMBED_HARD_TOKEN_LIMIT`
        is a startup failure rather than a corpus-wide grounding defect that
        nobody notices until a citation looks well-supported and is not.
        """
        effective_ceiling = int(self.embed_hard_token_limit * self.embed_safety_factor)
        if self.chunk_target_max_tokens >= effective_ceiling:
            raise ValueError(
                f"chunk_target_max_tokens ({self.chunk_target_max_tokens}) must be below the "
                f"effective embedder ceiling ({self.embed_hard_token_limit} x "
                f"{self.embed_safety_factor} = {effective_ceiling}). A chunk at or above the "
                "ceiling is silently truncated by the embedder, and the truncated tail "
                "becomes evidence the model never saw (R-001)."
            )
        if self.chunk_target_min_tokens >= self.chunk_target_max_tokens:
            raise ValueError("chunk_target_min_tokens must be below chunk_target_max_tokens")
        if self.chunk_overlap_tokens >= self.chunk_target_min_tokens:
            raise ValueError(
                f"chunk_overlap_tokens ({self.chunk_overlap_tokens}) must be below "
                f"chunk_target_min_tokens ({self.chunk_target_min_tokens}); an overlap that "
                "large would duplicate most of every chunk and make the same passage win "
                "every selection"
            )
        if self.chunk_min_useful_tokens >= self.chunk_target_min_tokens:
            raise ValueError(
                "chunk_min_useful_tokens must be below chunk_target_min_tokens, or no chunk "
                "can ever clear the usefulness floor"
            )
        return self

    # ---------------------------------------------------------------------
    # Accessors
    # ---------------------------------------------------------------------

    def has_secret(self, name: str) -> bool:
        """Presence only. There is deliberately no companion `get_secret`.

        `SecretPresence` in the API carries a boolean and no value field at all
        (FR-042). This method is the only way a caller learns whether a key is
        configured, so there is no path from a settings object to a rendered key.
        """
        value = getattr(self, name, None)
        return isinstance(value, SecretStr) and bool(value.get_secret_value().strip())

    @property
    def effective_embed_ceiling_tokens(self) -> int:
        """The token count the chunker may never exceed, with headroom applied."""
        return int(self.embed_hard_token_limit * self.embed_safety_factor)

    @property
    def secret_fields(self) -> tuple[str, ...]:
        """Names of the required secrets, for startup diagnostics."""
        return ("groq_api_key", "pinecone_api_key")

    def evaluation_thresholds(self) -> dict[str, float]:
        """The gates a run will be judged against, keyed as the run records them.

        Returned as a plain mapping rather than read field-by-field at the
        evaluation call site, so a gate added to this class cannot be silently
        left out of the recorded thresholds. If it is in one, it is in both.
        """
        return {
            "recall_at_5": self.eval_gate_recall_at_5,
            "precision_at_5": self.eval_gate_precision_at_5,
            "mrr": self.eval_gate_mrr,
            "cross_product_coverage": self.eval_gate_cross_product_coverage,
            "citation_validity": self.eval_gate_citation_validity,
            "claim_coverage": self.eval_gate_claim_coverage,
            "faithfulness": self.eval_gate_faithfulness,
            "unsupported_refusal_rate": self.eval_gate_unsupported_refusal_rate,
            "p95_latency_ms": float(self.eval_gate_p95_latency_ms),
        }

    def public_config(self) -> dict[str, Any]:
        """A display-safe view. Secrets appear as booleans, never as values.

        The shape is the `GET /config` response schema in
        `contracts/openapi.yaml` minus `corpus`, which the route fills from
        counted rows. Key names follow the contract exactly (`evidence_min`,
        `evidence_max`) rather than the field names on this class
        (`evidence_min_units`) — the contract is the public interface and the
        class is an implementation detail, so the contract's names win.

        `reasoning_exposed` is present and hard-coded `False`. It is there so an
        operator can *verify* FR-034 against the running system rather than take
        the specification's word for it, and the contract marks it `const: false`
        so a true value is a contract violation rather than a surprise.

        `dimension_source` is the literal string `"service"`, for the same
        reason: the claim being made is that the vector dimension is read from
        service configuration and never hard-coded (Principle VII), and a
        static string in the response is what makes the claim checkable.

        The two score floors are omitted when unset. Reporting a number there
        would suggest a threshold exists, and these are meant to be tuned
        against a measured distribution rather than picked now.
        """
        retrieval: dict[str, Any] = {
            "candidate_pool": self.retrieval_candidate_pool,
            "rerank_top_n": self.retrieval_rerank_top_n,
            "evidence_min": self.evidence_min_units,
            "evidence_max": self.evidence_max_units,
            "filters_enabled": self.filters_enabled,
        }
        if self.min_rerank_score is not None:
            retrieval["min_rerank_score"] = self.min_rerank_score
        if self.min_evidence_score is not None:
            retrieval["min_evidence_score"] = self.min_evidence_score

        return {
            "retrieval": retrieval,
            "generation": {
                "provider": "groq",
                "model": self.groq_model,
                "classification_model": self.groq_classification_model,
                "max_attempts": self.generation_max_attempts,
                # const: false in the contract. See the docstring.
                "reasoning_exposed": False,
            },
            "index": {
                "name": self.pinecone_index_name,
                "namespace": self.pinecone_namespace,
                "embed_model": self.pinecone_embed_model,
                # const: 'service' in the contract.
                "dimension_source": "service",
                "rerank_model": self.pinecone_rerank_model,
            },
            "secrets": {
                "pinecone_api_key": SecretPresence(
                    configured=self.has_secret("pinecone_api_key")
                ).model_dump(),
                "groq_api_key": SecretPresence(
                    configured=self.has_secret("groq_api_key")
                ).model_dump(),
                # The contract lists `database_url` alongside the two keys. Its
                # presence is reported; the URL itself is not, and a
                # password-bearing connection string is exactly the kind of
                # value that ends up in a support ticket.
                "database_url": SecretPresence(configured=bool(self.database_url)).model_dump(),
            },
        }

    def public_domains(self) -> list[str]:
        """The allowlisted crawl hosts, for the `corpus` block of `/config`."""
        return list(self.allowed_domains)


class SecretPresence:
    """Whether a secret is configured. Carries no value and no method to get one.

    A plain dataclass rather than a Pydantic model, because the *shape* is the
    guarantee: the API response is built from `configured` and there is no
    attribute to leak. Adding a `value` field here would make FR-042 a matter
    of discipline again.
    """

    __slots__ = ("configured",)

    def __init__(self, *, configured: bool) -> None:
        self.configured = configured

    def model_dump(self) -> dict[str, bool]:
        """Exactly the schema in contracts/openapi.yaml: one boolean, no more."""
        return {"configured": self.configured}

    def __repr__(self) -> str:
        return f"SecretPresence(configured={self.configured})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretPresence):
            return NotImplemented
        return self.configured == other.configured

    def __hash__(self) -> int:
        return hash(self.configured)


# ============================================================================
# Access
# ============================================================================


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The single settings instance.

    Cached, so the running application and the test suite observe the same
    values, and a re-read part-way through a request cannot disagree with what
    was validated at startup. A `ValidationError` here is a startup failure that
    names the offending variable, which is the intended behaviour: the
    alternative is a process running with a default nobody chose.
    """
    return Settings()


def reset_settings() -> None:
    """Clear the cached settings. For tests that change the environment."""
    get_settings.cache_clear()


__all__ = [
    "PageType",
    "ProductDomain",
    "QuestionIntent",
    "SecretPresence",
    "Settings",
    "VerificationStatus",
    "get_settings",
    "reset_settings",
]
