# AIDA-MATE

**AI-powered Pull Request Risk Analysis Agent.**

Assign a Linear issue to AIDA-MATE → it finds the linked GitHub PR, optionally analyzes it inside an isolated Docker Sandbox with an AI agent, classifies the risk deterministically, and writes labels and a summary back to both GitHub and Linear.

### Current Version

**AIDA-MATE does not write or modify code.** The current system is focused exclusively on **analysis and classification** of software development tasks and repositories.

The current version does **not** perform:

* Code generation or modification
* Commits
* Branch creation
* Pull request creation
* Pull request merging
* Direct implementation of development tasks

### Future Version

Future versions of AIDA-MATE will extend beyond analysis and classification to support the complete software development workflow, including:

* Code generation and modification
* Creating and managing branches
* Creating commits
* Creating and updating pull requests
* Reviewing and validating changes
* Merging pull requests
* Automated testing and verification

The long-term goal is to evolve AIDA-MATE from an **analysis and classification system** into an end-to-end **AI software development agent**.


See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design: sequence diagram, component responsibilities, data models, and agent/tool boundaries.

---


| Area | Scope | Status |
|---|---|---|
| Scaffold | Config, models, interfaces, FastAPI, health, webhook boundary | ✅ **Complete** |
| Linear OAuth | PKCE flow, token exchange/refresh/revoke, actor discovery | ✅ **Complete** |
| Webhook → job | Queue, worker pool, 10s acknowledgement, three-stage dedup | ✅ **Complete** |
| Review lifecycle | SHA-scoped identity, enforced skip-if-reviewed, `SKIPPED`/`INTERRUPTED` states, explicit retry | ✅ **Complete** |
| PR resolver | Attachment / branch-name / title-body strategies | ✅ **Complete** |
| GitHub integration | Reads, writes, archive download, App + dev-token auth | ✅ **Complete** |
| Deterministic engines | Area detection, risk scoring, label derivation | ✅ **Complete** |
| Publication | GitHub labels + comment, Linear comment/activity | ✅ **Complete** |
| Sandbox | `sbx` CLI adapter (Docker Sandboxes), plus a `SANDBOX_MODE=local` Docker-free stopgap | ✅ **Complete** — both modes live-verified on this dev host (`sbx` create/exec/remove cycle, and `local`) |
| Review agent | 6-agent pipeline behind one `analyze()` call: Context Agent, four concurrent specialists (Security/Code/Architecture/Testing), a Judge that reconciles into one result | ✅ **Complete and live-verified** (709 tests, `ruff` clean, plus a real run — see below) |
| Persistence | SQLite-backed job store (`REVIEW_STORE_PATH`) with restart recovery; file-backed Linear OAuth (`LINEAR_TOKEN_STORE_PATH`) | ✅ **Complete** |
| Live end-to-end run | Real PR, real Linear workspace, real AI agent | ✅ **Done** — two runs of the earlier single-agent pipeline (one real finding, one legitimate zero-findings result), plus one real run of the new 6-agent pipeline (`sentinel-trading-bot#6`: HIGH/288, 6 reconciled findings, 11 tool calls across 4 genuinely concurrent specialists) |

**What works today:** the full pipeline — Linear delegation → resolve PR → fetch → sandbox + AI analysis → deterministic risk/label classification → GitHub labels/comment → Linear comment — has run end to end against a real Linear workspace, a real GitHub PR, and a real Azure AI Foundry model, both before and after the 6-agent upgrade. The seam it runs behind (`IReviewAgentRunner.analyze()`) never changed, so the same deterministic risk/label/publish behavior applies either way. Re-running an already-reviewed PR through the new pipeline can legitimately move the published score — in the live run above it went from HIGH/228 to HIGH/288 because four independently-investigating specialists surfaced genuine `api`/`infrastructure` findings a single generalist pass had missed, not because anything deterministic changed. A PR revision is reviewed at most once per pipeline run: re-delegating an already-reviewed SHA is a no-op (`SKIPPED`), and `POST /reviews/{id}/retry` is the supported way to force another attempt — Linear assignment no longer needs to be undone and redone to get a re-review. Review state and Linear OAuth both survive a server restart when `REVIEW_STORE_PATH` / `LINEAR_TOKEN_STORE_PATH` are set; a job left mid-flight by a crash is recovered as `INTERRUPTED` (retryable) rather than lost. The service correctly reports its own capabilities via `/ready` (`sandbox`, `github`, `linear`), so a missing piece is visible rather than silently degraded.

**What's still open:** GitHub does not yet notify AIDA-MATE when a PR gets a new commit — a review only starts from a Linear trigger today (delegation, or explicit retry), not automatically when the SHA changes. The `docker` sandbox backend remains unverified on any host with a working Docker Sandbox VM (confirmed broken only on this specific dev machine). A specialist (or the Context Agent) failing mid-review is handled — recorded in `failed_specialists`, forcing human review and a `PARTIAL` published comment — but that specific path is only exercised by unit tests so far, not a live failure. `UTILITY_MODEL` must point at a real Azure deployment, same as `REVIEW_MODEL` — a placeholder value here fails the Context Agent immediately with `DeploymentNotFound` (caught live during this session's verification).

---

## Setup

Requires **Python 3.12+**.

```bash
cd gitmate
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -e ".[dev]"
copy .env.example .env            # then fill in real values
```

Model provider dependencies are optional extras, installed only when needed:

```bash
pip install -e ".[dev,openai]"        # OpenAI Agents SDK
pip install -e ".[dev,anthropic]"     # Anthropic SDK (no runner implementation yet)
```

The sandbox (Docker Sandboxes) is a system dependency, not a Python package — see [Sandbox setup](#sandbox-setup-docker-sandboxes) below.

## Run

```bash
python -u -m uvicorn app.main:app --reload
```

`-u` (or `PYTHONUNBUFFERED=1`) matters if stdout is redirected to a file (e.g. running as a background process): Python fully buffers stdout when it isn't a real terminal, so log lines can sit unflushed for a while without it — the app's own logging is already line-per-record and flushes correctly under a real console or with `-u`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /ready` | Readiness + which capabilities are configured (`sandbox`, `github`, `linear`) |
| `POST /webhooks/linear` | Linear event intake (HMAC-verified) |
| `GET /auth/linear/install` | OAuth start |
| `GET /auth/linear/callback` | OAuth redirect |
| `GET /auth/linear/status` | Authorized workspaces (identity/scopes only, never tokens) |
| `GET /reviews` | Recent reviews, newest first |
| `GET /reviews/{id}` | One review's status/verdict |
| `POST /reviews/{id}/retry` | Re-run a `FAILED` or `INTERRUPTED` review as a new attempt |

## Test

```bash
pytest              # 700+ tests, no network, no credentials, no Docker required
ruff check app tests
```

The suite runs offline on a clean checkout — `tests/conftest.py` injects a dummy environment automatically.

---

## Run in Docker

Portable by construction: the image pulls only from PyPI, bakes in no secrets, and reads all
configuration from the environment — it builds and runs identically on any machine with Docker,
including a different laptop than the one that built it.

```bash
copy .env.example .env     # fill in real values — this file never enters the image
docker compose up --build
```

That's it — `docker compose up --build` builds the image, starts the container, and exposes it on
`http://localhost:8000`. Review history and Linear OAuth installations persist in a named volume
(`aida-mate-data`) across rebuilds and restarts.

**To move to a different machine:** copy the repository (or `git clone` it once it's in a repo) and
your `.env` file — everything else is self-contained in the image. The named volume does *not*
travel with the code; if you want existing review history to come along too, `docker volume` has
export/import commands for that, or just start fresh (a lost SQLite file loses history, not
correctness — the app works the same either way).

**`SANDBOX_MODE`**: the compose file pins this to `local`, which runs entirely in pure Python — no
Docker socket needs to be mounted into the container. `docker` mode (real Docker Sandbox isolation)
would need the container to reach a Docker daemon, which typically means mounting
`/var/run/docker.sock` into it — a real security trade-off (a container with that access can
effectively reach host root), not something to enable without deliberately deciding to. Stick with
`local` unless you've specifically set that up and understand the trade-off.

**Without Docker Compose**, the two commands it wraps:

```bash
docker build -t aida-mate .
docker run -p 8000:8000 --env-file .env -e SANDBOX_MODE=local -v aida-mate-data:/data aida-mate
```

**Not yet done:** an actual cloud deployment (Railway, Fly.io, a VPS, Azure — wherever this ends up
running long-term) — that's a separate decision with its own setup once you're ready for it. This
section only covers making the container itself portable and correct; it doesn't pick where it runs.

---

## Configuration

Everything comes from the environment. **No credential is ever hardcoded.** See [.env.example](.env.example) for the full list.

| Group | Notes |
|---|---|
| **Linear** | OAuth (`LINEAR_CLIENT_ID`/`SECRET`) is the supported path. `LINEAR_WEBHOOK_SECRET` and `AIDA_MATE_LINEAR_ACTOR_ID` are needed to receive and recognise assignments. |
| **GitHub** | GitHub App (`GITHUB_APP_ID` + `GITHUB_INSTALLATION_ID` + `GITHUB_PRIVATE_KEY`) is the supported path. |
| **Sandbox** | `SANDBOX_MODE` (default `docker`) needs `SANDBOX_BINARY` on PATH with Docker Desktop running — see below. `local` is a Docker-free stopgap. Optional either way: reviews still run without it. |
| **Model** | Set `MODEL_PROVIDER`, then supply only that provider's key. `OPENAI_BASE_URL` points at an organization's own OpenAI-compatible gateway instead of `api.openai.com`; `OPENAI_API_VERSION` additionally selects the Azure OpenAI / Azure AI Foundry client (only needed for those endpoints — see [Model provider setup](#model-provider-setup) below). Model IDs are configuration: `REVIEW_MODEL` (default `gpt-5.6-sol`) and `UTILITY_MODEL` (default `gpt-5.6-luna`). |
| **Persistence** | `REVIEW_STORE_PATH` persists review jobs to SQLite; `LINEAR_TOKEN_STORE_PATH` persists OAuth installations to JSON. Both unset by default (pure in-memory — a restart loses everything). Neither is encrypted at rest; keep both paths out of version control, same as `.env`. |
| **Risk policy** | `RISK_LOW_MAX_SCORE`, `RISK_MEDIUM_MAX_SCORE`, `MEDIUM_REQUIRES_HUMAN_REVIEW` — thresholds are tunable, not baked into code. |
| **Execution** | `REVIEW_CONCURRENCY` (default 2) bounds parallel reviews so a webhook burst cannot exhaust sandbox quota or LLM budget; `REVIEW_QUEUE_MAXSIZE` turns overload into visible back-pressure. |

Startup **fails fast** on invalid configuration and logs a warning for every missing capability, so gaps surface at boot rather than mid-review.

`*_DEV_TOKEN` / `LINEAR_DEV_API_KEY` exist for local development before App/OAuth credentials are provisioned. Leave them unset in production.

---

## Sandbox setup (Docker Sandboxes)

AIDA-MATE uses **`sbx`**, Docker's standalone CLI for its "Docker Sandboxes" product, to isolate PR content from the host — live-verified this session (a full create → exec → remove cycle). The older `docker sandbox` CLI *plugin* AIDA-MATE originally targeted has since been deprecated and removed by Docker; `sbx` is its replacement and is not a `docker <subcommand>` plugin invocation.

```bash
# 1. Install Docker Sandboxes (https://www.docker.com/products/docker-sandboxes)
#    and start Docker Desktop.

# 2. Confirm Docker is running
docker info

# 3. Confirm sbx is installed
sbx version

# 4. One-time interactive login
sbx login

# 5. List active sandboxes (AIDA-MATE creates/removes its own per review;
#    this is just for checking the CLI works end-to-end)
sbx ls
```

**Unlike the earlier Daytona-based design, the sandbox is optional.** Without it, `/ready` reports `"sandbox": false` and reviews still run the full deterministic area/risk/label pipeline — just without AI-generated findings layered on top. There is no "fails closed" behavior tied to the sandbox.

**`SANDBOX_MODE=local`** is a stopgap for hosts where Docker Sandboxes cannot run at all — for instance, a locked-down machine with no admin rights to install the WSL2 update Docker Desktop's engine needs. It runs the same read-only inspection (list files, read a file, grep) directly against a host-side scratch temp directory instead of an isolated VM. Nothing from the PR is ever executed as code either way, but `local` is **not an isolation boundary** the way `docker` is — prefer `docker` wherever it will actually run, and treat `local` as a way to unblock development/verification, not a production default.

Repository contents and PR text are untrusted input regardless. AIDA-MATE must not execute them on the API host, and a prompt-injection payload buried in a PR must not be able to talk it into a low-risk verdict. Two independent defenses:

1. **The sandbox holds no credentials.** The host downloads the PR archive via GitHub's REST API and writes the raw bytes into the sandbox's mounted workspace; every GitHub and Linear write happens on the host *after* the sandbox is destroyed.
2. **Policy is deterministic.** Even a fully compromised agent can only emit findings — it cannot set the risk level, the labels, or `needs_human_review`.

---

## Model provider setup

`OPENAI_BASE_URL` alone points the agent at any OpenAI-compatible endpoint — an organization's own gateway or proxy in front of the OpenAI API. `OPENAI_API_VERSION` is the extra step needed specifically for **Azure OpenAI / Azure AI Foundry**, whose endpoints reject requests missing an `api-version` query parameter (recognizable if your base URL looks like `https://<resource>.services.ai.azure.com/...`). Setting `OPENAI_API_VERSION` switches AIDA-MATE to the Azure client (`AsyncAzureOpenAI`), which adds that parameter automatically; the plain OpenAI client has no equivalent.

Two things worth knowing if you're setting this up against Azure AI Foundry specifically:

- The api-version value is not discoverable from AIDA-MATE — it's whatever your Azure resource/project actually supports. Check the "Code"/sample-request tab in Azure AI Foundry's portal for your deployment; the value appears directly in the sample URL's `api-version=` query parameter.
- Azure AI Foundry's newer **"v1" API surface** (a base URL ending in `/openai/v1`) needs *no* `api-version` at all — leave `OPENAI_API_VERSION` unset in that case and use the plain client.
- `REVIEW_MODEL` must be the exact **deployment name** you created in Azure, not a generic model name — Azure will 404 with `DeploymentNotFound` otherwise.

Whichever endpoint is configured, tracing upload is automatically disabled: it always targets OpenAI's own tracing endpoint regardless of `OPENAI_BASE_URL`, which a gateway- or Azure-scoped key typically cannot authenticate against.

---

## Review lifecycle

A Linear delegation does not mean "run exactly one review, then require reassignment" — it means "AIDA-MATE is responsible for this issue." The two states are tracked separately:

- **Assignment** — who Linear says owns the issue. Unaffected by anything below.
- **Review** — AIDA-MATE's own record of whether *this exact PR revision* has been analyzed.

A review's identity is `linear_issue_id + repository + pr_number + head_sha` (`build_content_key`). Two consequences:

- **Explicit assignment/delegation always reviews.** The pipeline fetches the PR and runs a fresh review even when its SHA has already been reviewed. Duplicate deliveries and automatic/background triggers still use the content check to avoid duplicate work.
- **A genuinely new commit is a genuinely new review**, with its own row in history rather than overwriting the old one.

Three independent guards stop duplicate work, at increasing cost — see the module docstring in `app/services/review_service.py` for the full reasoning:

1. **Delivery** (`Linear-Delivery` header) — a redelivered webhook is dropped before any GitHub call.
2. **Intake** (`idempotency_key`) — a second trigger for an issue already mid-review collapses onto the in-flight job.
3. **Content** (`content_key`) — a trigger for a PR revision already `COMPLETED` stops before the sandbox and LLM.

**Retrying** a `FAILED` or `INTERRUPTED` review is explicit — `POST /reviews/{id}/retry`. The retry is a new job (new `attempt_number`, chained via `previous_review_id`) that bypasses the same-SHA content check.

**Restart recovery**: with `REVIEW_STORE_PATH` set, any job still non-terminal at startup (the process died mid-review) is marked `INTERRUPTED` rather than left stuck — `is_retryable` is true for that state, so `POST /reviews/{id}/retry` picks it back up.

**Known gap**: none of this is triggered by a new commit alone. GitHub pushing to a PR doesn't reach AIDA-MATE — there's no GitHub webhook yet, only the Linear one — so today a new SHA is only reviewed when Linear sends *some* event AIDA-MATE is already listening for (delegation, or an explicit retry). Automatic "new commit → new review" needs a GitHub `pull_request` webhook, deliberately left for a follow-up since it depends on the persistence layer above to map `repo + PR# → Linear issue`.

---

## Future scope

A planned future version of AIDA-MATE takes on the implementation itself, not just the review of it. The ticket still originates in Linear, same as today's trigger — but from there, an agent owns everything downstream, through to a verified, opened PR:

```
Linear ticket
  ↓
Understand task
  ↓
Inspect repository
  ↓
Plan implementation
  ↓
Modify files
  ↓
Run commands
  ↓
Observe errors
  ↓
Fix errors
  ↓
Run tests
  ↓
Run application
  ↓
Verify result
  ↓
Screenshot/video if required
  ↓
Ask human for decision if necessary
  ↓
Commit
  ↓
Push
  ↓
Create/update PR
```

---

## Project layout

```
app/
├── main.py         FastAPI app + composition root
├── api/            HTTP boundary — health, Linear webhook, OAuth, reviews (list/get/retry)
├── agents/         orchestrator.py (plain Python) + review_agent.py (OpenAI Agents SDK, 6-agent pipeline)
├── tools/          sandbox_tools.py — list_files / read_file / search_code
├── prompts/        review_prompt.py — one system prompt per agent (Context/Security/Code/Architecture/Testing/Judge)
├── services/       Orchestration + external clients (GitHub, Linear, sandbox, job repositories)
├── core/           config, logging, interfaces, errors, events, area/risk/label engines
└── models/         Pydantic domain models
tests/{unit,integration}/
skills/             Reference material used while building
```

