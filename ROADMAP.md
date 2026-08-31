# AIDA-MATE — Complete Roadmap

**AI-powered Pull Request Risk Analysis Agent for Linear + GitHub.**
As of **2026-08-13**: 9 build phases shipped, 709 tests passing (`ruff` clean), 3 real end-to-end verifications against live infrastructure.

> AIDA-MATE does not write code. No code generation, no commits, no branches, no PR creation, no merging. Analysis and classification only.

This document is the complete build log: what's shipped, what's running right now, and what's still open. See [README.md](README.md) for setup/usage and [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design.

---

## Part 1 — What we've built (in build order)

Every phase below is shipped and covered by the test suite. Phase 8 is written as ongoing because live verification against real infrastructure kept finding things the tests couldn't.

### Phase 0 — Scaffold — ✅ Done
Typed config, domain models, Protocol-based interfaces, the FastAPI composition root, health/readiness endpoints, and the webhook boundary — before any external integration existed.

### Phase 1 — Linear OAuth — ✅ Done
PKCE install flow, token exchange/refresh/revoke, and automatic actor discovery so AIDA-MATE learns its own Linear identity instead of needing it hardcoded.

### Phase 2 — Webhook intake & job lifecycle — ✅ Done
HMAC signature verification over the raw body, replay protection via delivery timestamp, a bounded worker queue, and three-stage deduplication (delivery → intake → content) so a redelivered webhook never costs a duplicate review.

### Phase 3 — GitHub integration — ✅ Done
PR metadata, file diffs, commits, archive download, label creation/application, and comment publishing — over the GitHub REST API, with a GitHub App path and a development-token fallback.

### Phase 4 — Deterministic engines — ✅ Done
Path-based area detection, rule-based risk scoring, and label derivation — pure Python, zero LLM involvement. This is the layer that makes every later AI addition structurally incapable of deciding the final risk level.

### Phase 5 — Sandbox + first review agent — ✅ Done
A single OpenAI Agents SDK reviewer with three read-only sandbox tools (`read_file`, `search_code`, `list_files`), plus Azure OpenAI / Azure AI Foundry client wiring.

> Docker Sandboxes turned out to be genuinely broken on the development host — Docker Desktop's engine needs a WSL2 update that can't be installed without admin rights. `LocalSandboxProvider` was built as an explicitly-labeled, non-isolated stopgap so live testing didn't have to wait on IT.

### Phase 6 — Review lifecycle & persistence — ✅ Done
SHA-scoped review identity, an enforced skip for a PR revision already reviewed, an explicit `POST /reviews/{id}/retry` endpoint, a SQLite-backed job store, and restart recovery for anything left mid-flight by a crash.

> Found and fixed live: the first SQLite schema **deleted a job's history** every time its idempotency key was reused after completion. Replaced the column-level unique constraint with a partial unique index scoped to non-terminal jobs only — completed reviews now keep every row, forever.

### Phase 7 — Multi-agent upgrade — ✅ Done
The single reviewer became a six-agent manager/specialist pipeline: a Context Agent, four concurrent specialists (Security, Code, Architecture, Testing), and a Judge that reconciles their output into the same one `ReviewAnalysis` schema the risk engine already trusted. Sequencing is plain Python throughout — never an LLM-controlled handoff.

> A specialist (or the Context Agent) failing is recorded, not silently dropped: `failed_specialists` forces human review and a **PARTIAL** heading on the published comment. All four specialists failing is the only path that stops the review before the Judge is even called.

### Phase 8 — Live verification — 🟡 Ongoing
Repeated end-to-end runs against a real Linear workspace, a real public GitHub repository, and a real Azure AI Foundry deployment — the thing that keeps finding what 709 unit tests can't.

> Bugs caught **only** by running the real thing: a misordered `--name` flag in the sandbox CLI call; Linear's "Delegate" action setting `delegateId` instead of `assigneeId`; delegation arriving at issue-creation time with no `updatedFrom` to inspect; the SQLite history bug above; and, most recently, a placeholder `UTILITY_MODEL` deployment name that didn't exist in Azure at all.

---

## Part 2 — Verified live runs

Real PRs on the public `karthixcarlo/sentinel-trading-bot` repository, real Azure OpenAI calls, real GitHub labels and comments, real Linear activity. Not a demo fixture.

| PR | Pipeline | Risk | Score | Findings | Tool calls |
|---|---|---|---|---|---|
| #4 — Security audit: auth-bypass IDOR chain | single-agent | 🟡 MEDIUM | 43 | 1 | — |
| #5 — Frontend cleanup, dead code removal | single-agent | 🟢 LOW | 5 | 0 | 0 |
| #6 — TOCTOU trade race, DB reliability | single-agent | 🔴 HIGH | 228 | several | 7 |
| #6 — same PR, same commit, re-run | 6-agent | 🔴 HIGH | 288 | 6 | 11 |

**#6's score moved from 228 to 288 under the new pipeline — not drift, a coverage gain.** Four independently-investigating specialists surfaced genuine `api` and `infrastructure` findings a single generalist pass had missed. The deterministic scoring rules did not change.

---

## Part 3 — Right now

**Nothing is mid-implementation.** The last completed unit of work was the live regression run above. The dev server and its ngrok tunnel are left running on request for ad hoc testing. Everything in Part 4 is an open thread awaiting a priority call — not something already underway.

---

## Part 4 — What's left

Ordered by the leverage each one buys, not by size.

### 1. GitHub commit webhook — Not started
Today a review only starts from a Linear event. A new commit on an already-reviewed PR sits unreviewed until someone touches Linear again. The persistence layer built in Phase 6 (repo + PR# lookup, restart recovery) makes this tractable now — the open question is trigger policy: every push, or only PRs AIDA-MATE has already reviewed once.

### 2. Live-verify a real specialist failure — Not started
The PARTIAL-comment path — a specialist timing out or erroring mid-review — is only exercised by mocked unit tests so far. No live run has actually forced one to fail.

### 3. Remaining scenario coverage on the 6-agent pipeline — Not started
PR #4 (security-heavy) and PR #5 (cheap, low-risk) have only been run through the earlier single-agent pipeline. Re-running them would confirm the cost-control story — that a trivial diff still needs zero or near-zero tool calls under four specialists, not four times the cost.

### 4. Provision the real GitHub App — Not started
Everything currently runs on a development personal access token. Production should move to the GitHub App credential path that already exists in config but has never been installed.

### 5. Verify Docker Sandbox mode on a working host — Blocked here
Confirmed broken only on this specific development machine (WSL2 kernel update needs admin rights this environment doesn't have). `local` mode remains the correct default until it's tried somewhere Docker Sandboxes can actually run.

### 6. Anthropic model provider — Not started
The provider interface already supports swapping in an `AnthropicAgentRunner` without touching GitHub, Linear, sandbox, or risk logic. It's simply never been built — the `anthropic` package isn't even installed yet.

### 7. Production-grade persistence — Not started
The SQLite job store and file-backed Linear token store are explicitly local-dev conveniences, opt-in and unencrypted at rest. A real deployment needs an encrypted database behind the same repository interfaces.

---

## Standing principles (still true today)

- **Risk score is 100% deterministic Python** — no LLM, not even the Judge, has ever had a field to write a verdict into.
- **AIDA-MATE never generates code, commits, branches, or PRs.** Analysis and classification only.
- **Every agent's tools are read-only.** No write, commit, push, or merge capability exists anywhere in the pipeline.

---

<sub>AIDA-MATE — AI-powered Pull Request Risk Analysis Agent · as of 2026-08-13</sub>
