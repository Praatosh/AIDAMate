"""OpenAI Agents SDK implementation of `IReviewAgentRunner`.

The only file that imports `agents` (the `openai-agents` package). Behind the
single `analyze()` call the Protocol requires, this runs a real multi-agent
pipeline — Context concurrently with four specialists, then a Judge — built
once at construction and reused across reviews, same as the single-agent
design this replaces.

The manager pattern here is plain Python (`asyncio.gather`, `await`, plain
`if`/`try`), never the SDK's own agent-to-agent handoff mechanism. This
mirrors `orchestrator.py`'s own argument, one level up, for why the outer
review pipeline is deterministic code rather than an LLM tool-calling loop:
the specialist sequence ((Context, parallel batch) -> Judge — Context and the
batch launched together, not one gating the other) also has exactly one valid
shape, and handing an LLM control over that sequencing would add
skip/reorder/failure modes for no benefit.

Everything downstream of `analyze()` is unaffected by any of this: it still
returns one `AgentRunOutcome` wrapping one `ReviewAnalysis` (the Judge's),
exactly as the single-agent version did. `orchestrator.py`, `risk_engine.py`,
and `report.py` do not know or care that six agents ran instead of one.
"""

import asyncio
import contextlib
from dataclasses import dataclass

from agents import Agent, ModelSettings, RunConfig, Runner, set_default_openai_client
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.core.errors import AgentError, AgentOutputError
from app.core.events import (
    ARCHITECTURE_AGENT_COMPLETED,
    ARCHITECTURE_AGENT_FAILED,
    ARCHITECTURE_AGENT_STARTED,
    CODE_AGENT_COMPLETED,
    CODE_AGENT_FAILED,
    CODE_AGENT_STARTED,
    CONTEXT_AGENT_COMPLETED,
    CONTEXT_AGENT_FAILED,
    CONTEXT_AGENT_STARTED,
    JUDGE_AGENT_COMPLETED,
    JUDGE_AGENT_STARTED,
    SECURITY_AGENT_COMPLETED,
    SECURITY_AGENT_FAILED,
    SECURITY_AGENT_STARTED,
    TESTING_AGENT_COMPLETED,
    TESTING_AGENT_FAILED,
    TESTING_AGENT_STARTED,
)
from app.core.interfaces import ISandbox
from app.core.logging import get_logger
from app.models.github import PullRequest
from app.models.review import AgentRunOutcome, PRContextAnalysis, ReviewAnalysis
from app.prompts.review_prompt import (
    ARCHITECTURE_SYSTEM_PROMPT,
    CODE_SYSTEM_PROMPT,
    CONTEXT_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    SECURITY_SYSTEM_PROMPT,
    TESTING_SYSTEM_PROMPT,
)
from app.tools.sandbox_tools import SandboxToolContext, list_files, read_file, search_code

logger = get_logger(__name__)

#: Bound on how much diff text is inlined into the prompt directly. The rest
#: of the repository is still reachable through tool calls — this only caps
#: what is pushed into context unconditionally, not what any agent can see.
_MAX_PATCH_CHARS_PER_FILE = 4_000
_MAX_TOTAL_PATCH_CHARS = 40_000

#: Maps a specialist's short internal name to its lifecycle log events. No
#: entry for "judge" or "context" — the Judge has no _FAILED event (its
#: failure is a whole-review failure, already REVIEW_FAILED) and Context is
#: handled inline in `analyze()` since it is the only stage with non-uniform
#: (non-fatal-on-failure-but-not-a-gather-batch-member) handling.
_SPECIALIST_EVENTS: dict[str, tuple[str, str, str]] = {
    "security": (SECURITY_AGENT_STARTED, SECURITY_AGENT_COMPLETED, SECURITY_AGENT_FAILED),
    "code": (CODE_AGENT_STARTED, CODE_AGENT_COMPLETED, CODE_AGENT_FAILED),
    "architecture": (ARCHITECTURE_AGENT_STARTED, ARCHITECTURE_AGENT_COMPLETED, ARCHITECTURE_AGENT_FAILED),
    "testing": (TESTING_AGENT_STARTED, TESTING_AGENT_COMPLETED, TESTING_AGENT_FAILED),
}


def _build_input(pull_request: PullRequest) -> str:
    """Render the pull request into the shared "PR facts" block.

    Title/body/diff hunks are already-fetched, credential-free GitHub data —
    this is the one place PR *content* (not credentials) reaches the LLM
    directly, which is expected: a code-review agent has to read the diff.
    Used as the base input for Context and, with a context hint appended, for
    each specialist.
    """
    lines = [
        f"Repository: {pull_request.ref.repository.full_name}",
        f"Pull request: #{pull_request.ref.number} — {pull_request.title}",
        f"Base: {pull_request.base_ref}  Head: {pull_request.head_ref} ({pull_request.head_sha})",
        "",
        "## Description",
        pull_request.body or "(no description provided)",
        "",
        "## Changed files",
    ]

    total = 0
    for changed in pull_request.changed_files:
        lines.append(f"\n### {changed.filename} ({changed.status.value})")
        if not changed.patch:
            lines.append("(no inline diff available — binary or too large; use read_file if needed)")
            continue
        patch = changed.patch[:_MAX_PATCH_CHARS_PER_FILE]
        if total + len(patch) > _MAX_TOTAL_PATCH_CHARS:
            lines.append("(diff omitted here to stay within budget — use read_file/search_code)")
            continue
        total += len(patch)
        lines.append(f"```diff\n{patch}\n```")

    return "\n".join(lines)


def _render_context_hint(context_analysis: PRContextAnalysis | None) -> str:
    """Render the Context Agent's output as a hint block for the specialists.

    Advisory only — no specialist's tool access is restricted by this, and a
    missing Context result (its own failure, or never having a finding to
    show) renders as an honest placeholder rather than silently omitting the
    section.
    """
    if context_analysis is None:
        return "## PR Context\n(PR context analysis unavailable for this review — investigate independently.)"

    lines = [
        "## PR Context (from the Context Agent — advisory only, investigate independently)",
        f"Intent: {context_analysis.intent}",
        f"Summary: {context_analysis.summary}",
    ]
    if context_analysis.important_files:
        lines.append(f"Important files: {', '.join(context_analysis.important_files)}")
    if context_analysis.affected_components:
        lines.append(f"Affected components: {', '.join(context_analysis.affected_components)}")
    return "\n".join(lines)


def _build_judge_input(
    context_analysis: PRContextAnalysis | None,
    successes: list["_SpecialistOutcome"],
    failed: list[str],
) -> str:
    """Render the Judge's input: the context hint plus each surviving
    specialist's structured output, labeled by name, plus which stages did
    not complete. The diff itself is deliberately not re-inlined here — the
    Judge reconciles judgments already grounded in tool-verified evidence
    rather than rediscovering them from scratch.
    """
    lines = [_render_context_hint(context_analysis), "", "## Specialist findings"]
    for outcome in successes:
        lines.append(f"\n### {outcome.name}")
        lines.append(outcome.analysis.model_dump_json(indent=2))  # type: ignore[union-attr]
    if failed:
        lines.append(f"\n## Specialists that did not complete: {', '.join(sorted(failed))}")
    return "\n".join(lines)


@dataclass
class _SpecialistOutcome:
    """Internal bookkeeping for one specialist's concurrent run."""

    name: str
    analysis: ReviewAnalysis | None
    tool_calls_count: int
    failed: bool


class OpenAIAgentRunner:
    """Runs AIDA-MATE's multi-agent review pipeline against a prepared sandbox."""

    def __init__(
        self,
        *,
        model: str,
        utility_model: str | None = None,
        max_turns: int = 15,
        specialist_timeout_s: float = 60.0,
        reasoning_effort: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
    ) -> None:
        # A custom `base_url` (an organization's own gateway/proxy in front of
        # the OpenAI API) is set once, globally, at construction time — the
        # SDK has no per-`Runner.run()` way to select a client. Tracing is
        # disabled whenever this is set: it always uploads to OpenAI's own
        # endpoint regardless of `base_url`, which a gateway-scoped key
        # typically cannot authenticate against.
        #
        # `api_version` selects the Azure client: Azure OpenAI / Azure AI
        # Foundry endpoints reject requests missing the `api-version` query
        # parameter, which only `AsyncAzureOpenAI` adds automatically — the
        # plain client has no equivalent option. `_tracing_disabled` is tied
        # to `api_version` too, not just `base_url`: an Azure/Azure AI Foundry
        # key generally can't authenticate against OpenAI's own tracing-upload
        # endpoint either, so tracing must stay disabled whenever the Azure
        # branch is taken — independent of whether `base_url is not None`
        # alone would already imply it (Settings' own validator requires the
        # two go together in practice, but this stays correct even for a
        # runner constructed directly, e.g. in a test, with `api_version` set
        # and `base_url` None).
        self._tracing_disabled = base_url is not None or api_version is not None
        if api_version:
            client = AsyncAzureOpenAI(base_url=base_url, api_key=api_key, api_version=api_version)
            set_default_openai_client(client, use_for_tracing=False)
        elif base_url is not None:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            set_default_openai_client(client, use_for_tracing=False)

        # Pinned to 0 for reproducibility — risk_engine.py's scoring is only as
        # reproducible as the finding categories feeding it (see its module
        # docstring). Left unset on reasoning models: those generally reject an
        # explicit `temperature` alongside `reasoning.effort`, so `reasoning_effort`
        # itself (already the low-latency choice when set) is the only determinism
        # lever available for that case.
        model_settings = ModelSettings(
            temperature=0.0 if not reasoning_effort else None,
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            verbosity="low" if reasoning_effort else None,
            parallel_tool_calls=True if reasoning_effort else None,
        )

        # Context is cheap orientation, not investigation — no tools, and the
        # cost-optimized model when one is configured (falls back to the
        # quality-critical model otherwise, same as every specialist).
        self._context_agent = Agent(
            name="AIDA-MATE Context Agent",
            instructions=CONTEXT_SYSTEM_PROMPT,
            model=utility_model or model,
            output_type=PRContextAnalysis,
            model_settings=model_settings,
        )

        tool_set = [list_files, read_file, search_code]
        self._security_agent = Agent(
            name="AIDA-MATE Security Agent",
            instructions=SECURITY_SYSTEM_PROMPT,
            model=model,
            tools=[*tool_set],
            output_type=ReviewAnalysis,
            model_settings=model_settings,
        )
        self._code_agent = Agent(
            name="AIDA-MATE Code Agent",
            instructions=CODE_SYSTEM_PROMPT,
            model=model,
            tools=[*tool_set],
            output_type=ReviewAnalysis,
            model_settings=model_settings,
        )
        self._architecture_agent = Agent(
            name="AIDA-MATE Architecture Agent",
            instructions=ARCHITECTURE_SYSTEM_PROMPT,
            model=model,
            tools=[*tool_set],
            output_type=ReviewAnalysis,
            model_settings=model_settings,
        )
        self._testing_agent = Agent(
            name="AIDA-MATE Testing Agent",
            instructions=TESTING_SYSTEM_PROMPT,
            model=model,
            tools=[*tool_set],
            output_type=ReviewAnalysis,
            model_settings=model_settings,
        )
        self._specialists: list[tuple[str, Agent]] = [
            ("security", self._security_agent),
            ("code", self._code_agent),
            ("architecture", self._architecture_agent),
            ("testing", self._testing_agent),
        ]

        # No tools: the Judge synthesizes evidence the specialists already
        # gathered rather than re-investigating the repository itself — this
        # keeps cost down and stops the Judge quietly overriding evidenced
        # findings with a shallower pass of its own.
        self._judge_agent = Agent(
            name="AIDA-MATE Judge Agent",
            instructions=JUDGE_SYSTEM_PROMPT,
            model=model,
            output_type=ReviewAnalysis,
            model_settings=model_settings,
        )

        self._model = model
        self._max_turns = max_turns
        self._specialist_timeout_s = specialist_timeout_s

    async def analyze(
        self, pull_request: PullRequest, sandbox: ISandbox, *, review_id: str
    ) -> AgentRunOutcome:
        """Run the multi-agent review pipeline over `pull_request`.

        Context runs *concurrently* with the four specialists rather than
        gating them: its output is a cost-control hint for the Judge, not a
        hard input to the specialists, which have always kept full
        independent tool access and full diff visibility regardless of
        whether Context succeeded (a Context failure has always been
        non-fatal, precisely because nothing downstream structurally depends
        on it). Blocking the specialist batch on a full Context round-trip
        first bought nothing but latency, since the specialist batch takes
        longer than Context in every realistic case — Context is always done
        well before the Judge, its only consumer, needs it. The four
        specialists run concurrently with each other too, each with its own
        `SandboxToolContext` (so per-call tool-call logging is unambiguous
        even though they overlap in time) and its own timeout (so one hung
        specialist cannot silently stall the whole batch until the outer,
        whole-run timeout fires). A specialist failure is recorded, not
        fatal, as long as at least one specialist survives. The Judge then
        reconciles whatever succeeded — a Judge failure is always fatal,
        since it is the only source of the `ReviewAnalysis` that ever
        reaches the risk engine, and there is no reasonable partial result
        without a synthesis step.

        `review_id` is for log correlation only (`AGENT_TOOL_CALL` and the
        per-agent lifecycle events) — it never reaches any prompt.

        Raises:
            AgentError: every specialist failed, or the Judge failed, timed
                out, or exceeded its turn budget.
            AgentOutputError: the Judge's final output did not satisfy the
                `ReviewAnalysis` schema.
        """
        base_input = _build_input(pull_request)
        failed: list[str] = []
        total_tool_calls = 0

        context_ctx = SandboxToolContext(sandbox=sandbox, review_id=review_id, agent_name="context")
        logger.info(CONTEXT_AGENT_STARTED, extra={"review_id": review_id})
        context_task = asyncio.create_task(
            self._run(
                self._context_agent, base_input, context=context_ctx, timeout_s=self._specialist_timeout_s
            )
        )

        # No hint to inject here: specialists never wait on Context, so it is
        # never ready in time to include, by construction. This is the same
        # "unavailable — investigate independently" placeholder specialists
        # already got whenever Context genuinely failed.
        specialist_input = f"{base_input}\n\n{_render_context_hint(None)}"
        results = await asyncio.gather(
            *(
                self._run_specialist(name, agent, sandbox, specialist_input, review_id)
                for name, agent in self._specialists
            )
        )

        successes = [outcome for outcome in results if not outcome.failed]
        if not successes:
            context_task.cancel()
            with contextlib.suppress(AgentError, AgentOutputError, asyncio.CancelledError):
                await context_task
            raise AgentError("All specialist agents failed; nothing for the Judge to reconcile")

        failed.extend(outcome.name for outcome in results if outcome.failed)
        total_tool_calls += sum(outcome.tool_calls_count for outcome in results)

        try:
            context_result = await context_task
            context_analysis = self._extract_output(
                context_result, PRContextAnalysis, self._context_agent.name
            )
            logger.info(CONTEXT_AGENT_COMPLETED, extra={"review_id": review_id})
        except (AgentError, AgentOutputError) as exc:
            logger.warning(CONTEXT_AGENT_FAILED, extra={"review_id": review_id, "error": str(exc)})
            failed.append("context")
            context_analysis = None
        total_tool_calls += context_ctx.tool_calls_count

        judge_input = _build_judge_input(context_analysis, successes, failed)
        judge_ctx = SandboxToolContext(sandbox=sandbox, review_id=review_id, agent_name="judge")
        logger.info(JUDGE_AGENT_STARTED, extra={"review_id": review_id})
        # No inner timeout for the Judge, deliberately: its failure is always
        # fatal, so the outer `asyncio.wait_for(analyze(...), agent_timeout_s)`
        # in orchestrator.py is the only bound it needs.
        judge_result = await self._run(self._judge_agent, judge_input, context=judge_ctx, timeout_s=None)
        judge_analysis = self._extract_output(judge_result, ReviewAnalysis, self._judge_agent.name)
        total_tool_calls += judge_ctx.tool_calls_count
        logger.info(
            JUDGE_AGENT_COMPLETED,
            extra={"review_id": review_id, "findings": len(judge_analysis.findings)},
        )

        return AgentRunOutcome(
            analysis=judge_analysis,
            model=self._model,
            tool_calls_count=total_tool_calls,
            failed_specialists=sorted(failed),
        )

    async def _run_specialist(
        self,
        name: str,
        agent: Agent,
        sandbox: ISandbox,
        prompt_input: str,
        review_id: str,
    ) -> _SpecialistOutcome:
        """Run one specialist, translating any failure into a recorded
        outcome rather than letting it propagate — `analyze()`'s
        `asyncio.gather` call needs every task to complete normally so the
        other specialists are never cancelled by one's failure."""
        started, completed, failed_event = _SPECIALIST_EVENTS[name]
        ctx = SandboxToolContext(sandbox=sandbox, review_id=review_id, agent_name=name)
        logger.info(started, extra={"review_id": review_id, "agent": name})
        try:
            result = await self._run(
                agent, prompt_input, context=ctx, timeout_s=self._specialist_timeout_s
            )
            analysis = self._extract_output(result, ReviewAnalysis, agent.name)
            logger.info(
                completed,
                extra={"review_id": review_id, "agent": name, "findings": len(analysis.findings)},
            )
            return _SpecialistOutcome(
                name=name, analysis=analysis, tool_calls_count=ctx.tool_calls_count, failed=False
            )
        except (AgentError, AgentOutputError) as exc:
            logger.warning(failed_event, extra={"review_id": review_id, "agent": name, "error": str(exc)})
            return _SpecialistOutcome(
                name=name, analysis=None, tool_calls_count=ctx.tool_calls_count, failed=True
            )

    async def _run(
        self,
        agent: Agent,
        prompt_input: str,
        *,
        context: SandboxToolContext,
        timeout_s: float | None,
    ):
        """Run one agent call, bounded by `timeout_s` when given, translating
        SDK exceptions into AIDA-MATE's own error types. Shared by every stage
        (Context, each specialist, Judge) — the only difference between them
        is which `Agent`, input, and timeout budget are passed in.
        """
        run_config = RunConfig(tracing_disabled=True) if self._tracing_disabled else None
        coro = Runner.run(
            agent, prompt_input, context=context, max_turns=self._max_turns, run_config=run_config
        )
        try:
            if timeout_s is not None:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            return await coro
        except TimeoutError as exc:
            raise AgentError(f"{agent.name} exceeded {timeout_s}s") from exc
        except MaxTurnsExceeded as exc:
            raise AgentError(f"{agent.name} exceeded {self._max_turns} turns") from exc
        except ModelBehaviorError as exc:
            raise AgentOutputError(f"{agent.name} output did not match the expected schema: {exc}") from exc
        except AgentsException as exc:
            raise AgentError(f"{agent.name} failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _extract_output(result, output_type: type, label: str):
        """Extract and type-check one agent's final output.

        Raises:
            AgentOutputError: belt-and-braces — `ModelBehaviorError` should
                already have caught a real schema mismatch inside `_run`, so
                reaching this means something more unusual happened in the
                SDK's own output coercion.
        """
        try:
            return result.final_output_as(output_type, raise_if_incorrect_type=True)
        except TypeError as exc:
            raise AgentOutputError(f"{label} final_output was not a {output_type.__name__}: {exc}") from exc
