# AIDA-MATE — Project Context for Claude

Read this first, before touching any code. It exists so a fresh session (or a
different Claude entirely) can pick this project up cold with minimal
back-and-forth. If you're Claude Code, this file loads automatically when the
working directory is `gitmate/` — no need to be handed it manually.

For depth beyond this summary: [README.md](README.md) (setup/usage),
[ARCHITECTURE.md](ARCHITECTURE.md) (full technical design + diagrams),
[ROADMAP.md](ROADMAP.md) (build history + backlog, more detail than §7 below).

---

## 1. What this is

**AIDA-MATE** is an AI-powered Pull Request Risk Analysis Agent. Trigger: a
user assigns/delegates a Linear issue to AIDA-MATE. Flow: Linear webhook →
resolve the linked GitHub PR → fetch it → sandboxed AI analysis → **deterministic
Python risk classification** (LOW/MEDIUM/HIGH) → GitHub labels + comment →
Linear comment/activity.

**Hard non-goal, stated explicitly because it's easy to drift toward:**
AIDA-MATE's *analysis and risk classification* never writes code — no code
generation, no commits, no branches, no PR creation, ever. Every agent tool in
this codebase is read-only, and no agent, LLM, or orchestration layer has (or
should ever gain) a write/commit/push/merge tool. The **one narrow,
explicitly-opt-in exception** is a separate, later action, not part of
analysis: when `AUTO_MERGE_ON_DONE_ENABLED=true` (off by default) and a human
moves a Linear issue to a completed-type state for an issue with a prior
`COMPLETED` AIDA-MATE review, AIDA-MATE may merge that review's linked PR —
immediately for LOW risk, or only after an explicit human "Yes, merge" click
on a confirmation page for MEDIUM/HIGH risk. This action is triggered only by
a human's Linear state change, never by the LLM or any agent tool, and never
re-classifies risk. See §1a.

## 1a. The one exception to "AIDA-MATE never merges" — gated, opt-in, human-triggered

`AUTO_MERGE_ON_DONE_ENABLED` (`app/core/config.py`, default `False`) gates a
wholly separate, later action from review/classification:

```
Linear issue -> completed-type state (human action)
  -> find prior COMPLETED review + its linked PR
    -> risk == LOW           -> merge immediately, no confirmation
    -> risk == MEDIUM/HIGH   -> Linear comment with a confirmation link
                                 -> human clicks "Yes, merge" -> merge
                                 -> "No" / no answer -> nothing happens
```

**Unchanged**: §2's rule that the LLM never sets risk, labels, or
`needs_human_review`. This action never re-runs analysis or re-classifies —
it only reads `risk` from an already-published `ReviewResult`
(`app/services/auto_merge_service.py`), and has no path back into the risk
engine.

**New**: `app/services/github_service.py`'s `merge_pull_request()` is the
first write tool that changes a PR's mergedness rather than its
labels/comments. It's called only from `app/services/auto_merge_service.py`,
triggered only from the Linear webhook's Done-transition path
(`app/api/linear_webhook.py`'s `_extract_issue_done_trigger`) — never from
`agents/orchestrator.py`, never from any agent tool, and never while the
sandbox/LLM stage is running. The confirmation dialog
(`app/api/merge_confirmation.py`) was originally the only HTML page in the
app — §1d's `app/api/scheduled_prompt_form.py` is now a second, both kept
deliberately separate from every JSON API router.

If you're asked to expand this — merge without the MEDIUM/HIGH confirmation,
or a trigger other than a human Linear state change — treat that as a change
to flag back to the user, the same way §2 asks for risk/label logic.

## 1b. The reverse direction — GitHub merge syncs Linear to Done

`GITHUB_MERGE_SYNC_ENABLED` (`app/core/config.py`, default `False`) is §1a's
mirror image, opposite direction:

```
GitHub PR merged into github_merge_sync_branch (default "main")
  -> find the COMPLETED review that links this PR to a Linear issue
    -> no such review -> no-op
    -> resolve the issue's team's Done-type workflow state
      -> none found -> no-op, logged
      -> found -> move the Linear issue to that state
```

**New**: `app/services/linear_service.py`'s `update_issue_state()` is the
first write to a Linear issue's *status* (previously only labels/comments
were written). Triggered only from `app/api/github_webhook.py` —
AIDA-MATE's first inbound GitHub webhook receiver — never from the review
pipeline, never from `agents/orchestrator.py`.

**Operational note**: this requires a webhook configured on the GitHub repo
side (Settings → Webhooks → Payload URL `{PUBLIC_BASE_URL}/webhooks/github`,
content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, "Pull
requests" events) — not something this app can configure for itself.

If you're asked to expand this — sync on something other than a merge into
the configured branch, or let it write anything besides the issue's state —
treat that as a change to flag back to the user, same as §1a.

## 1c. GitHub Issues & security vulnerabilities → Linear (create/update)

`GITHUB_ISSUE_SYNC_ENABLED` (`app/core/config.py`, default `False`) is a
third GitHub↔Linear behavior, distinct in kind from §1a/§1b: those move an
*existing* Linear issue's state or merge a PR; this one *creates* Linear
issues for GitHub objects that have no AIDA-MATE review behind them at all —
plus one narrow state sync of its own (closing), see below.

```
GitHub Issue / Code Scanning / Dependabot / Secret Scanning alert
  -> best-effort relationship lookup:
       issue        -> a PR (open or closed) mentioning "#<number>"
       security alert -> the PR for the alert's commit SHA, if it has one
  -> fingerprint = "github:{owner/repo}:{source_type}:{source_id}"
  -> existing SyncMapping for that fingerprint?
       yes -> update the Linear issue's title/description
       no  -> resolve LINEAR_SYNC_TEAM_KEY's team id -> ensure a Linear label
              exists ("GitHub Issue" for issues, "Security" shared across all
              three vulnerability sources) -> create a Linear issue tagged
              with it -> store the mapping
  -> (GitHub Issues only) state == "closed"?
       yes -> move the Linear issue to the team's completed-type state,
              immediately, same webhook delivery — reuses §1b's exact
              find_done_state_id/update_issue_state pair
       no  -> leave the Linear issue's status untouched
```

Scoped to plain GitHub Issues only — security alerts don't set this
(`close_when_closed` in `github_issue_sync_service.py`'s `_upsert` is opt-in
per call site). GitHub's alert states (`fixed`/`dismissed`/`open`) don't map
onto "closed" as cleanly as an Issue's does, and closing on those wasn't
asked for. Reopening a closed GitHub issue does NOT move the Linear issue
back out of Done — only closing is synced, matching what was actually
requested; only the content (title/description) keeps updating either way.

**The reverse direction holds too**: moving a synced Linear issue to a
completed-type state (a human dragging it to Done, or any other update that
lands it there) closes the linked GitHub Issue back, immediately — same
Linear webhook delivery that §1a's gated auto-merge trigger already detects
(`_extract_issue_done_trigger` in `app/api/linear_webhook.py`), dispatched
to `GitHubIssueSyncService.handle_linear_issue_done` alongside (not instead
of) `AutoMergeService.handle_issue_done`, gated independently by
`GITHUB_ISSUE_SYNC_ENABLED` rather than `AUTO_MERGE_ON_DONE_ENABLED`. Keyed
off `SyncMapping.find_by_linear_issue_id` (a GitHub-Issue-sourced mapping),
not a `ReviewJob` — a Linear issue with no such mapping, or one for a
security alert, is a normal no-op; most Linear Done transitions have nothing
to do with this sync. `GitHubService.close_issue()` is the second write that
changes a GitHub object's own state (the first was `merge_pull_request` in
§1a) — ordinary PATCH through the generic `_request` error mapping, unlike
`merge_pull_request`'s special-cased 405 handling, since closing has no
"expected common failure" status code the way merging does.

The two directions cannot loop: GitHub's `issues` webhook only fires on an
actual state transition, and Linear likely behaves the same way for
`issueUpdate` — closing an already-closed GitHub issue, or setting a Linear
issue to the completed state it's already in, is an idempotent no-op on both
sides rather than a re-triggering event. Confirmed live (see §8) rather than
assumed.

The label is attached only at creation, not on re-sync updates — a human is
unlikely to have removed it, and re-checking it on every update would add a
GraphQL round trip for no real benefit.

Label *creation* is best-effort, not required: live testing (§8) found Linear
can reject `issueLabelCreate` for the OAuth app actor with `FORBIDDEN` even
though `issueCreate` succeeds — a workspace permission restriction, not
something this code controls. `_upsert` in
`app/services/github_issue_sync_service.py` creates the issue untagged
rather than blocking the sync when that happens. A human pre-creating the
"GitHub Issue" / "Security" labels once in Linear's UI is enough —
`ensure_label_id`'s existing-label lookup then finds and reuses them on
every subsequent sync, no further code involvement needed.

**Explicitly never synced**, regardless of this flag: pull requests by
themselves, CI/Actions runs, checks, or any GitHub event that isn't one of
the four sources above — enforced in `app/api/github_webhook.py`'s dispatch,
which only recognizes `issues`/`code_scanning_alert`/`dependabot_alert`/
`secret_scanning_alert` for this path (`pull_request` stays §1b's alone).

**Bridges into §1d**: `app/services/default_schedule_service.py`'s
`DefaultRepoScheduleService.ensure_for_repository` creates one default
daily scheduled prompt (`DEFAULT_PROMPT`/`DEFAULT_RUN_AT_TIME="09:00"`/
`DEFAULT_TIMEZONE="Asia/Kolkata"`) for a repository if that repository has
no scheduled prompt at all yet — user-requested: "make sure this scheduler
thing appears in every repo linked with Linear," where "linked" was
confirmed to mean "has GitHub Issue sync active" (a `SyncMapping` exists).
Wired into `GitHubIssueSyncService._upsert`'s fresh-create branch only —
never on an update to an already-existing mapping, so a repo's 2nd, 3rd,
... synced Issue/alert doesn't re-check anything unnecessarily beyond the
service's own idempotent `list_all()` scan. It never touches a schedule a
human already created or customized for that repo (any existing schedule
counts, not just auto-created ones) — this only guarantees at-least-one,
the same "don't overwrite a human's customization" posture §1c's own label
resolution already has. Gated on two flags independently:
`GITHUB_ISSUE_SYNC_ENABLED` (for the trigger to exist at all) and
`SCHEDULED_PROMPTS_ENABLED` (for `default_schedule_service` to be
constructed rather than `None` in `main.py` — no point auto-creating
schedules the worker isn't running to execute). A one-time backfill was run
by hand for `karthixcarlo/sentinel-trading-bot`, the only repo already
linked when this shipped, via the existing `POST /scheduled-prompts` API
rather than a throwaway script — not an ongoing migration concern, matching
§1d's own `last_run_date` → `last_run_at` cutover note.

**New**: `app/services/linear_service.py`'s `create_issue()` — the first
*creation* of a Linear issue in this codebase; every other Linear write acts
on an issue that already exists. `ensure_label_id()` is the first Linear
*label* write too — `ensure_labels_exist`/`apply_labels` were declared on
`ILinearClient` from early on but never actually implemented until this.
`app/models/sync_mapping.py`'s
`SyncMapping` + `app/services/sync_mapping_repository.py` /
`sqlite_sync_mapping_repository.py` are a second dedup store, structurally
identical to `IReviewJobRepository` but tracking a completely different
relationship (GitHub object → Linear issue, not AIDA-MATE's own review
lifecycle) — sharing the same SQLite file as `review_jobs` when
`REVIEW_STORE_PATH` is set, but its own table.

**Honesty note**: the GitHub Issues path is now live-verified — a real test
issue synced correctly to Linear (content, repository, state, author, and
related-PR detection all confirmed against the actual API response) and
surfaced the label-permission finding above in the process. The three
security-alert sources remain *not* proven against live GitHub traffic —
this dev/test repo has Code Scanning, Dependabot, and Secret Scanning all
disabled. Field extraction in `app/api/github_webhook.py::_security_alert_event`
is built from GitHub's documented webhook schemas. If synced alert content
ever looks wrong, that function's `details` dict extraction is the first
place to check against a real payload — the equivalent of what live-testing
already caught and fixed once in §1b's `WORKFLOW_STATES_QUERY` (§8 below
documents that fix).

**Operational note**: requires subscribing the same GitHub webhook (see §1b)
to four more event types: "Issues", "Code scanning alerts", "Dependabot
alerts", "Secret scanning alerts" — same payload URL, same secret.

If you're asked to expand this — sync additional GitHub event types, change
what counts as a relationship, or let synced content include anything beyond
what §5 of the original spec listed — treat that as a change to flag back to
the user, same as §1a/§1b.

## 1d. Scheduled prompts — recurring, timezone-aware repo checks posted to Linear

`SCHEDULED_PROMPTS_ENABLED` (`app/core/config.py`, default `False`) is the
first **timer-driven** capability in AIDA-MATE — everything else in this
codebase reacts to an inbound Linear or GitHub webhook; this runs on a clock
instead:

```
A human stores a ScheduledPrompt (title, prompt text, "owner/repo", optional
branch, a frequency + its own required fields, IANA timezone, target Linear
issue) via the POST/GET/PATCH/DELETE /scheduled-prompts API or the web form
  -> ScheduledPromptWorker ticks every 60s, checks every stored schedule
     against its own frequency's due-check (`_is_due`, all judged from one
     `last_run_at` timestamp — see "Frequency" below)
    -> not due -> skip
    -> due -> claim the run instant (`mark_run` + save) BEFORE running, then:
         resolve branch (explicit, or the repo's current default branch)
           -> resolve its current commit sha
             -> download a repo archive, same as a PR review
               -> extract into a fresh sandbox
                 -> run the prompt through a single-agent LLM runner with the
                    same read-only list_files/read_file/search_code tools a
                    PR-review specialist gets
                   -> post the markdown output as a Linear comment on the
                      configured issue
        -> any failure at any step -> caught, logged, and reported to Linear
           as a short failure comment instead — never raises past the worker
```

**Frequency**: `ScheduledPrompt.frequency` is one of `once` / `hourly` /
`daily` / `weekly` / `monthly` (default `daily`), each with its own required
companion field(s) enforced by `ScheduledPromptCreate`'s
`model_validator(mode="after")` in `app/api/scheduled_prompts.py`:
`run_on_date` (ISO date) for `once`, `interval_hours` (1-23) for `hourly`,
`day_of_week` (0=Monday..6=Sunday) for `weekly`, `day_of_month` (1-31,
clamped to the month's actual last day when it's shorter — see
`_effective_day_of_month`) for `monthly`; `run_at_time` (`HH:MM`) is required
for every frequency except `hourly`, which fires on elapsed time instead of
a clock match. All five are judged in `app/workers/scheduled_prompt_worker.py`'s
`_is_due` from a **single** `last_run_at: datetime | None` (UTC) field —
deliberately not one tracking field per frequency: `hourly` compares elapsed
time against it directly, and `once`/`daily`/`weekly`/`monthly` all share one
"clock matches, and `last_run_at` isn't already in the current period"
shape, localized into the schedule's own timezone at check time. A fired
`once` schedule is deleted outright in the same `tick()` pass, before
`service.run()` is even called — not just disabled: user-requested, so a
one-shot schedule disappears from the dashboard the moment it's done rather
than lingering as a disabled row. Deleting first (rather than after the run
completes) doubles as the claim-before-execute step a recurring schedule
gets from `mark_run()` + `save()` — a row that no longer exists can't be
picked up by a later tick's `list_all()` either, so the race-safety
property is preserved via absence instead of a flag. Deletion happens
regardless of whether the run itself succeeds or fails, matching the
worker's existing "sync the dashboard either way" behavior below — the
schedule already "executed" the moment it fired, independent of its
outcome. `ScheduledPromptUpdate` (`PATCH`)
accepts the same fields individually validated but deliberately does **not**
cross-validate frequency-consistency — a known, accepted gap matching that
endpoint's existing lenient partial-update semantics, not something to "fix"
reflexively if noticed later. This replaced an earlier `last_run_date: str`
(bare ISO date) field — a one-time cutover, not an ongoing migration
concern: any schedule persisted before this shipped simply reads
`last_run_at` as `None` and becomes eligible to fire again next time its own
moment comes around, same as a freshly created one would.

**Distinct from §1a/§1b/§1c** in one important way: those three are all
GitHub↔Linear *sync* reactions to an event that already happened elsewhere. A
scheduled prompt has no triggering event at all — `ScheduledPromptWorker`
(`app/workers/scheduled_prompt_worker.py`) is the first `asyncio.Task` in
this codebase whose loop is `sleep` + clock-check rather than "wait on a
queue". It mirrors `ReviewQueue`'s `start()`/`stop()` task lifecycle
(`app/workers/review_worker.py`) but ticks on a timer, not a queue.

**Unchanged**: §2's rule. A scheduled prompt has no risk classification, no
labels, and no `needs_human_review` at all — its single agent
(`app/agents/prompt_runner.py`'s `ScheduledPromptRunner`) has no
`output_type` and returns freeform markdown for a human to read, not
anything any deterministic engine parses. It never touches
`risk_engine.py`/`label_engine.py`, and has no path into either.

**Comment format, deliberately terse**: `SCHEDULED_PROMPT_SYSTEM_PROMPT`
(`app/prompts/scheduled_prompt.py`) asks for one line per issue — a brief
description, then exactly where it occurs (file path, line number when
available) — no headings, no summary paragraph, no recommendations section.
`app/services/scheduled_prompt_service.py`'s `_render` wraps that output
with only the actual prompt text and the repository (`**Prompt:** "..." —
`owner/repo`\n\n{output}`) so it's unambiguous *which* schedule produced a
given comment when several post to the same dashboard issue — no separate
timestamp line, since Linear already shows each comment's own posted-at
time natively. If asked to expand the report format (findings grouped by
severity, a summary paragraph, etc.), treat that as a change to flag back
to the user — conciseness here was an explicit, deliberate request, not a
placeholder to fill in later.

**New**: the first genuinely new LLM runner alongside `review_agent.py`'s
six-agent pipeline — one `Agent`, no Context/specialists/Judge, since there
is no PR diff to synthesize multiple perspectives on, just one described
task. `app/services/scheduled_prompt_service.py`'s `ScheduledPromptService`
mirrors `ReviewOrchestrator._run_agent_analysis`'s sandbox-provisioning
sequence (download archive -> upload -> extract -> analyze -> destroy) but
simpler: no `ReviewJob`, no status transitions. `GitHubService` gained two
read methods no PR-bound call site needed before:
`get_default_branch`/`get_commit_sha` — resolving "the latest code on a
branch" outside a PR's own `head_sha`. `ScheduledPrompt` /
`IScheduledPromptRepository` (`InMemoryScheduledPromptRepository` +
`SqliteScheduledPromptRepository`, sharing `REVIEW_STORE_PATH`'s SQLite file
with its own table when one is configured) are structurally identical to
`SyncMapping`/`ISyncMappingRepository` (§1c) — same in-memory/SQLite pair
pattern, tracking a completely different kind of record.

The worker's "claim the run instant before running" ordering
(`ScheduledPromptWorker.tick` in `app/workers/scheduled_prompt_worker.py`) is
what makes a slow run safe *without* a per-entry `asyncio.Lock` the way §8's
`AutoMergeService`/`LinearAuthService` races needed: ticks are processed
sequentially inside one `tick()` call, and the loop's next `sleep` doesn't
start until `tick()` returns, so there is no concurrent second tick to race
against in the first place — solved architecturally rather than with a lock.

**Operational note**: named timezone support (`zoneinfo.ZoneInfo`) needs the
`tzdata` package installed — Windows has no system IANA timezone database
for `zoneinfo` to fall back to, unlike Linux/macOS. It's a normal
`pyproject.toml` dependency now (pure-Python, no compiled code), so a fresh
`pip install -e .` picks it up; only worth remembering if you're debugging a
"No time zone found with key ..." error on an older checkout.

If you're asked to expand this — additional output destinations besides a
Linear comment, sub-minute scheduling resolution, or letting the scheduled
agent's output feed anything risk/label-related — treat that as a change to
flag back to the user, same as §1a/§1b/§1c.

**Dashboard**: Linear has no custom-widget/dashboard API, so "see the
schedules visually in Linear" (a real user request) means the only Linear
issue in this codebase that AIDA-MATE keeps continuously in sync as a
*document* rather than posting one-off comments to:

```
A schedule is created / updated / deleted via the API
  -> ScheduledPromptDashboardService.sync(organization_id)
       -> no dashboard issue on record for this org yet?
            -> resolve LINEAR_SYNC_TEAM_KEY's team id -> create one
               ("AIDA-MATE Scheduled Prompts"), store the mapping
          -> dashboard issue already on record -> update its description
       -> description = a markdown table of every schedule for this org
          (title, repository, HH:MM + timezone, enabled, last run) —
          Linear renders it natively, so this is as "visual" as a plain
          issue description gets
A worker tick fires a due schedule (success OR failure)
  -> same ScheduledPromptDashboardService.sync call, so "last run" stays current
```

**Reuses `LINEAR_SYNC_TEAM_KEY`** — the same setting §1c's GitHub Issue sync
already uses — rather than a new dedicated setting; confirmed with the user
as the simpler choice over adding one, since this dev environment already
has it set to `GIT`, as the single team that new *schedules* are created
against (see below). **The dashboard's read-only visibility, however, fans
out to every Team in the workspace**, not just that one configured team —
a later, separate request ("make sure this scheduler thing appears as an
issue in each teamspace"), confirmed to mean every Linear *Team* (the
concept `LinearService.list_teams`/`TEAMS_QUERY` already models), not
Linear's distinct "Teamspace" grouping feature. `sync()` now loops
`list_teams()` and pushes the *identical* organization-wide description to
each team's own dashboard issue via `_sync_one_team`, wrapped in its own
`try/except LinearError` so one team's failure (e.g. no create permission
there) doesn't stop the rest of the workspace from getting an up-to-date
dashboard. `ensure()` — the "where does a *new* schedule's result actually
post to" path used by the web form and `DefaultRepoScheduleService` — stays
pinned to the one configured `LINEAR_SYNC_TEAM_KEY` team via
`find_team_id_by_key`, unaffected by the fan-out; only the dashboard's
visibility is broadcast, not where the work happens. §1c's GitHub
Issue/security-alert sync (and `DefaultRepoScheduleService`'s auto-created
schedules) likewise still target that one configured team only — fanning
those out too would need a repo-to-team mapping this codebase has no
concept of, and wasn't asked for; flag back if that's wanted later.
**Cost note**: every dashboard sync now makes roughly N times the Linear
GraphQL calls it used to, where N = team count in the workspace — worth
watching in a large company workspace with many teams, though not a
blocker for what was asked.

**A newly created Linear team picks up its own dashboard issue
automatically, without a human touching a schedule first.** `sync()` only
ever runs today when a schedule is created/updated/deleted, or when the
worker fires one — so a team created between those events would otherwise
sit with no dashboard until something happened to trigger a sync. Linear
has no webhook event for team creation this app can react to (confirmed by
checking `skills/` — no such resource type is documented there), so this
is solved by polling rather than a push: `ScheduledPromptWorker.tick()`
(`app/workers/scheduled_prompt_worker.py`) now also calls
`_maybe_resync_dashboards()` on every tick, which re-syncs every distinct
`organization_id` found across all stored schedules — but only once every
`DEFAULT_DASHBOARD_RESYNC_INTERVAL_S` (600s / 10 minutes), tracked via a
single `_last_dashboard_resync_at` timestamp, not every 60s tick — a full
resync already costs one Linear call per team in the organization (the
cost note above), so doing it every tick would multiply that for no
benefit when nothing changed. A schedule's own due-fire sync (existing
behavior, unchanged) is tracked per-tick in an `already_synced` set and
excluded from the same tick's periodic resync pass, so a normal tick where
a schedule happens to fire never double-syncs that organization. Worst
case, a new team's dashboard issue appears within 10 minutes of the team
being created — confirmed live: creating a second real team in the dev
workspace and then manually forcing a sync (`PATCH` on an existing
schedule) produced a fresh dashboard issue on that team immediately;
`_maybe_resync_dashboards` generalizes that same `sync()` call to run
automatically on a timer instead of requiring the manual trigger.

One dashboard issue **per `(organization_id, team_id)` pair**
(`ScheduledPromptDashboard`'s natural key — previously `organization_id`
alone, before the fan-out), never deleted or archived — `ILinearClient` has
no delete capability anywhere in this codebase, so an empty schedule list
just renders an empty-state message instead, on every team's copy. The
underlying SQLite table's primary key changed shape for this
(`organization_id` alone -> composite `(organization_id, team_id)`), which
a plain `ALTER TABLE ADD COLUMN` can't express — the old single-column
primary key would still reject a second row per organization. One-time
cutover: `SqliteScheduledPromptDashboardRepository.__init__` detects the
pre-fan-out schema (no `team_id` column) and renames that table to
`scheduled_prompt_dashboards_legacy_pre_teams` rather than dropping it, the
same "old data preserved, not migrated in place" posture as every other
by-hand schema change in this codebase — each organization simply gets a
fresh per-team dashboard row created on its next sync. No Linear label on
any dashboard issue, unlike §1c's "GitHub Issue"/"Security" labels — each
one is found via its own persisted `ScheduledPromptDashboard` mapping
(`app/services/scheduled_prompt_dashboard_repository.py` /
`sqlite_scheduled_prompt_dashboard_repository.py`, the same in-memory/SQLite
pair shape every other store here uses), never by searching Linear, so a
label would buy nothing. `render_dashboard_description`
(`app/core/scheduled_prompt_dashboard.py`) is pure formatting, mirroring
`core/report.py`'s style — its one defensive detail is escaping a
schedule's `title` (the only fully-freeform field shown) so a literal `|`
or newline in it can't corrupt the table; every other column is already
constrained by `app/api/scheduled_prompts.py`'s validators.
`ScheduledPromptDashboardService.sync` never raises — a `LinearError` is
caught inside the service itself, not by either call site (the CRUD API
routes or the worker's `tick()`), the same lesson §8 already drew from
`GitHubMergeSyncService`. The description also always opens with a plain-text
link to the web form (see below) that creates new schedules — built from
`settings.public_base_url` and rendered fresh on every `sync()` call
(`ScheduledPromptDashboardService.__init__`'s `base_url` argument, same
naming/shape as `AutoMergeService`'s own `base_url` for its merge-confirmation
links), so it can never go stale even if `PUBLIC_BASE_URL` changes later —
never written once at creation time and left to rot.

If you're asked to expand this — additional dashboard columns that need a
live Linear read per schedule (e.g. linking to each schedule's own target
issue by its human identifier, which isn't stored on `ScheduledPrompt`
today), a dashboard scoped to something other than one-per-organization, or
letting dashboard sync fail loudly instead of logging and swallowing —
treat that as a change to flag back to the user, same as the rest of §1d.

**Web form**: `app/api/scheduled_prompt_form.py` (`GET`/`POST
/scheduled-prompts/new`) is a human-facing entry point into the exact same
creation path the JSON API uses — it constructs a `ScheduledPromptCreate`
and calls `scheduled_prompts.py`'s `create_scheduled_prompt` directly as a
plain function rather than reimplementing organization resolution,
validation, or dashboard sync. Three deliberate simplifications from the
JSON API, all explicit user choices rather than a technical limit: the form
has no timezone selector (every prompt created through it runs on
`Asia/Kolkata`/IST — the JSON API still accepts any IANA timezone), no
target-issue field (every submission's result posts to the organization's
dashboard issue itself, via the new `ScheduledPromptDashboardService.ensure`
method, rather than a separately-chosen issue — config and results both
live in one place), and the repository field takes a plain GitHub URL
(`_parse_github_repository`, tolerant of `www.`, missing scheme, a trailing
`.git`/slash, or extra path/query like `/tree/main`) rather than the
`owner/repo` slug `ScheduledPrompt.repository` actually stores — pasting the
address-bar URL is friendlier than reformatting it by hand, and a bare
`owner/repo` typed without a link is deliberately rejected now, not
silently accepted. `title` isn't a form field either — it's derived from
the prompt text (whitespace collapsed, otherwise verbatim — not truncated,
so the dashboard always shows the complete title rather than a cut-off
fragment) since a fourth field wasn't asked for; edit it afterward via
`PATCH /scheduled-prompts/{id}` if a different title is wanted. Registered
in `main.py` **before**
`scheduled_prompts.router`: its literal `/scheduled-prompts/new` path must
be matched before that router's `/scheduled-prompts/{scheduled_id}` pattern
would otherwise swallow "new" as if it were an id.

The same repository-link field also accepts a **pull request** URL
(`.../pull/123` or `.../pulls/123`), parsed by a second, narrower regex
(`_parse_pr_number`) run alongside `_parse_github_repository` on the same
raw input — the latter still just extracts `owner/repo` from either URL
shape unchanged. When a PR number is found, `ScheduledPrompt.pr_number` is
set and always takes precedence over `branch`/the repo's default branch: the
run downloads that PR's head commit
(`GitHubService.get_pull_request_head_sha`, deliberately lighter than
`get_pull_request` since a scheduled prompt has no diff to synthesize, only
a snapshot to explore) rather than the branch snapshot every other schedule
gets. This fixed a real gap: before this, pasting a PR link into the field
silently fell through to studying the default branch instead — the old
regex's trailing "ignore anything after owner/repo" group swallowed
`/pull/123` with no error and no indication the PR part was dropped. The
dashboard's Repository column and the Linear result comment's `**Prompt:**`
header both say "PR #N" when a schedule targets one, so it's never
ambiguous what was actually studied. There is still no way to pick a
specific *non-default branch* from the web form itself (only via the JSON
API's `branch` field) — confirmed with the user as intentionally out of
scope for this change, not an oversight.

The frequency picker (Once/Daily/Weekly/Hourly/Monthly) and its five
companion fields all reuse `ScheduledPromptCreate`'s own cross-field
`model_validator` for authoritative validation — the form's inline
`<script>` (`aidaMateUpdateFrequencyFields`) only shows/hides the relevant
block client-side for a cleaner UI, and is never trusted on its own. That
matters concretely: a browser still submits a `display:none`-hidden field's
(blank) value unless it's `disabled`, so `submit_scheduled_prompt_form`
accepts all five as plain optional strings rather than typed
`int | None = Form(...)` parameters — a typed parameter would 422 on the
blank string FastAPI/Starlette still receives from whichever fields the
chosen frequency doesn't use. `_to_int` converts by hand instead (blank ->
`None`, otherwise `int(...)`), so a genuinely garbled value reports a clear
form error instead of a raw 422.

**Deleting from the dashboard**: every row in the dashboard table (§ above)
ends with a `[Delete](.../scheduled-prompts/{id}/delete)` link, built fresh
from `settings.public_base_url` the same way the "create" link is.
`app/api/scheduled_prompt_form.py`'s `GET /scheduled-prompts/{id}/delete`
shows a confirmation page with a real button — never deletes on the GET
itself, the same GET-shows/POST-acts split `merge_confirmation.py` already
uses, so a link preview or crawler fetching the URL can't trigger a real
deletion. The POST handler reuses `scheduled_prompts.py`'s
`delete_scheduled_prompt` directly (same reuse pattern as the create form),
which already re-syncs the dashboard afterward — no separate sync call
needed in the form handler.

An earlier, more ambitious design was considered and rejected during
planning: typing the schedule as a **comment on the dashboard issue in
Linear itself**, parsed via a new Comment webhook path. Explicitly not
built — it would have needed a new webhook event subscription, a typed
format a human has to get exactly right with unclear error feedback, actor-
id loop-prevention (so AIDA-MATE's own reply comments don't get re-parsed
as new submissions), and a way to resolve a human-typed issue identifier
like "GIT-16" to Linear's internal id (nothing in this codebase does that
today). The web form gets the same practical outcome — typing a prompt
somewhere and having it show up scheduled — without any of that. If asked
to build the comment-driven version after all, treat it as new work, not an
extension of the form; the two are different entry points with different
failure modes, not a smaller/larger version of the same thing.

## 1e. A delete link on every Linear comment AIDA-MATE posts

Every comment `LinearService.add_comment` posts (`app/services/
linear_service.py`) — review outcomes, scheduled-prompt results/failures,
auto-merge notices/requests, review failures — carries an appended
"[Delete this comment](...)" link. Scoped to **comments only**, not Agent
Activities (`emit_agent_activity` — a different Linear object, no known
delete mutation, reads as a live status log rather than something a human
deletes); confirmed with the user. **Always on, no settings flag** —
narrower blast radius than the already-unconditional merge-confirmation
link (CLAUDE.md §1a), since deleting a comment can't merge code, change
issue state, or touch GitHub; confirmed with the user as the one write
capability in this codebase that didn't need an opt-in gate.

Injected in exactly one place — `add_comment` itself, not any of its 5 call
sites — matching §2's "enforce structurally, not by per-call-site
discipline" philosophy at a much lower-stakes scale: a future 6th call site
gets the delete link for free, with nothing to remember to wire up.
`add_comment` now takes an optional `posted_comment_repository`/`base_url`
pair at `LinearService` construction (both `None` by default, so every
existing test/fake construction is unaffected); when both are set — always
true in production, per `main.py` — it mints a `uuid4()` token, appends a
link built from it to the comment body, reads the newly created comment's
own id back from `COMMENT_CREATE_MUTATION`'s `comment { id url }` response
(previously discarded; this is the first caller that needed it), and
persists `{token -> linear_comment_id}` via the new `PostedComment` model
(`app/models/posted_comment.py`) / `IPostedCommentRepository`
(`InMemoryPostedCommentRepository` + `SqlitePostedCommentRepository`,
sharing `REVIEW_STORE_PATH`'s file when configured, own table) — the token,
not the real comment id, is what's public, same "unguessable bearer token
in a URL" pattern as `review_id` in the merge-confirmation link. A
repository-save failure is caught and logged, never allowed to look like
the comment post itself failed — the comment already succeeded by that
point.

`app/api/comment_deletion.py` is a third HTML page in the app (after
merge_confirmation.py and scheduled_prompt_form.py), same GET-shows/
POST-acts shape and no-referrer-policy reasoning as merge_confirmation.py:
`GET /comments/{token}/delete` renders a confirm dialog (or a "nothing
here" page for an unknown/already-used token); `POST /comments/{token}/delete`
calls the new `LinearService.delete_comment` (`COMMENT_DELETE_MUTATION`,
`$id: String!` matching `ISSUE_UPDATE_MUTATION`'s top-level-entity-id
convention — **confirmed live via introspection before trusting**, the
same discipline the `$teamId: String!` bug in §8 established) and, on
success, removes the `PostedComment` row. A `LinearError` from the delete
call is caught and shown as a plain error page rather than a 500, and
deliberately leaves the row in place so the same link can be retried.

If you're asked to expand this — delete links on Agent Activities too, or
gating it behind a new flag after all — treat that as a change to flag
back to the user, same as §1a-§1d.

## 2. The one rule that must never break

```
AI Agent(s)  →  structured findings  →  Python Risk Engine  →  LOW / MEDIUM / HIGH
               (judgment)                (deterministic policy)
```

The LLM is **never** asked for the final risk level, the labels, or the
human-review flag — not even implicitly via a "suggested risk" field. This is
enforced at the *schema* level, not by instruction: `ReviewAnalysis` (the
model's output type) has no `risk` field, so an LLM cannot supply one even if
it tries. `core/risk_engine.py` and `core/label_engine.py` are the only code
that ever sets risk/labels, and they are pure Python, unit-testable without
mocks.

A second, less obvious half of the same guarantee: only a `Finding.category`
— never a model's own top-level `ReviewAnalysis.areas` claim — feeds the
score (`app/agents/orchestrator.py`: `effective_areas = {c.area for c in
assessment.breakdown}`). An unsubstantiated area claim moving the score would
be the same "LLM controls risk" hole in a different shape.

Anti-gaming property worth knowing before touching the risk engine: findings
are unioned by **area**, not counted individually
(`effective_areas = set(areas) | {finding.category for finding in findings}`
in `assess_risk()`). A chattier agent — or four specialists instead of one —
cannot inflate the score by producing more findings about the same area; only
a genuinely new area moves it. This is what let the multi-agent upgrade (§7)
ship without reopening the "LLM controls risk" question.

**If you're asked to change risk/label logic, treat any request that would
let an LLM's output directly set risk/labels/needs-human-review as something
to flag back to the user, not silently implement.**

## 3. Stack & requirements

- Python 3.12+, FastAPI, Pydantic v2 / pydantic-settings, httpx, `openai-agents`
  SDK (import name `agents`) for the review pipeline.
- `pytest` + `pytest-asyncio` (auto mode) + `respx` for HTTP mocking; `ruff`
  for lint (`select = ["E","F","I","UP","B","SIM"]`, line-length 110).
- No database driver — persistence (when enabled) uses stdlib `sqlite3` via
  `asyncio.to_thread`, deliberately avoiding a new async-driver dependency.
- **Stale leftover to ignore**: `pyproject.toml` still lists a
  `sandbox = ["daytona>=0.20"]` optional extra from an early design that was
  abandoned. The actual sandbox implementations are the `docker sandbox` CLI
  (`services/sandbox_service.py`) and a pure-stdlib local stopgap
  (`services/local_sandbox_service.py`) — Daytona is not used anywhere and
  the extra is safe to ignore or remove if you're doing dependency cleanup.

## 4. Project structure (one line each)

```
app/
├── main.py                        FastAPI app + composition root — the ONLY place
│                                   that knows which concrete adapter backs which
│                                   interface (DIP boundary)
├── api/
│   ├── health.py                  liveness/readiness + capability flags
│   ├── linear_webhook.py          HMAC verify, replay-reject, filter, dedupe, enqueue
│   ├── linear_auth.py             OAuth install/callback/status
│   ├── reviews.py                 GET /reviews, GET /reviews/{id}, POST /{id}/retry
│   ├── merge_confirmation.py      GET/POST /reviews/{id}/merge-confirm — the only
│   │                               HTML page in the app (§1a gated auto-merge)
│   └── github_webhook.py          HMAC(sha256=) verify — AIDA-MATE's first INBOUND
│                                   GitHub webhook, one endpoint dispatching
│                                   pull_request (§1b) and issues/code_scanning_alert/
│                                   dependabot_alert/secret_scanning_alert (§1c)
├── agents/
│   ├── orchestrator.py            plain-Python pipeline: resolve→fetch→sandbox→
│   │                               analyze→classify→publish. NOT an LLM loop.
│   └── review_agent.py            the ONLY file importing `agents` (OpenAI Agents
│                                   SDK). Builds/runs the 6-agent pipeline (§7).
├── tools/sandbox_tools.py         list_files / read_file / search_code — the agents'
│                                   only tools, all read-only, shell-escaped
├── prompts/review_prompt.py       one system prompt per agent (6 total) + a shared
│                                   prompt-injection-defense preamble
├── services/
│   ├── review_service.py          intake: ReviewTrigger → persisted ReviewJob
│   ├── linear_service.py          Linear GraphQL client — issue read, comments,
│   │                               labels, agent activities, workflow-state
│   │                               lookup + issueUpdate (§1b), team lookup +
│   │                               issueCreate (§1c)
│   ├── linear_auth_service.py     OAuth (PKCE) token exchange/refresh/revoke
│   ├── github_service.py          GitHub REST: PR/files/diff/commits/archive/labels/
│   │                               comments/merge
│   ├── sandbox_service.py         `docker sandbox` CLI adapter
│   ├── local_sandbox_service.py   Docker-free stopgap — host temp dir, stdlib only
│   ├── pr_resolver.py             finds the linked PR: attachment→branch→title
│   ├── job_repository.py          IReviewJobRepository + in-memory impl
│   ├── sqlite_job_repository.py   durable impl, partial-unique-index dedup
│   ├── auto_merge_service.py      gated merge-on-Done (§1a): looks up a COMPLETED
│   │                               review, merges (LOW) or requests human
│   │                               confirmation via Linear comment (MEDIUM/HIGH)
│   ├── github_merge_sync_service.py  §1a's mirror (§1b): GitHub merge → moves
│   │                               the linked Linear issue to a Done-type state
│   ├── github_issue_sync_service.py  GitHub Issues/vulnerabilities → creates
│   │                               or updates a Linear issue (§1c)
│   ├── sync_mapping_repository.py,
│   │   sqlite_sync_mapping_repository.py  ISyncMappingRepository + both impls —
│   │                               the §1c dedup store, same shape as job_repository.py
│   └── token_store.py             Linear OAuth token storage (memory / file)
├── core/
│   ├── config.py                  typed Settings, fail-fast validation
│   ├── interfaces.py              every Protocol (the DIP contracts)
│   ├── area_detector.py           regex path-matching → Area set (deterministic)
│   ├── risk_engine.py             areas+findings → score → LOW/MEDIUM/HIGH (§2)
│   ├── label_engine.py            risk+areas → GitHub label set
│   ├── report.py                  renders the GitHub/Linear comment text (pure)
│   ├── logging.py                 structured JSON logs, secret redaction
│   ├── errors.py                  domain exception hierarchy
│   └── events.py                  lifecycle event name constants
├── models/
│   ├── common.py                  RiskLevel, Severity, Area, MergeStatus (§1a) — the
│   │                               shared taxonomy
│   ├── review.py                  ReviewAnalysis, PRContextAnalysis, AgentRunOutcome,
│   │                               RiskAssessment, ReviewResult, ReviewJob
│   ├── github.py, linear.py       external API shapes; github.py also has
│   │                               GitHubIssueEvent/SecurityAlertEvent (§1c)
│   └── sync_mapping.py            SyncMapping — the §1c dedup record
└── workers/review_worker.py       bounded queue + worker pool, guarantees terminal
                                    state and a non-raising run() loop
tests/{unit,integration}/          868 tests, offline, no network/Docker required
skills/                            reference material (Linear/GitHub webhooks, SDK)
```

## 5. Run & test

```bash
cd gitmate
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell — this dev host is Windows
pip install -e ".[dev,openai]"
copy .env.example .env              # then fill in real values — see §6
python -u -m uvicorn app.main:app --reload
```

```bash
pytest                              # 709 tests, offline, ~30s
ruff check app tests
```

`-u` / `PYTHONUNBUFFERED=1` matters if stdout is redirected to a file — Python
fully buffers stdout when it isn't a real TTY.

## 6. Environment specifics on THIS dev machine

- **Windows, PowerShell — not bash.** No `sh`/`grep`/POSIX `head` on PATH.
  `LocalSandbox.exec()` reimplements `find`/`grep`/`tar` semantics in pure
  Python for this reason; don't add code that shells out to POSIX utilities.
- **No `git` installed**, and no admin rights to install it. PR content is
  fetched entirely via GitHub's REST/archive API — never assume a local
  clone is available.
- **Docker Desktop's general engine now works here (re-verified 2026-08-28)
  — the earlier "confirmed broken" note above is stale, corrected rather
  than trusted.** Originally `docker sandbox create` failed because the
  WSL2 backend needed an update this machine had no admin rights to apply;
  at the time that was assumed to mean Docker Desktop itself was unusable
  here. Re-tested live while dockerizing the app (`docker version`/`docker
  info`/`docker compose build`/`docker compose up -d` against the repo's
  existing `Dockerfile`/`docker-compose.yml`): the engine starts cleanly
  (Docker Desktop 4.86.0, `docker-desktop` WSL distro comes up fine),
  `docker compose up -d` built and ran the whole app in a container,
  `/health`/`/ready` both reported healthy, `MANAGEMENT_API_KEY` auth
  worked (401 without it, 200 with it) — confirmed via the container's own
  access log, not just an HTTP 200, since a leftover host `uvicorn`
  process from an earlier restart this session was also still bound to
  `127.0.0.1:8000` at the same time and had to be ruled out (and then
  killed) before trusting the result — see the port-conflict lesson in the
  entry just below. Whatever the earlier WSL2 issue was, it's no longer
  reproducing; Docker Desktop likely self-updated since. **The narrower
  claim is still true, just for a different reason now**: `docker sandbox`
  (the specific CLI `app/services/sandbox_service.py`'s `SbxSandbox`
  backend shells out to) now prints `"docker sandbox" is deprecated and
  has been removed. Please migrate to Docker Sandboxes:
  https://www.docker.com/products/docker-sandboxes` — Docker itself
  discontinued that subcommand, unrelated to this machine's earlier WSL2
  problem. `SANDBOX_MODE=local` therefore remains the correct default here,
  but now because the `docker sandbox` CLI doesn't exist anymore on any
  host with this Docker version, not because the engine can't run
  containers — general `docker`/`docker compose` usage (building and
  running the app itself, as opposed to `SANDBOX_MODE=docker`'s specific
  CLI dependency) is unaffected and confirmed working. If `SbxSandbox` is
  ever revisited, it needs to target whatever the new "Docker Sandboxes"
  product's CLI/API actually is — this is a real, separate migration, not
  just a re-test.
- **A stale host `uvicorn` process surviving across restarts can silently
  intercept `localhost` traffic meant for a Docker container on the same
  port.** While verifying the dockerized app above, `Get-NetTCPConnection
  -LocalPort 8000` showed three simultaneous listeners: a leftover host
  `python.exe`/`uvicorn` process bound to `127.0.0.1:8000` specifically
  (from an earlier restart this session, never cleaned up), Docker's own
  port-proxy (`com.docker.backend`) bound to the IPv4/IPv6 wildcard, and
  WSL's `wslrelay.exe` on `::1`. All three coexisted without Windows
  rejecting either bind. `curl http://localhost:8000/...` could plausibly
  have hit either the stale host process or the container with no
  visible difference in a single response — verification only became
  trustworthy by cross-checking each curl request actually appears in
  `docker logs aida-mate`'s own access log (it did), then killing the
  stale host process and re-confirming `/health`/`/ready` still worked
  through the container alone. The general lesson already in this file
  ("duplicate/stale server processes have recurred across restarts") is
  broader than previously written: it now also applies across a host
  process and a Docker container sharing the same port, not just two host
  processes — check `Get-NetTCPConnection -LocalPort <port>` for more than
  one owner before trusting *any* `localhost` response on this machine,
  not just before restarting the bare `uvicorn` server.
- **Azure OpenAI / Azure AI Foundry**, not plain OpenAI. `OPENAI_BASE_URL`
  ends in `/openai/v1` (the newer "v1" surface) — no `OPENAI_API_VERSION`
  needed for this specific endpoint shape; setting one would be wrong here.
  `REVIEW_MODEL` and `UTILITY_MODEL` must be **exact Azure deployment
  names** that actually exist in the resource, not generic model strings —
  a placeholder value fails fast with `DeploymentNotFound` (this exact bug
  was hit and fixed live — see §8).
- Secrets live in `.env` (gitignored). **Never print secret values** in logs
  or terminal output — variable names and presence booleans only (e.g.
  `openai_api_key_set: True`), even for values that feel low-stakes.
- Live testing setup (when resuming live verification): local server on
  port 8000 + an ngrok free-tier static tunnel forwarding Linear webhooks to
  it. The tunnel process can die independently of the server and needs
  restarting separately; the static domain persists across restarts.
- Test repo used for live verification: the public
  `karthixcarlo/sentinel-trading-bot` on GitHub, linked to issues in a real
  Linear workspace. PRs #4/#5/#6 there are known scenarios — see ROADMAP.md
  Part 2 for what each one covers.

## 7. Current architecture snapshot (multi-agent, shipped)

The review pipeline (`review_agent.py`) is a **6-agent manager/specialist
pipeline**, not one agent, behind an unchanged `IReviewAgentRunner.analyze()`
seam:

1. **Context Agent** — no tools, cheap model, produces `PRContextAnalysis`
   (orientation hint only, never a restriction on specialist tool access).
   Failure here is non-fatal.
2. **Four specialists run concurrently** via `asyncio.gather` (never
   sequentially — they have no data dependency on each other): Security,
   Code, Architecture, Testing. Each has its own `SandboxToolContext` (own
   `agent_name`, own `tool_calls_count`) and its own
   `SPECIALIST_TIMEOUT_SECONDS` budget. A specialist failing is recorded in
   `failed_specialists`, not fatal, as long as ≥1 survives. All 4 failing
   raises before the Judge is ever called.
3. **Judge** — no tools, reconciles the survivors' `ReviewAnalysis` outputs
   (deduplicates, merges) into the one `ReviewAnalysis` that ever reaches
   `assess_risk()`. Judge failure is always fatal (no partial result
   possible without synthesis). The Judge has zero authority over final
   risk — see §2.

Sequencing throughout is plain Python (`await`, `asyncio.gather`, `try`),
mirroring `orchestrator.py`'s own argument one level up for why the outer
pipeline isn't an LLM tool-calling loop either. **Do not** introduce
LangGraph/CrewAI/AutoGen/a custom workflow engine, or let any orchestration
layer itself become an LLM — this was an explicit constraint from the user
when this was built.

`failed_specialists: list[str]` threads through `AgentRunOutcome` →
`ReviewResult`; a non-empty list forces `needs_human_review=True` (a plain
Python OR, immune to gaming) and a `PARTIAL` heading on both published
comments (`core/report.py`).

## 8. Known bugs already fixed — don't reintroduce

Found only by live-testing against real infrastructure, not by the 600+ unit
tests — worth knowing so the same class of bug doesn't come back:

- **SQLite history deletion**: an early schema `DELETE`d a job's row when its
  idempotency key was reused after completion, destroying review history on
  every re-delegation. Fixed with a **partial unique index**
  (`WHERE status NOT IN (terminal_statuses)`) instead of a column-level
  UNIQUE — uniqueness now only applies to non-terminal (in-flight) jobs.
- **Sandbox CLI flag ordering**: `docker sandbox create shell WORKSPACE --name
  NAME` silently didn't work; the flag has to come before the subcommand's
  positional args (`--name NAME shell WORKSPACE`).
- **Linear delegation triggers**: the "Delegate to agent" UI action sets
  `delegateId` (not `assigneeId`), via a plain `Issue`/`update` event (not
  `AgentSessionEvent`) — and can also arrive as `delegateId` already set at
  issue **creation** time, with no `updatedFrom` to diff against. Both cases
  are handled in the trigger-extraction logic; a naive "check `assigneeId` in
  `updatedFrom`" reimplementation would silently miss both.
- **`UTILITY_MODEL` placeholder**: a config value that looks plausible
  (`gpt-5.6-luna`) but was never actually created as a real Azure deployment
  fails the Context Agent immediately with `DeploymentNotFound`. If a live
  run fails at `CONTEXT_AGENT_STARTED` with a 404, check this first.
- **Linear `$teamId` GraphQL type**: `app/services/linear_service.py`'s
  `WORKFLOW_STATES_QUERY` was originally declared `$teamId: String!`;
  Linear's real schema rejects that with `GRAPHQL_VALIDATION_FAILED` ("used
  in position expecting type ID") — it needs `ID!`. Caught only by a live
  §1b test (merging a real PR), not by any mocked unit test, since
  `respx`-mocked GraphQL calls never validate the query against a real
  schema. Worth checking with introspection (`__type(name: "X")`) before
  trusting *any* new GraphQL variable's type in this file, not just copying
  the pattern of an existing query.
- **A `LinearError` must be caught inside the sync service, not left to the
  webhook handler**: the bug above also exposed a second issue —
  `GitHubMergeSyncService.handle_pull_request_merged` didn't catch the
  `LinearError` that query bug raised, so it propagated past the webhook
  handler to FastAPI's global `AidaMateError` handler, which returned a 400
  to GitHub — breaking the "webhooks always get a 2xx" contract §1b/§1c's
  docstrings promise. Fixed by wrapping each sync service's Linear-calling
  section in its own `try/except LinearError`. `GitHubIssueSyncService`
  (§1c) was built with this already in place; if a new sync path is added,
  it needs the same wrapping from the start, not bolted on after a live
  failure finds it.
- **§1a's `AutoMergeService` had a TOCTOU race between reading `merge_status`
  and writing it**: `handle_issue_done` (a Linear webhook redelivery) and
  `confirm` (a double-clicked "Yes, merge") each read a job's `merge_status`,
  decided what to do, then wrote a new status later — with nothing preventing
  two concurrent calls for the same job from both passing the "not already
  merged" check. The loser's `merge_pull_request` then 405s (GitHub: already
  merged) and its `PullRequestNotMergeableError` handler overwrote the
  winner's correctly-recorded `MERGED` with `FAILED`, silently corrupting the
  record of a merge that actually succeeded. Found by code audit, not live
  traffic — caught before it caused a real incident. Fixed with a per-job
  `asyncio.Lock` in `AutoMergeService` (keyed by job id, not global) wrapping
  the whole read-decide-write sequence in both `handle_issue_done` and
  `confirm`, with a re-read of the job immediately after acquiring the lock
  so the second (now-serialized) caller sees the first caller's outcome and
  no-ops instead of overwriting it. Regression tests
  (`test_concurrent_redeliveries_do_not_both_merge`,
  `test_concurrent_confirm_calls_do_not_both_merge` in
  `tests/unit/test_auto_merge_service.py`) use a GitHub fake that yields
  control mid-merge (`await asyncio.sleep(0)`) so `asyncio.gather` actually
  interleaves the two calls — confirmed to fail without the lock before being
  kept as permanent coverage. Any other service that reads-then-later-writes
  a `ReviewJob`'s mutable state from a webhook-triggered path should get the
  same per-id-lock treatment, not just this one.
- **`LinearAuthService.get_access_token` had the same class of race, and it's
  the likely real explanation for the "OAuth expired" reconnects hit
  repeatedly during this project's live testing**: it read
  `installation.is_expired()` and called `refresh()` with no locking. Linear
  rotates its refresh token on every redemption (single-use), so two
  coroutines that both observe an expired token at once — easy to hit, since
  one review can make several Linear calls in flight — each send the *same*
  refresh_token to Linear; the loser is rejected with `Invalid refresh
  token`, which looks identical to (and was previously assumed to be) the
  installation itself having gone bad, forcing a full manual
  `/auth/linear/install` reconnect even though nothing was actually wrong
  with the connection. Fixed the same way as the `AutoMergeService` race
  above: a per-organization `asyncio.Lock` in `LinearAuthService` around the
  check-and-refresh sequence, re-checking `is_expired()` after acquiring the
  lock so a second caller that was only waiting sees the first caller's
  already-refreshed token instead of redeeming the now-stale refresh_token a
  second time. Regression test
  `test_concurrent_expired_access_does_not_double_refresh` in
  `tests/unit/test_linear_auth_service.py` uses a slow respx mock
  (`await asyncio.sleep(0)` mid-response) so `asyncio.gather` actually
  interleaves two `get_access_token` calls — confirmed 2 refresh calls
  without the lock, 1 with it. This closes the race *within one process*;
  running two server processes against the same `LINEAR_TOKEN_STORE_PATH`
  file at once (e.g. a stale `--reload` worker left alive alongside a fresh
  manual restart — see the `uvicorn --reload` note above) would still race
  across processes, since the lock is in-memory per-process. Don't start a
  new server without confirming the old one is actually stopped.
- **§1c's Linear OAuth app actor cannot create labels**: live-testing the
  GitHub Issues sync found `issueLabelCreate` rejected with
  `FORBIDDEN: "not allowed to take action"` /
  `userPresentableMessage: "You are not allowed to create labels in this
  team."` for the AIDA-MATE app actor on the GIT team, even though
  `issueCreate` on the same team succeeds and `ensure_label_id`'s
  existing-label *lookup* query (`TEAM_LABELS_QUERY`, `$teamId: ID!` — this
  one was already correct, unlike §1b's original `WORKFLOW_STATES_QUERY` bug)
  works fine. This is a Linear workspace/team permission restriction on the
  app actor, not something any GraphQL query change fixes. Originally
  `_upsert` treated any label failure as fatal to the whole sync, which given
  this restriction meant *every* sync failed until a human manually
  pre-creates the label once. Fixed by making label resolution best-effort:
  `_upsert` now catches `LinearError` around `ensure_label_id` specifically
  and creates the issue with `label_ids=None` rather than aborting — see the
  updated docstring and §1c's own note above. Confirmed live: GIT-13 was
  created with full correct content and no label, then future syncs will
  pick up the label automatically once one is pre-created by hand — no code
  change needed for that half.
- **Security audit: `LocalSandbox`'s `find`/`grep` did not enforce the
  workspace boundary `read_file`/`upload_bytes` already did — a live,
  exploitable path-traversal bug on this machine's actual running config**.
  `SANDBOX_MODE=local` (§6's documented working default here) runs `find`/
  `grep` directly on the host filesystem via `LocalSandbox._find_files`/
  `_grep` in `app/services/local_sandbox_service.py`. Unlike `upload_bytes`/
  `read_file`, which resolve every path through `_resolve_workspace_path`
  and reject anything outside the sandbox workspace, `_find_files`/`_grep`
  computed `cwd / target` with no such check — `app/tools/sandbox_tools.py`'s
  own `_scoped_path` docstring *claimed* "`ISandbox._resolve_workspace_path`
  additionally rejects any attempt to escape the workspace root," but that
  was only true for `read_file`, not for the `list_files`/`search_code`
  tools that go through `exec()`. A live proof-of-concept while reverting
  the fix (kept in git history via the regression tests below, not
  committed) showed `search_code` grepping a file one directory above the
  sandbox workspace and returning its exact contents in `stdout` — the same
  path a `Finding` takes to reach the public GitHub PR comment. Two
  redundant fixes, deliberately not either/or given what leaking through
  this path could expose (e.g. `LINEAR_TOKEN_STORE_PATH`'s plaintext OAuth
  tokens): (1) `app/tools/sandbox_tools.py`'s new `_validated_relative_path`
  rejects any `..`-climbing or absolute `path` argument *before* it reaches
  either sandbox backend, shared by `list_files`/`read_file`/`search_code`
  alike; (2) `LocalSandbox` now also enforces its own boundary independently
  via a new `_enforce_within_workspace` (refactored out of the existing
  `_resolve_workspace_path`), so the module's own documented promise is
  actually true. `SbxSandbox` (the `docker sandbox` backend) has the same
  shape of gap in principle, but there `find`/`grep` stay inside the
  isolated VM — much smaller blast radius, arguably within the documented
  threat model, not separately patched at the backend level (fix #1 above
  still covers it, since both backends share `sandbox_tools.py`). Regression
  tests prove the exploit and the fix: `tests/unit/test_local_sandbox_service.py`'s
  `test_exec_find_rejects_a_workspace_escape`/`test_exec_grep_rejects_a_workspace_escape`
  write a real secret to a file outside the workspace and confirm it's
  unreachable; `tests/unit/test_sandbox_tools.py` has equivalent coverage at
  the shared layer, including a sibling-directory-prefix case
  (`"repository_sibling"` vs `"repo"`) guarding against a naive
  `startswith(repo_dir)` check without the trailing `/`. Found by a
  dedicated security-audit agent pass, not live traffic — this class of gap
  (a documented protection that silently doesn't apply to every code path
  it's claimed for) is worth re-checking any time a new sandbox operation is
  added, not just trusting the existing docstring.
- **Security audit, lower-severity findings, all fixed**: (1)
  `GitHubService.download_archive` had no upper bound on response size —
  now streams via `httpx.AsyncClient.stream()` and aborts past
  `_MAX_ARCHIVE_BYTES` (500 MB) instead of buffering an unbounded body into
  memory. (2) `search_pull_requests`/`search_pull_requests_referencing`
  embedded `text` directly inside a `"..."` GitHub search qualifier with no
  escaping — a `text` value containing a literal `"` could break out of the
  quoted phrase and inject extra search qualifiers; neither of today's two
  callers passes attacker-controlled text, but a future caller might, so
  `_quoted_search_phrase` now strips embedded quotes unconditionally. (3)
  `app/api/merge_confirmation.py`'s page embeds `review_id` (an unguessable
  bearer token) in its own URL and links out to GitHub with no referrer
  policy — modern browsers' default already truncates the leaked referrer to
  just the origin, but the page now sets `<meta name="referrer"
  content="no-referrer">` and `rel="noreferrer"` on the outbound link
  explicitly rather than relying on that default. All three have regression
  tests; none were live-exploitable given today's actual callers/browsers,
  unlike the sandbox finding above — fixed as defense-in-depth, not urgent
  remediation.
- **No retry for transient GitHub/Linear failures, even though the error
  hierarchy already promised it**: `GitHubRateLimitError`'s own docstring
  said "the operation is retryable later" while nothing in the codebase
  ever retried anything — a single dropped connection or momentary 502/503
  failed an entire review outright, needing a human to hit
  `POST /reviews/{id}/retry` for something that would likely have succeeded
  seconds later on its own. Found by reading `orchestrator.py`/
  `review_agent.py`/`github_service.py`/`linear_service.py` end to end
  while asked to "strengthen the pipeline," not by live traffic. Fixed with
  `app/core/retry.py`'s `retry_async` (bounded exponential backoff, no
  jitter — this app's scale doesn't need thundering-herd protection),
  applied to `GitHubService._request`/`download_archive` and
  `LinearGraphQLClient.execute` — the two HTTP clients this codebase owns
  directly. Two new error types make the retry decision a plain
  `isinstance` check instead of string-matching a message:
  `GitHubServerError` (a GitHub 5xx) and `LinearUnavailableError`/
  `LinearServerError` (Linear network failure / 5xx) join the existing
  `GitHubRateLimitError`/`GitHubUnavailableError` as the only retried
  types — a 4xx, a GraphQL `errors` array, or a non-JSON body are never
  retried, since retrying those changes nothing. Deliberately **not**
  applied to two places: `GitHubService.merge_pull_request` (the TOCTOU
  race two entries above is exactly why blindly retrying a merge attempt
  is riskier than the transient failure it would paper over), and the
  OpenAI Agents SDK path in `review_agent.py` (the underlying `openai`
  client already retries transient failures internally before any
  `AgentsException` reaches `_run` — a second retry layer on top would
  risk compounding backoff delays against
  `SPECIALIST_TIMEOUT_SECONDS`/`AGENT_TIMEOUT_SECONDS` for no real gain).
  `retry_async`'s own tests (`tests/unit/test_retry.py`) pass a recording
  fake `sleep` directly to assert on backoff timing precisely and instantly.
  An earlier version of this change added an autouse `conftest.py` fixture
  that globally monkeypatched `asyncio.sleep` to make every retried
  failure-path test fast — reverted after it broke unrelated tests
  elsewhere in the suite that rely on a *real* `asyncio.sleep(N)` to
  simulate a slow operation for their own timeout assertions (e.g.
  `test_prompt_runner.py::test_run_times_out`'s fake agent call sleeping
  10s to prove a 0.01s timeout fires) — patching `asyncio.sleep` at the
  module level patches it for literally every caller in the process, not
  just `retry_async`. The handful of GitHub/Linear tests that exercise a
  retried-then-succeeds path just accept the small real backoff delay
  (~0.2s + ~0.4s) instead; `retry_async`'s default parameters keep that
  bounded and short. If test-suite speed here becomes a real problem later,
  the fix is scoping retry-speed to the specific test files/fixtures that
  need it, not a global autouse patch.
- **Security audit (multi-agent review + false-positive filtering), two
  HIGH findings fixed**: (1) The gated auto-merge confirmation link
  (`app/api/merge_confirmation.py`, CLAUDE.md §1a) used `job.id` as its
  entire "unguessable bearer token" security model — but `job.id` is also
  returned by the unauthenticated `GET /reviews` listing (`app/api/
  reviews.py`), so it was never actually secret. With `AUTO_MERGE_ON_DONE_
  ENABLED=true`, anyone who could reach `GET /reviews` could read a
  MEDIUM/HIGH-risk job's id and `POST /reviews/{id}/merge-confirm` directly,
  merging a PR with no human confirmation at all — exactly the control the
  feature exists to enforce. Fixed with a dedicated `ReviewJob.
  merge_confirmation_token` (`app/models/review.py`), minted fresh by
  `mark_merge_pending()` and never returned by any listing endpoint — the
  only place it's ever handed out is the Linear comment the confirmation
  link is posted in, the same decoupled-opaque-token pattern already used
  for `PostedComment` (§1e) and `review_id` in the OAuth `state` parameter.
  `IReviewJobRepository` gained `find_by_merge_confirmation_token`
  (both impls; the SQLite one scans COMPLETED rows, same tradeoff as
  `find_latest_completed_by_linear_issue_id`) — explicitly guarded so
  `token=None` never matches a job that hasn't minted one yet, since without
  that guard two `None`s would compare equal. `AutoMergeService.confirm`
  and its per-job lock are now keyed by this token instead of `job.id`
  (the lock's job is unchanged: serializing concurrent calls carrying the
  same value, which the token still is). (2) `POST /scheduled-prompts` and
  the web form (`app/api/scheduled_prompt_form.py`) had no restriction on
  which repository a schedule could target, unlike `github_webhook.py`'s
  existing `settings.github_repos` allowlist check — an unauthenticated
  caller could point a schedule (with LLM file-read tools) at any repo the
  server's GitHub credentials could reach, with results posted to an
  equally attacker-suppliable `linear_issue_id`. Fixed with the same
  allowlist check, added once in `_ensure_repository_allowed`
  (`app/api/scheduled_prompts.py`) and applied to both create and update
  (a PATCH could otherwise re-target an already-created schedule past the
  create-time check) — the web form inherits it for free, since it calls
  `create_scheduled_prompt` directly rather than reimplementing validation.
  Both findings came from `/security-review`'s standard flow: an
  identification pass over the PR diff, then one independent
  false-positive-filtering sub-agent per candidate, keeping only findings
  scored ≥8/10 — a third candidate (unescaped markdown link syntax in a
  scheduled prompt's `title` on the org-wide dashboard) scored 7/10 and was
  correctly dropped as a real-but-lower-confidence content-hygiene finding,
  not ignored by oversight.
- **Full-repo audit (not just the PR diff), three findings fixed**: run at
  the user's explicit request with `pip-audit` (dependency CVEs — clean) and
  `bandit` (static security lint — 7 hits, all confirmed false positives on
  manual review: constant strings bandit mistook for passwords, two
  intentional internal-invariant `assert`s, and two SQL queries that only
  interpolate a fixed enum-derived skeleton while binding every real value
  via `?` placeholders) alongside manual reading. (1) **HIGH — `/reviews*`
  and `/scheduled-prompts*` had no authentication at all** — the two
  webhooks are HMAC-signed and the three human-facing HTML pages gate on an
  unguessable bearer token in their own URL, but the plain JSON CRUD API had
  neither; `/scheduled-prompts` in particular is a full read/write/delete
  surface with no restriction on `linear_issue_id` or `prompt` content,
  reachable by anyone who could reach the host. Fixed with
  `app/core/api_auth.py`'s `require_management_api_key` — an `X-Api-Key`
  header checked with `hmac.compare_digest` against `MANAGEMENT_API_KEY`,
  applied as a router-level dependency to `reviews.router` and
  `scheduled_prompts.router` only (never to `merge_confirmation.py`/
  `comment_deletion.py`/`scheduled_prompt_form.py`, which keep their own
  token-in-URL model — and `scheduled_prompt_form.py`'s create/delete
  handlers call `scheduled_prompts.py`'s functions directly as plain Python,
  never re-entering that router's own HTTP routing, so the human-facing web
  form needs no key either). **Fail-closed by default** — unset
  `MANAGEMENT_API_KEY` rejects every request with 401, matching this
  codebase's own `GITHUB_WEBHOOK_SECRET`/`GITHUB_REPO_ALLOWLIST` precedent,
  not a fail-open default that would leave the gap in place. (2) **MEDIUM —
  every SQLite repository leaked a connection object.** All five
  (`sqlite_job_repository.py` and the four newer ones) did
  `with self._connect() as conn:` and nothing else — verified live in an
  interpreter that `sqlite3.Connection.__exit__` only commits/rolls back the
  transaction, it never closes the connection, contrary to what that
  pattern looks like it's doing. CPython's refcounting likely closes the
  underlying handle promptly in straight-line code today, but that's an
  implementation detail the sqlite3 docs explicitly warn against relying on
  ("the connection object should be closed manually"), and it silently stops
  being true the moment a connection reference is ever captured somewhere
  longer-lived. Fixed by turning `_connect()` into a `@contextmanager`
  generator that closes in a `finally` — every call site's existing
  `with self._connect() as conn:` needed zero changes, since entering a
  generator-based context manager reads identically to entering a raw
  connection's own. (3) **LOW — inconsistent referrer-policy hardening**:
  `merge_confirmation.py`/`comment_deletion.py` both set
  `<meta name="referrer" content="no-referrer">` as defense-in-depth for
  their id-in-URL pages; `scheduled_prompt_form.py`'s delete-confirmation
  page didn't. Added for consistency — actual impact was always minimal,
  since `scheduled_id` was never a secret the way the other two tokens are
  (it's already exposed by the same unauthenticated `GET /scheduled-prompts`
  finding (1) fixed). All three findings, and the audit that found them, ran
  as a full-repo pass (not scoped to the PR diff) at the user's explicit
  request — distinct from `/security-review`'s usual PR-diff-only scope.
- **Pipeline-strengthening pass, two "never raises" gaps found and fixed**:
  run at the user's explicit request ("strengthen the pipeline"), alongside
  a fresh `pip-audit`/`bandit` pass (clean — same 7 bandit hits as the audit
  above, all still the same confirmed false positives, nothing new). Both
  findings are the same class of bug as the `LinearError`-must-be-caught-
  inside-the-service lesson higher up this list, just missed in two spots
  that lesson hadn't reached yet. (1) **`AutoMergeService.handle_issue_done`
  didn't actually honor its own "never raises" docstring.** Unlike
  `GitHubMergeSyncService.handle_pull_request_merged` (which wraps its
  Linear-calling section in `try/except LinearError`, the fix that very bug
  taught this codebase), `handle_issue_done` had no exception handling at
  all — a `GitHubServerError`/`GitHubUnavailableError` surviving
  `merge_pull_request`'s retries, a `LinearError` from
  `add_comment`, or even a repository `save()` failure would propagate
  straight through the Linear webhook handler to FastAPI's global
  `Exception` handler, returning a 500 to Linear and breaking the same
  "webhooks always get 2xx" contract §8's earlier entry already fixed
  elsewhere. Fixed by extracting the existing body into `_handle_issue_done`
  and wrapping the call in `handle_issue_done` with a blanket
  `try/except Exception` + `logger.exception` — broader than the narrow
  `except LinearError` its sibling service uses, deliberately: this method
  spans both GitHub and Linear calls plus the job repository, not one
  client's calls specifically, so no single exception type covers every
  failure mode along it. Regression tests
  (`test_handle_issue_done_never_raises_on_a_github_failure`,
  `test_handle_issue_done_never_raises_on_a_linear_failure` in
  `tests/unit/test_auto_merge_service.py`) use fakes that raise
  unconditionally on the merge/comment call and confirm `handle_issue_done`
  still returns normally. (2) **`ScheduledPromptWorker._loop` had no guard
  around `tick()` itself.** `tick()` already wraps each individual
  schedule's `service.run()` and dashboard sync in their own try/except, but
  nothing wrapped `tick()`'s own machinery — `self._repository.list_all()`
  in particular. `ReviewQueue._consume` has exactly this guard around
  `self._worker.run(job_id)` ("this only catches a defect in that handling,
  which must not kill the worker permanently"); `ScheduledPromptWorker`
  was missing the equivalent. Since nothing ever awaits the worker's
  `asyncio.Task` outside `stop()`, an unhandled exception there would kill
  the timer permanently and silently — no schedule would fire again, with
  no error surfaced anywhere short of a human noticing schedules stopped
  running, until a manual server restart. Fixed by wrapping the `await
  self.tick()` call in `_loop` the same way `_consume` wraps its worker
  call. Regression test `test_loop_survives_tick_raising` in
  `tests/unit/test_scheduled_prompt_worker.py` uses a repository whose
  `list_all()` always raises, ticks fast enough to observe more than one
  call despite every call failing, and confirms `stop()` doesn't re-raise
  the exception a dead task would otherwise still be holding. Both fixes
  verified: full suite (1194 tests) green, `ruff check` clean, both new
  tests passing on their own before the full run.
- **External code-review batch (11 findings + 1 unfounded), verified one by
  one against current code before touching anything — 8 fixed, 3 skipped
  with reasons recorded here.** Findings arrived as untrusted review text
  (file paths and line numbers only, no access to this repo's own design
  docs), so several either duplicated a decision this file already
  documents deliberately, or described a mechanism that doesn't exist here
  — both treated as reasons to skip, not silently implement. **Fixed:**
  (1) `GET /auth/linear/status` had no authentication at all — leaked every
  connected workspace's identity/scopes to anyone reaching the host, unlike
  `/reviews*`/`/scheduled-prompts*`. Gated with `require_management_api_key`
  applied per-route (not at the router level), so `/install`/`/callback`
  stay browser-accessible for the OAuth redirect flow. (2) `AutoMergeService.
  handle_issue_done`'s "already decided" guard didn't include
  `MergeStatus.DECLINED` — a redelivered Done event after a human clicked
  "No" would merge a LOW-risk PR the human never confirmed, or repost a
  fresh MEDIUM/HIGH confirmation request as if nothing happened. `FAILED`
  deliberately stays retryable, unchanged. (3) `AutoMergeService.confirm()`
  locked by `merge_confirmation_token` while `handle_issue_done` locked by
  `job.id` — two different keys for the same job, so the two entry points
  only ever serialized against duplicates of themselves. `confirm()` now
  resolves the job from the token first, then locks and re-validates by
  `job.id`, matching `_handle_issue_done`'s key. (4) `DefaultRepoSchedule
  Service.ensure_for_repository`'s `list_all()` + `create()` wasn't atomic —
  two GitHub objects landing in the same brand-new repo could each create
  their own default schedule. Fixed with a single process-wide
  `asyncio.Lock` (this operation fires once per newly-linked repo, never a
  hot path, so the coarser-than-per-repo contention is negligible) — the
  same in-process-lock tradeoff already established for `AutoMergeService`/
  `LinearAuthService` above, not a repository-interface change. (5) A
  non-string `severity` value in a GitHub security-alert payload (field
  types not verified against live traffic — see §1c) would crash
  `severity.title()`, turning a webhook delivery into an unhandled 500
  instead of a synced issue. Coerced to `str` first; `severity` absent
  still omits the prefix, unchanged. (6) `ScheduledPromptDashboardService.
  ensure()`'s `find_team_id_by_key` call wasn't wrapped in `try/except
  LinearError` the way `sync()`'s `list_teams()` call already is — a
  transient Linear failure escaped as a raw exception to `ensure()`'s
  callers (the web form, `DefaultRepoScheduleService`) instead of the
  `None` they already handle gracefully. (7) `ScheduledPromptWorker._is_due`
  could receive a schedule missing the field its own frequency requires
  (`interval_hours`/`day_of_month` — reachable via `PATCH`, which
  deliberately doesn't cross-validate frequency-consistency, see below) and
  crash `timedelta`/`min` on `None`; the crash aborted `tick()`'s for-loop
  entirely, silently skipping every later schedule and
  `_maybe_resync_dashboards()` for that tick, every tick, forever. Now
  treated as not-due with a logged warning, plus a per-entry try/except
  around the due-check itself as a second net. (8) `ReviewWorker.run()`'s
  `except AidaMateError`/`except Exception` never caught
  `asyncio.CancelledError` (a `BaseException`, correctly — swallowing
  cancellation would break `ReviewQueue.stop()`'s shutdown semantics), so a
  job cancelled mid-flight by queue shutdown was left stuck at whatever
  intermediate status it last saved until the next startup's `INTERRUPTED`
  reconciliation sweep caught it. Now caught, marks the job `INTERRUPTED`
  (already-existing, already-retryable status — see `job_repository.py`'s
  startup reconciliation) immediately, and re-raises so cancellation still
  completes. **Fixed, narrower than requested:** (9) `docker sandbox`'s
  deprecation (already documented above) meant `SbxSandboxFactory.create()`
  failed with a generic exit-code-and-stderr dump — an operator would go
  hunting for a Docker Desktop problem that doesn't exist. Detects Docker's
  own "deprecated and has been removed" stderr text and raises a clear,
  actionable message instead; `.env.example`/`ARCHITECTURE.md` updated to
  match. Explicitly did **not** attempt "implement a replacement `sbx`
  adapter" — there is no verified specification for whatever Docker's
  current "Docker Sandboxes" product's actual CLI/API shape is (confirmed
  live this session that `docker sandbox --help` gives zero information
  about it), and fabricating one would be worse than today's honest,
  already-non-silent failure (`execute()`'s own documented design: "an
  operator who turned the capability on should be told when it breaks, not
  have it quietly vanish"). **Skipped, invalid against current code or
  current design:** (a) "Add session authentication and CSRF protection to
  the scheduled-prompt web form" — this codebase has no session/user-auth
  mechanism anywhere (by design: server-to-server + webhook + unguessable-
  token-link, never a logged-in-user app), and the form's lack of auth is
  an explicit, previously-reviewed decision recorded in §1d and in
  `api_auth.py`'s own module docstring, which already explains why
  (`/scheduled-prompts/new` is advertised as a plain link inside the Linear
  dashboard description precisely so any team member can use it without a
  shared key). A prior audit already chose the repo-allowlist restriction
  over an auth gate for this exact surface (§8, "no repo allowlist"
  finding, above). Building a login system now would be a major
  unrequested architecture addition, not a fix. (b) "Cross-validate
  frequency-consistency on `ScheduledPromptUpdate`/PATCH" — §1d already
  documents this as "a known, accepted gap... not something to fix
  reflexively if noticed later." The one concrete failure mode a malformed
  PATCH could cause (a crash in the worker's due-check) is exactly what
  fix (7) above now independently guards against, which removes the actual
  justification for revisiting that documented decision here. (c) "Replace
  shell-based archive extraction (`mkdir`/`tar`) in
  `scheduled_prompt_service.py` with a typed sandbox operation" —
  `LocalSandbox.exec()` (`app/services/local_sandbox_service.py`) already
  recognizes this exact command by exact string match and natively
  reimplements it via `tarfile` in pure Python; there is no actual
  shell/POSIX-utility dependency on the `local` backend this project
  actually runs on. The `docker` backend's `sh -c`/`tar` inside a genuine
  Linux container is standard, not fragile, and moot regardless while that
  backend is non-functional per finding (9). (d) "Docstring coverage below
  an 80% pre-merge target" — no such gate exists anywhere in this repo (no
  `interrogate`/`pydocstyle`, no `.github/workflows`, ruff's own `select`
  doesn't include `D`); the finding doesn't correspond to anything
  configured here. All fixes covered by new regression tests (16 new
  tests: 2 for the `/status` auth gate, 5 for the malformed-schedule guard,
  3 for the DECLINED/lock fixes, 1 for the default-schedule race, 1 for the
  severity coercion, 1 for the dashboard `ensure()` guard, 2 for
  cancellation handling, 1 for the sandbox error message). Full suite
  (1210 tests) green, `ruff check` clean.
- **The Docker Sandbox adapter now targets `sbx`, live-verified — the
  "no verified replacement spec" reason the previous entry gave for not
  building one no longer holds.** A follow-up finding pointed at "Docker's
  documented sbx CLI"; unlike the earlier vague "public docs describe a
  differently-shaped standalone `sbx` binary" note (§6, written when
  `docker sandbox` still worked and nobody had reason to look closer), this
  time `sbx --help` and every subcommand's own `--help` were read directly
  — `sbx` turned out to be a real, currently-installed, fully-documented
  binary on this machine (`C:\Users\...\DockerSandboxes\bin\sbx.exe`,
  `sbx version` reports v0.38.0), not a hypothetical. Verified with a full
  live cycle through the actual rewritten adapter code (not just manual CLI
  probing): `SbxSandboxFactory.create()` → `SbxSandbox.upload_bytes()` →
  `.exec()` → `.read_file()` → `.destroy()`, all against the real `sbx`
  binary, before trusting any of this. Command surface changed shape
  entirely, not just the binary name: `sbx <subcommand>` directly (`sbx
  create`, `sbx exec`, `sbx rm`), not `docker sandbox <subcommand>`.
  `sbx create shell WORKSPACE --name NAME` both provisions and starts the
  sandbox in one step — confirmed live that `exec` worked immediately after
  `create` with no separate `run` call needed, unlike the old plugin.
  `sbx rm SANDBOX --force` needs `--force`, or it blocks on an interactive
  confirmation prompt that would hang this app's non-interactive subprocess
  call forever (also confirmed live). **A genuine, Windows-specific gotcha
  found only by testing, not readable from `--help` text**: the sandbox
  does NOT mount the workspace at the literal host path — a Windows path
  like `C:\Users\...\workspace` is visible inside the Linux container at a
  POSIX-translated path (`/c/Users/.../workspace`); passing the raw host
  path as `sbx exec --workdir` fails with "No such file or directory"
  (reproduced live before fixing). Since `sbx exec` already defaults its
  own cwd to the mounted workspace when `--workdir` is omitted (also
  confirmed live) and every caller in this codebase calls `exec()` with
  `cwd=None`, the fix is to simply omit `--workdir` in that case rather
  than computing and passing a path — the previous adapter's approach
  (always passing an explicit `--workdir`, because `docker sandbox`'s own
  default wasn't documented) would be actively wrong against `sbx` on this
  host. `SANDBOX_BINARY` default changed `docker` → `sbx`
  (`app/core/config.py`); `.env.example`, `README.md` (setup steps and the
  status table), `ARCHITECTURE.md` (two spots, one via an explicit
  "correction, preserved" note rather than silently rewriting the old
  claim — same pattern §6 already uses), and stray docstring references in
  `orchestrator.py`/`errors.py`/`main.py`/`local_sandbox_service.py` all
  updated to match. `local_sandbox_service.py`'s own claim that Docker
  Sandboxes "cannot run on this particular machine" was corrected the same
  way. Test suite rewritten against the new command shapes (the old
  create-then-run two-call assertion, the always-pass-`--workdir` default,
  and the `docker sandbox`-deprecation-message-detection test — that
  message only ever came from the old plugin's stderr and can no longer
  occur — all replaced; net two fewer tests, same coverage per current
  behavior). Full suite (1208 tests) green, `ruff check` clean.

## 9. Standing working agreements with this user

- **Confirm before installing new software or other system-modifying
  actions.** Config file edits and code changes within the repo don't need
  this; installing packages, changing system state, etc. do.
- **Never print/expose secret values** — see §6.
- **Live-test before trusting the unit suite alone.** This project's history
  shows real bugs (§8) that 600+ passing tests did not catch, only running
  the real system did. When asked to verify something is "done," prefer an
  actual run over "tests pass" when the two are both available.
- User prefers being asked before large-blast-radius live actions (posting
  real comments to a real public repo, spending real LLM API cost) —
  `AskUserQuestion` first, then proceed once confirmed.
- Multi-agent architecture was deliberately deferred twice earlier in this
  project's history before finally being approved and built (§7) — if asked
  to touch it, the design rationale in ARCHITECTURE.md §6a is the record of
  *why* it looks the way it does, not just *what* it is.

## 10. Where things stand right now

9 phases shipped (scaffold → OAuth → webhook/lifecycle → GitHub integration →
deterministic engines → sandbox+single agent → persistence → multi-agent
upgrade → ongoing live verification), 709 tests passing, `ruff` clean, 3 real
end-to-end verifications against live Linear + GitHub + Azure OpenAI. Nothing
is mid-implementation as of this writing. Full backlog, priority order, and
the reasoning behind it: **[ROADMAP.md](ROADMAP.md) Parts 3–4.**

---

<sub>Generated as a session-handoff document — as of 2026-08-13. If this drifts
from the real state of the code, trust the code and the other docs over this
file, and update this file to match.</sub>
