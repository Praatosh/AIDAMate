"""Typed application configuration.

Every setting AIDA-MATE will need across all 15 phases is declared here up front,
so later phases consume configuration rather than repeatedly reopening this
module. Secrets are read from the environment only — never hardcoded, never
defaulted to a real value.
"""

import shutil
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelProvider(StrEnum):
    """Supported LLM providers. Exactly one is active per deployment."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class SandboxMode(StrEnum):
    """Which `ISandboxFactory` implementation backs the agent-enrichment stage."""

    DOCKER = "docker"
    LOCAL = "local"


class Settings(BaseSettings):
    """Validated settings loaded from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    app_env: Literal["local", "dev", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    public_base_url: str = Field(
        default="http://localhost:8000",
        description=(
            "This service's own externally-reachable base URL, used to build the "
            "merge-confirmation link posted to Linear for MEDIUM/HIGH risk (see "
            "AUTO_MERGE_ON_DONE_ENABLED below). Must be the ngrok tunnel (or real deployment "
            "URL) in any environment where a human needs to click the link from Linear's UI — "
            "localhost is only correct if Linear itself is being opened on this same machine."
        ),
    )

    management_api_key: str | None = Field(
        default=None,
        description=(
            "Shared secret required (as an `X-Api-Key` header) to call AIDA-MATE's own JSON "
            "management API — `/reviews*` and `/scheduled-prompts*`. Neither router has any other "
            "authentication of its own: unlike the two webhooks (HMAC-signed) and the three "
            "human-facing HTML pages (gated on an unguessable bearer token in their own URL), these "
            "plain CRUD endpoints had nothing at all — security-audit finding. Fail-closed by "
            "default, matching this app's own GITHUB_WEBHOOK_SECRET/GITHUB_REPO_ALLOWLIST pattern: "
            "unset means every request to a protected route is rejected, not silently allowed "
            "through. Set a real value before deploying anywhere reachable by anyone but you."
        ),
    )

    # --- Linear ------------------------------------------------------------
    linear_client_id: str | None = None
    linear_client_secret: str | None = None
    linear_webhook_secret: str = Field(
        description="Shared secret used to verify the HMAC signature on Linear webhooks."
    )
    linear_redirect_uri: str = "http://localhost:8000/auth/linear/callback"
    linear_oauth_scopes: str = Field(
        default="read,write,app:assignable,app:mentionable",
        description=(
            "Comma-separated OAuth scopes. `app:assignable` is what lets AIDA-MATE be delegated "
            "issues as an agent; `app:mentionable` lets it respond to @-mentions."
        ),
    )
    aida_mate_linear_actor_id: str | None = Field(
        default=None,
        description=(
            "Optional override for AIDA-MATE's own Linear actor ID. Normally discovered "
            "automatically at install time via `viewer { id }` and stored on the installation."
        ),
    )
    linear_webhook_max_age_seconds: int = Field(
        default=60,
        ge=1,
        description="Reject webhooks whose webhookTimestamp is older than this, to blunt replays.",
    )
    linear_dev_api_key: str | None = Field(
        default=None,
        description="Development-only personal API key used instead of OAuth. Unset in production.",
    )
    linear_token_store_path: str | None = Field(
        default=None,
        description=(
            "Local development convenience: persist OAuth installations to this JSON file so a "
            "server restart doesn't force every workspace to reconnect. Unset (default): pure "
            "in-memory, matching production, where a real encrypted database store is the intended "
            "path. NOT encrypted at rest — keep this file out of version control, same as .env, "
            "and never point it at a shared or multi-user location."
        ),
    )
    linear_auto_review_enabled: bool = Field(
        default=False,
        description=(
            "Review automatically whenever a ticket with a linked PR is created or updated, in "
            "addition to explicit delegation. Off by default: ticket edits are frequent, and each "
            "review costs a sandbox and LLM tokens. Content-level dedup prevents re-reviewing an "
            "unchanged PR, so the real cost is one PR lookup per ticket event."
        ),
    )
    auto_merge_on_done_enabled: bool = Field(
        default=False,
        description=(
            "Merge a pull request automatically when its Linear issue moves into a "
            "completed-type workflow state, if AIDA-MATE already holds a COMPLETED review for "
            "it (see CLAUDE.md §1a). LOW risk merges immediately with no confirmation; "
            "MEDIUM/HIGH require an explicit human 'Yes, merge' via the confirmation page "
            "linked from a Linear comment — never merged silently. Off by default: this is the "
            "only capability in AIDA-MATE that writes a merge to a repository."
        ),
    )

    # --- GitHub ------------------------------------------------------------
    github_app_id: str | None = None
    github_installation_id: str | None = None
    github_private_key: str | None = Field(
        default=None,
        description="GitHub App PEM private key (literal with escaped newlines, or base64).",
    )
    github_dev_token: str | None = Field(
        default=None,
        description="Development-only PAT used instead of the GitHub App. Unset in production.",
    )
    github_api_url: str = Field(
        default="https://api.github.com",
        description="GitHub REST base URL. Override for GitHub Enterprise Server.",
    )
    github_repo_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated `owner/repo` entries searched when a Linear issue has no attached "
            "PR and AIDA-MATE must fall back to branch-name or title matching. Empty disables "
            "those fallbacks: searching every accessible repository would be slow and would "
            "invite false matches. Also used to allowlist which repos' merges "
            "GITHUB_MERGE_SYNC_ENABLED (below) is allowed to act on."
        ),
    )
    github_webhook_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret for verifying `X-Hub-Signature-256` on inbound GitHub webhooks (see "
            "CLAUDE.md §1b). Required to receive any GitHub webhook traffic at all — unset means "
            "every delivery is rejected as unsigned, fail-closed rather than fail-open."
        ),
    )
    github_merge_sync_enabled: bool = Field(
        default=False,
        description=(
            "Move a Linear issue to a completed-type workflow state when its linked GitHub PR is "
            "merged into github_merge_sync_branch, if AIDA-MATE already holds a COMPLETED review "
            "linking that PR to the issue (see CLAUDE.md §1b). The reverse direction of "
            "AUTO_MERGE_ON_DONE_ENABLED. Off by default: this is the only capability in AIDA-MATE "
            "that writes a Linear issue's status automatically from GitHub activity."
        ),
    )
    github_merge_sync_branch: str = Field(
        default="main",
        description=(
            "Only a PR merged into this base branch counts as 'done' for github_merge_sync_enabled. "
            "A merge into any other branch (e.g. a staging/integration branch) is ignored."
        ),
    )
    github_issue_sync_enabled: bool = Field(
        default=False,
        description=(
            "Create/update a Linear issue for GitHub Issues and security alerts (Code Scanning, "
            "Dependabot, Secret Scanning) on the linear_sync_team_key team (see CLAUDE.md §1c). "
            "Off by default. Pull requests by themselves, CI/Actions runs, and checks are never "
            "synced regardless of this flag — only the four sources §1c documents."
        ),
    )
    linear_sync_team_key: str | None = Field(
        default=None,
        description=(
            "Linear team key (e.g. 'GIT', as shown in Linear's UI/URLs) that github_issue_sync_enabled "
            "creates issues under. Required for that flag to do anything; resolved to a team id once "
            "per sync via a query, not cached — matching find_done_state_id's own reasoning."
        ),
    )
    scheduled_prompts_enabled: bool = Field(
        default=False,
        description=(
            "Run the scheduled-prompt timer loop (see CLAUDE.md §1d) — daily, timezone-aware "
            "prompts run against a repo snapshot and posted as a Linear comment. Off by default. "
            "Structurally inert without GitHub credentials and a sandbox backend configured, same "
            "as the review pipeline's own agent-enrichment stage."
        ),
    )

    # --- Sandbox (Docker Sandboxes, the standalone `sbx` CLI) ----------------
    sandbox_mode: SandboxMode = Field(
        default=SandboxMode.DOCKER,
        description=(
            "`docker` (default): isolate PR content in a real Docker Sandbox VM. `local`: run the "
            "same read-only inspection (list/read/grep, never execute) directly on a host-side "
            "scratch temp directory instead — NOT an isolation boundary, a stopgap for hosts where "
            "Docker Sandboxes cannot run at all. Use only when you understand that trade-off."
        ),
    )
    sandbox_binary: str = Field(
        default="sbx",
        description=(
            "Executable used for sandbox operations, invoked directly as `<binary> <subcommand>` "
            "(e.g. `sbx create shell ...`) — `sbx` is Docker's own standalone CLI for its current "
            "\"Docker Sandboxes\" product, not a `docker <subcommand>` plugin (the older `docker "
            "sandbox` plugin this used to invoke has been deprecated and removed by Docker). "
            "Requires Docker Desktop running, plus a one-time interactive `sbx login` this app "
            "cannot perform on its own."
        ),
    )
    sandbox_timeout_seconds: float = Field(default=900, ge=30, le=3600)
    agent_timeout_seconds: float = Field(
        default=300,
        ge=10,
        le=1800,
        description=(
            "Wall-clock budget for the entire multi-agent analyze() run — Context, all "
            "specialists, and the Judge combined. Independent of sandbox_timeout_seconds, which "
            "bounds individual sandboxed commands, and of specialist_timeout_seconds, which "
            "bounds each inner agent call."
        ),
    )
    specialist_timeout_seconds: float = Field(
        default=60,
        ge=10,
        le=600,
        description=(
            "Per-agent wall-clock budget inside one multi-agent analyze() run (Context, each "
            "specialist). Without this, one hung specialist would stall the whole concurrent "
            "batch until the outer agent_timeout_seconds fired and failed the entire review — "
            "this is what lets a single slow specialist be recorded as failed instead."
        ),
    )

    # --- Review execution --------------------------------------------------
    review_concurrency: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Reviews processed in parallel. Bounds sandbox and LLM spend under bursts.",
    )
    review_queue_maxsize: int = Field(
        default=100,
        ge=1,
        description="Queued reviews before new ones are rejected with visible back-pressure.",
    )

    # --- Model provider ----------------------------------------------------
    model_provider: ModelProvider = ModelProvider.OPENAI
    openai_api_key: str | None = None
    openai_base_url: str | None = Field(
        default=None,
        description=(
            "Override for the OpenAI-compatible endpoint the review agent talks to — e.g. an "
            "organization's own gateway/proxy in front of the OpenAI API. Leave unset to use "
            "OpenAI's default endpoint. When set, tracing upload is disabled (it always targets "
            "OpenAI's own endpoint regardless of this override, which a gateway-scoped key "
            "typically cannot authenticate against)."
        ),
    )
    openai_api_version: str | None = Field(
        default=None,
        description=(
            "Set alongside OPENAI_BASE_URL only for an Azure OpenAI / Azure AI Foundry endpoint "
            "(recognizable by its 'api-version' query parameter requirement). Selects the Azure "
            "client instead of the plain OpenAI one. Get the value from whoever provisioned the "
            "Azure resource/project — it is not discoverable from this app."
        ),
    )
    anthropic_api_key: str | None = None
    review_model: str = Field(
        default="gpt-5.6-sol",
        description="Model used by the four tool-using specialists and the Judge (quality-critical).",
    )
    utility_model: str = Field(
        default="gpt-5.6-luna",
        description="Cost-optimized model for the toolless Context Agent (orientation, not investigation).",
    )
    model_reasoning_effort: str | None = Field(
        default=None,
        pattern="^(low|medium|high)$",
        description=(
            "Optional reasoning effort for supported models. Lower effort reduces latency and cost; "
            "leave unset to use the model/provider default."
        ),
    )

    # --- Persistence -------------------------------------------------------
    review_store_path: str | None = Field(
        default=None,
        description=(
            "SQLite file for review jobs. Set it and review state survives restarts, "
            "interrupted reviews become recoverable, and dedup is enforced by a UNIQUE index "
            "rather than a racy read-then-write. Unset (default): in-memory, losing all review "
            "state on restart. Holds no secrets, but is not encrypted — keep it out of version "
            "control alongside .env."
        ),
    )
    database_url: str | None = Field(
        default=None,
        description="Reserved for a future server-backed database; unused today.",
    )

    # --- Risk policy (configurable, not hardcoded business rules) ----------
    risk_low_max_score: int = Field(default=20, ge=0)
    risk_medium_max_score: int = Field(default=60, ge=1)
    medium_requires_human_review: bool = False

    # --- Validation --------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_like_values(cls, data: object) -> object:
        """Accept mixed-case LOG_LEVEL / MODEL_PROVIDER / APP_ENV from the environment."""
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for key, transform in (
            ("log_level", str.upper),
            ("model_provider", str.lower),
            ("app_env", str.lower),
            ("sandbox_mode", str.lower),
        ):
            for candidate in (key, key.upper()):
                value = normalized.get(candidate)
                if isinstance(value, str):
                    normalized[candidate] = transform(value)
        return normalized

    @model_validator(mode="after")
    def _require_key_for_selected_provider(self) -> "Settings":
        """Require only the API key of the provider actually in use.

        Requiring both keys would force operators to hold credentials they do
        not need; requiring neither would defer a fatal misconfiguration to the
        first review, long after startup.
        """
        required = {
            ModelProvider.OPENAI: ("openai_api_key", "OPENAI_API_KEY"),
            ModelProvider.ANTHROPIC: ("anthropic_api_key", "ANTHROPIC_API_KEY"),
        }[self.model_provider]

        if not getattr(self, required[0]):
            raise ValueError(f"MODEL_PROVIDER={self.model_provider.value} requires {required[1]} to be set")
        return self

    @model_validator(mode="after")
    def _openai_api_version_requires_base_url(self) -> "Settings":
        """`OPENAI_API_VERSION` selects the Azure client and is meaningless without
        `OPENAI_BASE_URL` (both `OpenAIAgentRunner`/`ScheduledPromptRunner` construct
        `AsyncAzureOpenAI(base_url=..., api_version=...)` together — see
        `openai_api_version`'s own description). Without this check, setting only
        `OPENAI_API_VERSION` would defer a fatal misconfiguration to the SDK's own
        opaque error on the first review, rather than failing fast at startup the
        way every other config precondition here does.
        """
        if self.openai_api_version and not self.openai_base_url:
            raise ValueError("OPENAI_API_VERSION requires OPENAI_BASE_URL to also be set")
        return self

    @model_validator(mode="after")
    def _validate_risk_thresholds(self) -> "Settings":
        """Thresholds must be ordered, or the LOW/MEDIUM/HIGH buckets are ill-defined."""
        if self.risk_medium_max_score <= self.risk_low_max_score:
            raise ValueError(
                "RISK_MEDIUM_MAX_SCORE must be greater than RISK_LOW_MAX_SCORE "
                f"(got {self.risk_medium_max_score} <= {self.risk_low_max_score})"
            )
        return self

    # --- Derived helpers ---------------------------------------------------

    @property
    def sandbox_configured(self) -> bool:
        """Whether a sandbox backend is available for the optional agent-enrichment stage.

        In `local` mode this is unconditionally True: it needs no external
        binary, only the Python standard library. False means the stage is
        skipped, not that reviews fail closed — the deterministic
        area/risk/label pipeline works without a sandbox and never depended
        on one.
        """
        if self.sandbox_mode is SandboxMode.LOCAL:
            return True
        return shutil.which(self.sandbox_binary) is not None

    @property
    def github_app_configured(self) -> bool:
        """Whether GitHub App credentials are complete."""
        return all((self.github_app_id, self.github_installation_id, self.github_private_key))

    @property
    def linear_oauth_configured(self) -> bool:
        """Whether Linear OAuth credentials are complete."""
        return all((self.linear_client_id, self.linear_client_secret))

    @property
    def linear_scope_list(self) -> list[str]:
        """OAuth scopes as a list, from the comma-separated setting."""
        return [scope.strip() for scope in self.linear_oauth_scopes.split(",") if scope.strip()]

    @property
    def github_repos(self) -> list[str]:
        """Allowlisted `owner/repo` entries, normalized and de-duplicated."""
        seen: dict[str, None] = {}
        for entry in self.github_repo_allowlist.split(","):
            cleaned = entry.strip().strip("/")
            if cleaned.count("/") == 1 and all(cleaned.split("/")):
                seen.setdefault(cleaned.lower(), None)
        return list(seen)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance.

    Tests that mutate the environment should call `get_settings.cache_clear()`
    first so the change is observed.
    """
    return Settings()
