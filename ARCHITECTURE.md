# AIDA-MATE — Architecture

**AI-powered Pull Request Risk Analysis Agent.**
Triggered by assigning a Linear issue to AIDA-MATE; analyzes the linked GitHub PR (optionally inside a Docker Sandbox, with an AI agent contributing findings); classifies risk deterministically; writes labels and summaries back to GitHub and Linear.

> **AIDA-MATE does not write code.** No code generation, no commits, no branches, no PR creation, no merging. Analysis and classification only.

---

## 1. Architecture overview

Responsibilities, deliberately not blurred:

| Layer | Owns | Never does |
|---|---|---|
| **FastAPI** | HTTP intake, webhook validation, job lifecycle | Repository execution, risk policy |
| **Review Agent (6 LLM agents)** | Context orientation, then four concurrent specialists (Security/Code/Architecture/Testing), then a Judge that reconciles into one `ReviewAnalysis` | Deciding final risk; mutating GitHub/Linear |
| **Tools** | Discrete sandboxed actions (`read_file`, `search_code`, `list_files`) — never given to the Judge | Reasoning, policy |
| **Docker Sandbox** | Isolated execution over untrusted repo content | Holding write credentials |
| **Risk Engine** | Deterministic policy: areas/findings → score → LOW/MEDIUM/HIGH | Any LLM involvement |

### The central invariant

```
AI Agent  →  structured findings  →  Python Risk Engine  →  final risk
             (judgment)              (deterministic policy)
```

The LLM is **never** asked for the final risk level, the label set, or the human-review flag. Those come from `core/risk_engine.py` and `core/label_engine.py` — pure Python, table-driven, unit-testable without mocks. The Review Agent's output schema (`ReviewAnalysis`) does not even contain a `risk` field, so it cannot supply one; this is a schema-level guarantee rather than a post-hoc overwrite that could be forgotten.

A second, less obvious guarantee: only a finding's `category` — never the agent's own top-level `ReviewAnalysis.areas` claim — feeds the score. Letting an unsubstantiated area claim move the score independently of an evidenced finding would be the same "LLM controls risk" back door in a different shape. `ReviewResult.areas`/labels are derived from whichever areas actually scored (`{c.area for c in assessment.breakdown}`), not from what the model merely asserted.

### Trust model

Repository contents, PR titles, descriptions, commit messages, and READMEs are **untrusted input**, treated strictly as data. A prompt-injection payload in a PR must not be able to talk AIDA-MATE into a low-risk verdict or an approving comment. The system prompt (`prompts/review_prompt.py`) states this explicitly and instructs the agent to report an injection attempt itself as a `security` finding. Two structural defenses back this up:

1. **Credential isolation** — the sandbox holds no GitHub token and no Linear key. It receives inert PR bytes only (a downloaded tarball, extracted inside the sandbox). All writes happen from the trusted host *after* the sandbox is destroyed.
2. **Deterministic policy** — even a fully compromised agent can only emit findings; it cannot set the risk level, the labels, or `needs_human_review`.

---

## 2. Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    participant Dev as Developer
    participant Lin as Linear
    participant API as FastAPI
    participant Job as Review Worker
    participant Orc as Orchestrator
    participant Box as Docker Sandbox
    participant Agent as Review Agent (6 LLM agents)
    participant GH as GitHub

    Dev->>Lin: Delegate / assign issue to AIDA-MATE
    Lin->>API: POST /webhooks/linear (AgentSessionEvent, signed)
    API->>API: Verify HMAC over raw body
    API->>API: Reject stale delivery (replay protection)
    API->>API: Filter: is this a review trigger?
    API->>Job: Enqueue ReviewJob (QUEUED), intake-dedup'd
    API-->>Lin: 202 Accepted (fast ack)

    Note over Job: Everything below is asynchronous

    Job->>Lin: Emit `thought` activity (within Linear's 10s window)
    Job->>Orc: execute(job)

    Orc->>Lin: Fetch issue + attachments (FETCHING_PR)
    Orc->>Orc: Resolve linked GitHub PR
    alt No PR found, or PR closed/empty
        Orc-->>Job: raises (LinkedPullRequestNotFoundError / InvalidPullRequestError)
        Job->>Lin: Emit `error` activity
        Job-->>Job: FAILED (no sandbox ever created)
    end
    Orc->>GH: Fetch PR, files, diff, commits

    Orc->>Orc: detect_areas() — deterministic, path-based (ANALYZING)
    opt Sandbox + agent configured
        Orc->>GH: Download archive at exact head SHA
        Orc->>Box: Create sandbox, upload archive, extract
        Orc->>Agent: analyze(pull_request, sandbox)
        Agent->>Box: read_file / search_code / list_files
        Box-->>Agent: bounded output
        Agent-->>Orc: ReviewAnalysis (findings, areas — advisory only)
        Orc->>Box: Destroy sandbox (finally, unconditional)
    end

    Orc->>Orc: assess_risk(areas ∪ finding.category) (CLASSIFYING)
    Orc->>Orc: build_labels() from what actually scored

    Orc->>Job: Persist ReviewResult (before any publish)
    Orc->>GH: Labels + summary comment (UPDATING_GITHUB)
    Orc->>Lin: Result activity/comment (UPDATING_LINEAR)
    Job-->>Job: COMPLETED
```

Ordering decisions worth noting:

- **The sandbox is destroyed before any write happens.** Writes use credentials the sandbox never saw.
- **The result is persisted before external updates.** A GitHub outage must not destroy a completed analysis; the update is retryable against the stored result.
- **A sandbox/agent failure fails the whole review once configured**, rather than silently degrading to areas-only scoring — an operator who enabled the capability should be told when it breaks.

---

## 2a. Inside `analyze()`: the multi-agent pipeline

From the orchestrator's point of view, nothing above changed: it still calls `Agent.analyze(pull_request, sandbox, review_id=...)` once and gets back one `AgentRunOutcome` wrapping one `ReviewAnalysis`. What that call does internally is itself a small, fixed-order pipeline — plain Python (`await`, `asyncio.gather`, `try`/`except`), never an SDK handoff or a second LLM controlling sequencing, for the same reason `orchestrator.py` itself is plain Python (§6).

```mermaid
sequenceDiagram
    autonumber
    participant Orc as Orchestrator
    participant Ctx as Context Agent
    participant Sec as Security Agent
    participant Code as Code Agent
    participant Arch as Architecture Agent
    participant Test as Testing Agent
    participant Judge as Judge Agent
    participant Box as Sandbox

    Orc->>Ctx: run(PR facts) — no tools
    alt Context succeeds
        Ctx-->>Orc: PRContextAnalysis (summary, intent, important_files)
    else Context fails or times out
        Ctx-->>Orc: recorded as failed_specialists += "context" (non-fatal)
    end

    par Security
        Orc->>Sec: run(PR facts + context hint)
        Sec->>Box: read_file / search_code / list_files
        Sec-->>Orc: ReviewAnalysis, or recorded failure
    and Code
        Orc->>Code: run(PR facts + context hint)
        Code->>Box: read_file / search_code / list_files
        Code-->>Orc: ReviewAnalysis, or recorded failure
    and Architecture
        Orc->>Arch: run(PR facts + context hint)
        Arch->>Box: read_file / search_code / list_files
        Arch-->>Orc: ReviewAnalysis, or recorded failure
    and Testing
        Orc->>Test: run(PR facts + context hint)
        Test->>Box: read_file / search_code / list_files
        Test-->>Orc: ReviewAnalysis, or recorded failure
    end

    alt All 4 specialists failed
        Orc-->>Orc: raise AgentError — Judge is never called
    else At least 1 specialist survived
        Orc->>Judge: run(context hint + survivors' ReviewAnalysis JSON + failed names) — no tools
        Judge-->>Orc: ReviewAnalysis (reconciled findings, one summary)
    end

    Orc-->>Orc: AgentRunOutcome(analysis=Judge's output, failed_specialists=[...])
```

Decisions worth calling out explicitly:

- **Testing runs concurrently with Security/Code/Architecture**, not after them as a naive reading of "Context → specialists → Judge" might suggest. It has no data dependency on the other three's findings — only the PR facts and the same context hint they all get — so there is no reason to serialize it. See §6a.
- **Context's failure is absorbed, not propagated.** Its output is a cost-control hint ("here's where to look first"), never a restriction on what a specialist may read — a specialist that never received a hint just investigates from the raw PR facts instead, with no reduction in tool access.
- **The Judge is the only path to `assess_risk()`.** Whatever it produces — even a sloppy reconciliation of the survivors — is the one `ReviewAnalysis` the orchestrator ever sees. See §1's central invariant: this is exactly as true with 4 specialists feeding 1 Judge as it was with 1 agent feeding the risk engine directly.

---

## 3. Component responsibilities

### `app/api/` — HTTP boundary
| Module | Responsibility |
|---|---|
| `health.py` | Liveness/readiness (`sandbox`, `github`, `linear` capability flags) |
| `linear_webhook.py` | Verify signature over **raw body**, reject replays, filter events, dedupe, enqueue, return 2xx fast |
| `linear_auth.py` | OAuth install/callback/status — isolated from all agent logic |

The webhook handler never performs a review inline. It validates and enqueues.

### The Linear agent trigger

Linear's first-class agent mechanism is the **`AgentSessionEvent`** webhook, fired with `action: "created"` when an app holding the `app:assignable` scope is delegated an issue or @-mentioned. This is the primary trigger. A plain `Issue`/`update` assignee change to AIDA-MATE's app user is supported as a secondary path, and an opt-in "auto" mode (any create/update on an issue with a linked PR) as a third; all three normalize to a single `ReviewTrigger`.

Two consequences that shape the design:

- **A 10-second clock.** After a `created` event, AIDA-MATE must emit an activity or be marked unresponsive. This is why the webhook handler must stay non-blocking and why acknowledgement precedes the actual review.
- **`webhookTimestamp` must be checked.** A captured delivery keeps a valid signature indefinitely; only an age check stops it being replayed.

Reference: [Linear agent interaction](https://linear.app/developers/agent-interaction)

### `app/workers/` — execution

`ReviewQueue` is a bounded in-process queue with a fixed worker pool, not FastAPI `BackgroundTasks`. Two reasons: a review takes minutes, so its lifetime must not be tied to the HTTP request that triggered it; and unbounded concurrency would let a webhook burst exhaust sandbox quota and LLM budget at once. A bounded queue turns overload into visible back-pressure instead of silent memory growth.

`ReviewWorker` guarantees three things: the Linear session is acknowledged **before** any slow work, the job always reaches a terminal state, and `run()` never raises — an escaped exception would leave the requester waiting on a review that is not coming.

`IReviewExecutor` is the seam between that machinery and the analysis pipeline. `ReviewOrchestrator` is the real implementation when GitHub credentials are configured; `PendingPipelineExecutor` (always raises `ReviewPipelineIncompleteError`) is the fallback when they aren't — so no job can appear to have succeeded without ever having fetched a PR.

### `app/services/` — orchestration
| Module | Responsibility |
|---|---|
| `review_service.py` | Intake: turns a `ReviewTrigger` into a persisted `ReviewJob` and enqueues it (two-stage dedup) |
| `linear_service.py` | Linear GraphQL: issue fetch, attachments, comments, agent-session activities |
| `github_service.py` | GitHub REST: PR, files, diff, commits, archive download, labels, comments |
| `sandbox_service.py` | Docker Sandbox lifecycle via the `docker sandbox` CLI: create, upload, exec, destroy — **known gap**: Docker has deprecated and removed this CLI plugin (confirmed live, see CLAUDE.md §6); `SANDBOX_MODE=local` (`local_sandbox_service.py`) is the working default until a replacement is built |
| `linear_auth_service.py` | OAuth (PKCE) token exchange, refresh, revoke |
| `pr_resolver.py` | Finds the linked PR: attachment → branch name → title/body, in that order |

### `app/agents/` — reasoning and sequencing
| Module | LLM? | Why |
|---|---|---|
| `orchestrator.py` | **No** | Fixed-order pipeline with no branching — see §6. The full analysis sequence lives here |
| `review_agent.py` | Yes | The only file importing the OpenAI Agents SDK. Builds and runs 6 `Agent` objects: Context, four specialists (Security/Code/Architecture/Testing), and a Judge |

GitHub access is still a plain typed service (§6 explains why), and risk/label decisions are still plain Python — neither of those became an agent. What changed is the single reasoning step itself: it is now internally a manager/specialist pipeline (§6a), not one `ReviewAgent` covering every concern through one system prompt. Only the Context, Security, Code, Architecture, and Testing agents hold the three read-only sandbox tools; the Judge holds none — it reconciles the specialists' already-evidenced output rather than re-investigating.

### `app/tools/` and `app/prompts/`
| Module | Responsibility |
|---|---|
| `tools/sandbox_tools.py` | `list_files`/`read_file`/`search_code` — thin, shell-escaped wrappers over `ISandbox.exec()` |
| `prompts/review_prompt.py` | The review agent's system prompt, including the prompt-injection defense clause |

### `app/core/` — policy and cross-cutting
| Module | Responsibility |
|---|---|
| `area_detector.py` | Regex path-matching → `Area` set. Pure, deterministic, no LLM |
| `risk_engine.py` | Areas (+ finding categories) → score → LOW/MEDIUM/HIGH. Pure, configurable |
| `label_engine.py` | Risk + areas → label set (`risk:`, `review:`, `area:`, `owasp:`) |
| `report.py` | Renders the GitHub/Linear comment text. Pure formatting, no I/O |
| `config.py` | Typed settings, fail-fast validation |
| `logging.py` | Structured JSON logging, secret redaction |
| `interfaces.py` | Protocol definitions (the DIP boundary) |
| `errors.py` | Domain exception hierarchy |
| `events.py` | Lifecycle event name constants |

---

## 4. Project structure

```
gitmate/
├── pyproject.toml
├── .env.example
├── README.md
├── ARCHITECTURE.md
├── app/
│   ├── main.py                     FastAPI app + composition root
│   ├── api/
│   │   ├── health.py
│   │   ├── linear_webhook.py
│   │   └── linear_auth.py
│   ├── agents/
│   │   ├── orchestrator.py         deterministic pipeline; optional sandbox+agent stage
│   │   └── review_agent.py         the only file importing the OpenAI Agents SDK
│   ├── tools/
│   │   └── sandbox_tools.py        list_files / read_file / search_code
│   ├── prompts/
│   │   └── review_prompt.py
│   ├── services/
│   │   ├── review_service.py       intake
│   │   ├── linear_service.py
│   │   ├── linear_auth_service.py
│   │   ├── github_service.py
│   │   ├── sandbox_service.py      docker sandbox CLI adapter
│   │   ├── pr_resolver.py
│   │   ├── job_repository.py
│   │   └── token_store.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   ├── interfaces.py
│   │   ├── errors.py
│   │   ├── events.py
│   │   ├── area_detector.py
│   │   ├── risk_engine.py
│   │   ├── label_engine.py
│   │   └── report.py
│   ├── models/
│   │   ├── common.py               shared taxonomy
│   │   ├── review.py
│   │   ├── github.py
│   │   └── linear.py
│   └── workers/
│       └── review_worker.py
└── tests/
    ├── unit/
    └── integration/
```

**One deviation from the spec's structure listing:** `app/core/interfaces.py` was added. The spec's structure omits it, but the first-task list requires interfaces for the sandbox, GitHub, and the AI agent. `core/` is the dependency-free centre, so it owns the contracts that outer layers implement.

---

## 5. Data models

### Shared taxonomy (`models/common.py`)

`RiskLevel` = LOW · MEDIUM · HIGH
`Severity` = INFO · LOW · MEDIUM · HIGH · CRITICAL
`Area` = authentication · authorization · security · backend · frontend · api · database · migrations · infrastructure · ci_cd · payments · business_logic · configuration · dependencies · testing · documentation

**Design note — one taxonomy, not two.** Finding *category* and PR *area* use the same `Area` enum rather than two parallel near-identical vocabularies that would inevitably drift.

**Design note — risk and severity are different scales, deliberately.** A PR can be MEDIUM risk overall while containing one CRITICAL-severity finding. Collapsing them would lose information.

### Agent output (`models/review.py`)

```python
class Finding(BaseModel):
    category: Area
    severity: Severity
    description: str
    reason: str = ""
    file: str | None = None
    line: int | None = None
    recommendation: str | None = None

class ReviewAnalysis(BaseModel):      # ← what a specialist, and the Judge, produce
    summary: str
    findings: list[Finding]
    areas: list[Area]                 # advisory only — see §1
    security_impact: bool
    owasp_relevant: bool

class PRContextAnalysis(BaseModel):   # ← what the Context Agent produces — orientation, not judgment
    summary: str
    intent: str
    important_files: list[str]
    affected_components: list[str]

class AgentRunOutcome(BaseModel):     # ← what analyze() returns to the orchestrator
    analysis: ReviewAnalysis          # the Judge's reconciled output
    model: str
    tool_calls_count: int
    failed_specialists: list[str]     # e.g. ["security"] — empty means every stage completed
```

Note what is **absent** from `ReviewAnalysis` and `PRContextAnalysis`: no `risk`, no `risk_score`, no `labels`, no `needs_human_review` — the same schema-level guarantee applies to every agent in the pipeline, not just one.

### Final result (`models/review.py`)

```python
class ReviewResult(BaseModel):
    risk: RiskLevel               # ← Risk Engine
    risk_score: int               # ← Risk Engine
    needs_human_review: bool      # ← Risk Engine
    labels: list[str]             # ← Label Engine
    areas: list[Area]             # ← whatever actually scored (breakdown), not analysis.areas
    security_impact: bool         # ← deterministic OWASP-category membership
    owasp_relevant: bool          # ← same
    summary: str                  # ← Agent
    findings: list[Finding]       # ← Agent
    breakdown: list[ScoreContribution]  # ← audit trail: which rule contributed how many points
    ai_analysis_ran: bool         # ← True only if the agent actually executed — never asserted
    ai_model: str | None          # ← which model produced the findings, when ai_analysis_ran
    tool_calls_count: int         # ← counted by our own code as calls happened, never self-reported
    sandbox_mode: str | None      # ← 'docker' or 'local', when ai_analysis_ran
    failed_specialists: list[str]  # ← non-empty means partial coverage; forces needs_human_review
```

`ai_analysis_ran` gates both `ai_model` and `sandbox_mode` — neither means anything when the deterministic-only path ran, and published text (`core/report.py`) checks it before ever writing "AI analysis complete" or "AIDA-MATE AI Review". `tool_calls_count` is `SandboxToolContext.tool_calls_count`, incremented by the tool wrappers themselves as calls happen (`app/tools/sandbox_tools.py`) and read back by `OpenAIAgentRunner.analyze()` after the run — the same "never let the model report on itself" discipline as the risk score, applied to proof of investigation instead of proof of judgment.

`failed_specialists` carries `AgentRunOutcome.failed_specialists` straight through — a plain Python list of agent names, never anything an LLM writes into. `orchestrator.py` computes `needs_human_review = assessment.needs_human_review or bool(failed_specialists)`: a non-empty list forces human review and a `PARTIAL` heading in both published comments (§6a, `core/report.py`), regardless of how low the deterministic score came out. A gap in analysis coverage is exactly the situation a human should be told about, and this OR is immune to gaming for the same reason the risk score is — it depends on a *count of names AIDA-MATE's own code recorded*, not on anything a model emits.

### Review job (`models/review.py`)

`ReviewJob` carries: `id`, `idempotency_key`, `delivery_id`, `linear_issue_id`/`linear_issue_identifier`, `organization_id`, `trigger_source`, `agent_session_id`, `pull_request` (a `PullRequestRef`), `head_sha`, `content_key`, `status`, `attempt_number`, `previous_review_id`, `sandbox_id`, timestamps, `risk_level`, `result`, `error_code`/`error`.

**Status machine:**
```
QUEUED → PROVISIONING → FETCHING_PR → ANALYZING → CLASSIFYING
       → UPDATING_GITHUB → UPDATING_LINEAR → COMPLETED
                    ↘ SKIPPED      (content-key check: already reviewed)
                    ↘ FAILED       (from any state)
                    ↘ INTERRUPTED  (process died mid-review; found at startup)
```

`PROVISIONING` is set by the worker before the orchestrator runs (it's when the Linear acknowledgement happens); the optional sandbox/agent stage runs *inside* `ANALYZING` rather than getting its own status. Four states are terminal (`COMPLETED`, `SKIPPED`, `FAILED`, `INTERRUPTED`); only the latter two are `is_retryable`.

**Three independent guards stop duplicate work, at increasing cost:**

| Stage | Key | Guards against | Known when |
|---|---|---|---|
| **Delivery** | `Linear-Delivery` header | A redelivered webhook, dropped before any GitHub call | At the webhook, from the HTTP header (not the payload's `webhookId`, which names the *subscription* and repeats across every delivery) |
| **Intake** | `session:<id>` or `issue:<id>` | A second trigger for an issue already mid-review | At the webhook |
| **Content** | `issue : pr_number : head_sha` | Re-reviewing a PR revision already `COMPLETED` | After the PR resolver + GitHub fetch — the only stage that knows the head SHA |

The content check is **enforced**, not merely observed: finding a prior `COMPLETED` review of the same `content_key` sets the job `SKIPPED` and returns before the sandbox or LLM is touched (`ReviewOrchestrator._skip_if_already_reviewed`). This is what makes a Linear delegation mean "AIDA-MATE is responsible for this issue" rather than "run exactly one review, then require reassignment" — re-delegating an unchanged PR costs one PR fetch, not a full sandbox+LLM run.

A retry (`ReviewService.retry`, `POST /reviews/{id}/retry`) is the supported way to force another attempt at a `FAILED` or `INTERRUPTED` review. It creates a new job with an attempt-scoped idempotency key (`retry:<original_id>:<attempt>`, so it can never collide with a webhook-generated key) and `attempt_number > 1`, which is also the signal `_skip_if_already_reviewed` checks to bypass itself — a retry is definitionally a request to review this revision again, so the check that exists to prevent *accidental* re-review must not block a *deliberate* one.

### Persistence (`services/sqlite_job_repository.py`)

`IReviewJobRepository` has two implementations, selected by whether `REVIEW_STORE_PATH` is set:

- **`InMemoryReviewJobRepository`** (default) — plain dicts behind an `asyncio.Lock`. Fast, zero dependencies, loses all review state on restart.
- **`SqliteReviewJobRepository`** — stdlib `sqlite3` on `asyncio.to_thread` (matching the pattern already used by both sandbox backends), not an async driver. The lifecycle fields worth indexing (`idempotency_key`, `delivery_id`, `content_key`, `status`) are real columns; the rest of `ReviewJob` round-trips through one JSON `payload` column, trading SQL-queryability of those fields for zero migrations while the schema is still moving.

Two guarantees only the SQLite backend can make:

1. **Dedup is race-safe.** A UNIQUE index on `idempotency_key` means two concurrent webhook deliveries racing to create the same job cannot both win — the loser gets an `IntegrityError` and reads back the winner's row. A `SELECT`-then-`INSERT` in application code always has a gap between the two statements; the database closing that gap is what a `if review_exists():` check in Python cannot do.
2. **Restart recovery.** At startup, `recover_interrupted()` finds every non-terminal job (the process died somewhere in `ANALYZING`, `UPDATING_GITHUB`, etc.) and marks it `INTERRUPTED` — visible and retryable, instead of a row that is neither running nor finished and permanently stuck. `InMemoryReviewJobRepository.recover_interrupted()` is a no-op that satisfies the same interface, since a fresh in-memory store never has anything to recover.

Neither backend stores a secret: `ReviewJob` holds identifiers, status, and results, never credentials. The SQLite file is not encrypted at rest regardless — keep `REVIEW_STORE_PATH` out of version control, the same as `.env`.

### Risk scoring (`core/risk_engine.py`)

Configurable defaults, scored **per distinct area, counted once** — not per finding:

| Area | Points | | Area | Points |
|---|---|---|---|---|
| authentication | 50 | | ci_cd | 20 |
| authorization | 50 | | business_logic | 20 |
| security | 40 | | ai | 20 |
| migrations | 40 | | backend | 15 |
| payments | 40 | | configuration | 10 |
| infrastructure | 30 | | dependencies | 10 |
| api | 30 | | frontend | 5 |
| database | 25 | | testing | 3 |
|  |  | | documentation | 1 |

Thresholds: `0–20 → LOW` · `21–60 → MEDIUM` · `61+ → HIGH` (configurable via `RISK_LOW_MAX_SCORE`/`RISK_MEDIUM_MAX_SCORE`).

**Why per-area, not per-finding.** Counting per finding would let the LLM inflate risk simply by emitting more observations about the same area — a chattier agent would score higher than a terse one for an identical diff, handing the model indirect control over risk magnitude. Counting per distinct area closes that door: `assess_risk()` unions the areas detected from file paths with the *categories* of any agent findings, then scores each area once. Severity is carried for display and can force human review, but never multiplies the score.

`needs_human_review` is `True` for HIGH unconditionally, configurable for MEDIUM, `False` for LOW (unless a finding's severity is HIGH/CRITICAL, which forces it regardless of the computed level). **HIGH can never be silently bypassed.**

---

## 6. Agent / tool boundaries

### Why the orchestrator is Python, not an LLM

The pipeline `Resolve → Fetch → (Sandbox → Review) → Risk/Label → Publish` has **exactly one valid execution order** — every step consumes the previous step's output. An LLM tool-calling loop over a fixed-order DAG adds skip/reorder/retry/hallucinated-completion failure modes and buys no flexibility, in the layer that most needs predictability.

The OpenAI Agents SDK remains the only agent framework in the system (no LangGraph/CrewAI/AutoGen) and the one reasoning step that exists runs through `Runner.run()`. Only *sequencing* is fixed Python.

### Why GitHub access is not an agent

The spec is explicit: GitHub interaction should involve "no reasoning." `services/github_service.py` is a plain typed async `httpx` client; nothing about listing files, fetching a diff, or applying a label benefits from being routed through an LLM.

### Model provider abstraction

```python
class IReviewAgentRunner(Protocol):
    async def analyze(
        self, pull_request: PullRequest, sandbox: ISandbox, *, review_id: str
    ) -> AgentRunOutcome: ...
```

`OpenAIAgentRunner` implements it now, using `openai-agents` to build and run 6 `Agent` objects behind that one `analyze()` call — see §6a for the pipeline they form. The return is `AgentRunOutcome` (the Judge's `ReviewAnalysis` + model + `tool_calls_count` summed across every stage + `failed_specialists`), not bare `ReviewAnalysis` — the same "the model doesn't get to report on itself" discipline as the risk score, applied to proof of tool use. An `AnthropicAgentRunner` could be added later without touching GitHub, Linear, sandbox, or risk logic — the `anthropic` package just isn't installed in this environment yet. Provider is selected by `MODEL_PROVIDER`; only the selected provider's API key is required.

**Azure OpenAI / Azure AI Foundry** is supported via the same interface: setting `OPENAI_API_VERSION` alongside `OPENAI_BASE_URL` switches `OpenAIAgentRunner`'s construction from `AsyncOpenAI` to `AsyncAzureOpenAI` (installed once, globally, via `agents.set_default_openai_client` — the SDK has no per-call way to select a client). Tracing upload is disabled whenever a custom `OPENAI_BASE_URL` is set, since it always targets OpenAI's own tracing endpoint regardless of the override, which a gateway- or Azure-scoped key typically cannot authenticate against. Live-verified against a real Azure AI Foundry "v1" deployment this session, including a genuine `read_file` tool call and a genuine finding.

**Model IDs** are configuration, never hardcoded (`REVIEW_MODEL`/`UTILITY_MODEL`). Current defaults (`gpt-5.6-sol` / `gpt-5.6-luna`) were verified against OpenAI's live docs when set, per the project's "never speculate on model IDs" rule — re-verify before relying on them if significant time has passed.

### Sandbox boundary

| Runs on trusted host | Runs in the Docker Sandbox |
|---|---|
| Webhook verification, job management | Archive extraction |
| Linear/GitHub API calls (including the archive download) | `read_file`, `search_code`, `list_files` |
| Risk + Label engines | — |
| **All writes** (labels, comments) | — |
| Holds GitHub + Linear credentials | **Holds no credentials** |

The LLM itself runs on the host (it's an API call, not a sandboxed process) and calls sandbox tools; the sandbox only executes filesystem operations against the extracted PR tree and returns bounded output. Sandbox teardown is unconditional (`finally`) and never masks the primary result.

**How PR content reaches the sandbox without a credential**: the host downloads the PR archive via GitHub's REST tarball endpoint (`GET /repos/{owner}/{repo}/tarball/{sha}`, using its own GitHub token), then uploads the raw bytes into a per-review sandbox (`docker sandbox create shell <workspace>`, which bind-mounts a host temp directory into the sandbox at an identical path — no separate `cp` step needed). The sandbox extracts the archive itself via `exec`. The sandbox never sees a token; the host never executes untrusted repository code.

**`SANDBOX_MODE=local` — the Docker-free stopgap.** `LocalSandboxFactory`/`LocalSandbox` (`app/services/local_sandbox_service.py`) implement the same `ISandbox`/`ISandboxFactory` Protocols but back them with a plain host-side temp directory instead of a Docker Sandbox VM. It exists because Docker Sandboxes cannot run on every host — this session's own development machine included, where Docker Desktop's engine requires a WSL2 kernel update that cannot be installed without admin rights. `exec()` never runs an arbitrary shell: AIDA-MATE's own code is the only caller (the LLM only ever supplies tool *arguments*, never a raw command string), and it recognizes exactly the three fixed command shapes AIDA-MATE itself generates — archive extraction (native `tarfile`, replicating `--strip-components=1`), `find`-shaped listing, and `grep`-shaped search — giving each a native Python implementation instead of shelling out to POSIX utilities this Windows host doesn't have. Nothing from the PR is ever executed as code, matching the same guarantee the Docker-backed tools already provide. **This is explicitly not an isolation boundary** — the "runs in the Docker Sandbox" column above becomes "runs on the host, read-only" — so `docker` remains the default and `local` should only be selected where Docker Sandboxes genuinely cannot run.

---

## 6a. The manager pattern inside `review_agent.py`

`OpenAIAgentRunner.analyze()` is a **manager/specialist** pipeline, built the same way `orchestrator.py` builds the outer review pipeline one level up: plain Python sequencing (`await`, `asyncio.gather`), never the SDK's own agent-to-agent handoff mechanism, and never a second LLM deciding what runs next or in what order. The specialist sequence (Context → parallel batch → Judge) has exactly one valid shape, so handing an LLM control over it would add the same skip/reorder/retry failure modes §6 already argues against for the outer pipeline — with no offsetting benefit.

**Why Testing runs inside the concurrent batch, not after it.** The flow "Context → specialists → Judge" could be read as suggesting Testing waits for Security/Code/Architecture to finish first. It doesn't: Testing has no data dependency on any of the other three's findings, only on the same PR facts and context hint they all receive, so it runs in the same `asyncio.gather` batch. Serializing it would only add latency for no correctness benefit — the same "don't run sequentially when they can operate independently" reasoning behind running the four specialists concurrently at all.

**Per-agent system prompts, not one shared prompt.** Each of the 6 agents (`app/prompts/review_prompt.py`) has a system prompt scoped to exactly its lane — the Security prompt digs into auth/injection/secrets and explicitly defers correctness and architecture concerns to the other specialists, and likewise for Code/Architecture/Testing. All six share one `UNTRUSTED_INPUT_PREAMBLE` (the prompt-injection defense from §1's Trust model) so that defense cannot be present in five prompts and quietly missing from a sixth.

**Partial-failure policy — a specialist failing is data, not a crash:**

| Stage fails | Effect |
|---|---|
| Context | Non-fatal. Recorded in `failed_specialists`; specialists proceed with an honest "(context unavailable)" placeholder instead of a hint, with no reduction in tool access. |
| One of Security/Code/Architecture/Testing | Non-fatal, as long as ≥1 specialist survives. Recorded in `failed_specialists`; the Judge reconciles whoever survived. |
| All 4 specialists | Fatal — `AgentError` is raised *before* the Judge is ever called. There is nothing evidentiary left to reconcile. |
| Judge | Always fatal. It is the only source of the `ReviewAnalysis` that ever reaches `assess_risk()`, so there is no reasonable partial result without a synthesis step. |

Each specialist call has its **own** `SPECIALIST_TIMEOUT_SECONDS` budget (default 60s), separate from `AGENT_TIMEOUT_SECONDS` (the whole `analyze()` call's outer budget, default 300s). Without a per-specialist timeout, one hung specialist would silently stall the entire concurrent batch until the *outer* timeout fired and failed the *whole review* — the opposite of the honest, granular partial-failure reporting this design is for. The Judge itself has no inner timeout: its failure is unconditionally fatal, so the outer `agent_timeout_s` is the only bound it needs.

**Per-specialist `SandboxToolContext`, not one shared context.** Four specialists run concurrently via `asyncio.gather`; each gets its own `SandboxToolContext` (carrying its own `agent_name` for log correlation and its own `tool_calls_count`) rather than sharing one, so a tool-call log line is unambiguous about which specialist made it even though several are in flight at once. `OpenAIAgentRunner.analyze()` sums every stage's count afterward for the published `tool_calls_count`.

**Why this doesn't reopen "the LLM controls risk."** `orchestrator.py` still calls `assess_risk()` with exactly one findings list — the Judge's. Even a Judge that reconciles poorly (e.g. lists the same bug twice because two specialists both noticed it) cannot move the score: `assess_risk()`'s `effective_areas = set(areas) | {finding.category for finding in findings}` is a set union, so duplicate or overlapping categories from any number of specialists collapse to one contribution. A sloppy Judge can only cost comment readability, never risk-score integrity. `needs_human_review = assessment.needs_human_review or bool(failed_specialists)` is likewise immune: it is driven by a count of agent *names* AIDA-MATE's own code recorded, never by anything a model outputs.

**Cost, honestly.** Four independent specialists genuinely investigating the same diff may legitimately call `read_file` on the same file more than once — that's real independent verification, not a bug to optimize away. Each prompt's `_INVESTIGATE_EFFICIENTLY` clause instructs "no tool calls needed" as a valid, encouraged outcome for a trivial diff, but per-review tool-call volume and LLM cost on a non-trivial PR are structurally higher than the single-agent design this replaces — see §7's status table note.

---

## 7. Implementation status

| Area | Scope | Status |
|---|---|---|
| Scaffold | Config, models, interfaces, FastAPI, health, webhook boundary | **Complete** |
| Linear OAuth | PKCE, `actor=app`, token exchange/refresh/revoke, actor discovery | **Complete** |
| Webhook → job | Queue, worker pool, 10s acknowledgement, three-stage dedup | **Complete** |
| Review lifecycle | SHA-scoped identity, enforced skip-if-reviewed, `SKIPPED`/`INTERRUPTED` states, explicit retry endpoint | **Complete** |
| PR resolver | Attachment / branch-name / title-body strategies | **Complete** |
| GitHub integration | Reads, writes, archive download, App + dev-token auth | **Complete** |
| Deterministic engines | Area detection, risk scoring, label derivation | **Complete** |
| Publication | GitHub labels + comment, Linear comment/activity | **Complete** |
| Sandbox | `docker sandbox` CLI adapter, plus `SANDBOX_MODE=local` Docker-free stopgap | **Complete** — `docker` mode confirmed non-functional on this dev host (WSL2, no admin); `local` mode live-verified |
| Review agent | OpenAI Agents SDK (OpenAI + Azure OpenAI/Azure AI Foundry), sandboxed tools, structured output, real tool-call counting | **Complete** — live-verified with genuine tool calls and a real finding (single-agent pipeline; see the Multi-agent split row below for the current 6-agent shape) |
| Multi-agent split | Context Agent + 4 concurrent specialists (Security/Code/Architecture/Testing) + Judge, behind the same `analyze()` seam — see §6a | **Delivered and live-verified** — 709 tests passing, `ruff` clean, plus a real end-to-end run (below). Per-review tool-call volume and LLM cost are now structurally higher than the single-agent design on a non-trivial PR (independent specialists may legitimately re-`read_file` the same file) — an accepted trade-off for genuine independent investigation, not a bug |
| Orchestrator wiring | Sandbox + agent as optional pipeline stage | **Complete** |
| Persistence | SQLite-backed job store with restart recovery; file-backed Linear OAuth | **Complete**, opt-in via `REVIEW_STORE_PATH`/`LINEAR_TOKEN_STORE_PATH` |
| Live end-to-end run | Against a real PR, real Linear workspace, real sandbox, real AI agent | **Done** — one real finding + one legitimate zero-findings result against the earlier single-agent pipeline, plus one real 6-specialist run against the new pipeline (`sentinel-trading-bot#6`: HIGH/288, 6 reconciled findings, 11 real tool calls across 4 concurrently-running specialists, `failed_specialists: []`) — see §8.7 |
| New-commit auto-review | GitHub `pull_request` webhook → automatic re-review on a new SHA | Not started — see §8.6 |

Each change lands with a real test run (not just written-and-assumed) before being considered complete.

---

## 8. Known constraints (stated honestly)

1. **Docker Sandboxes are confirmed broken on the current development host, not just unverified.** A live `docker sandbox create ... shell ...` probe this session failed consistently with `starting LinuxKit VM: VM exited ungracefully during shutdown: context canceled`, traced to Docker Desktop's engine requiring a WSL2 kernel update that cannot be installed without admin rights on this machine. `SANDBOX_MODE=local` (§ "Sandbox boundary" above) exists specifically to unblock live verification on hosts like this one; it is not a substitute for fixing the real Docker Sandbox path where a working host is available.

2. **Sandbox and agent are optional, not mandatory.** Unlike the earlier Daytona-based design, there is no "fails closed without a sandbox" requirement — the deterministic area/risk/label pipeline never depended on one. `/ready` reports `sandbox: false` and reviews simply run without agent-contributed findings when no sandbox backend is available. Published text (`ReviewResult.ai_analysis_ran`) is honest about which happened — GitHub/Linear comments never say "AI analysis complete" unless the agent actually ran.

3. **GitHub App is not yet provisioned.** A development PAT (`GITHUB_DEV_TOKEN`) works as a fallback; production should move to the GitHub App path (`GITHUB_APP_ID`/`GITHUB_INSTALLATION_ID`/`GITHUB_PRIVATE_KEY`).

4. **Persistence is opt-in, not on by default.** Both `IReviewJobRepository` and the Linear token store have a durable implementation now (`SqliteReviewJobRepository`, `FileLinearTokenStore`), but the default remains in-memory unless `REVIEW_STORE_PATH` / `LINEAR_TOKEN_STORE_PATH` are set — matching production, where a real encrypted database is the intended path and neither of these local-file backends is. Set both for local development; job state and OAuth installations then survive a restart, and a job left mid-flight by a crash is recovered as `INTERRUPTED` rather than lost.

5. **`git` is not installed on the current development host.** No impact by design — PR content is retrieved through GitHub's REST API (metadata and diffs) and archive endpoint (full tree, for sandboxed analysis) rather than local cloning.

6. **A new commit does not trigger a new review by itself.** AIDA-MATE only hears from Linear — there is no GitHub `pull_request` webhook yet — so a genuinely new SHA is reviewed automatically only when *some* Linear event AIDA-MATE is already listening for arrives (a fresh delegation, or an explicit `POST /reviews/{id}/retry`). What *is* fixed: re-delegating an issue whose current SHA was already reviewed is correctly a no-op (`SKIPPED`) rather than a wasteful duplicate review — see §5's review-job section above.

7. **The 6-agent pipeline is live-verified, and one real run already changed a published score — honestly, not a regression.** Re-running `sentinel-trading-bot#6` (same PR, same head SHA) through the new Context/Security/Code/Architecture/Testing/Judge pipeline produced HIGH/288 with `area: api` and `area: infrastructure` newly detected, versus the earlier single-agent pipeline's HIGH/228 without them. The deterministic engine and its weights did not change; four independently-investigating specialists simply surfaced genuine findings (an API-contract-relevant change, an infrastructure/CI-CD-relevant change) that one generalist agent's single pass had missed. This is the coverage improvement the multi-agent split exists to deliver, not an inconsistency — but it does mean a re-review of an already-reviewed PR under the new pipeline can legitimately move the score upward. Logs confirmed genuine concurrency (interleaved `AGENT_TOOL_CALL` lines from `security`/`code`/`architecture` in the same second), 11 real tool calls, a coherent 6-finding Judge reconciliation with no duplicates, and `failed_specialists: []` correctly producing the non-`PARTIAL` heading.
