"""OpenAIAgentRunner: multi-agent pipeline construction, sequencing, partial-failure
handling, and SDK error translation.

`agents.Runner.run` is monkeypatched throughout — no real OpenAI API key or
network call is needed to run this suite. Because the same `Runner.run` seam
is shared by all six agents (Context, four specialists, Judge), most fakes
dispatch on `agent.name` to return the right output type for whichever stage
is calling.
"""

import asyncio

import pytest
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from openai import AsyncAzureOpenAI

from app.agents.review_agent import (
    OpenAIAgentRunner,
    _build_input,
    _build_judge_input,
    _render_context_hint,
    _SpecialistOutcome,
)
from app.core.errors import AgentError, AgentOutputError
from app.models.common import Area, Severity
from app.models.github import ChangedFile, FileChangeStatus, PullRequest, PullRequestRef, RepositoryRef
from app.models.review import Finding, PRContextAnalysis, ReviewAnalysis
from app.prompts.review_prompt import (
    ARCHITECTURE_SYSTEM_PROMPT,
    CODE_SYSTEM_PROMPT,
    CONTEXT_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    SECURITY_SYSTEM_PROMPT,
    TESTING_SYSTEM_PROMPT,
    UNTRUSTED_INPUT_PREAMBLE,
)

REF = PullRequestRef(
    repository=RepositoryRef(owner="acme", name="api"), number=431, url="https://github.com/acme/api/pull/431"
)

_CONTEXT_NAME = "AIDA-MATE Context Agent"
_SECURITY_NAME = "AIDA-MATE Security Agent"
_CODE_NAME = "AIDA-MATE Code Agent"
_ARCHITECTURE_NAME = "AIDA-MATE Architecture Agent"
_TESTING_NAME = "AIDA-MATE Testing Agent"
_JUDGE_NAME = "AIDA-MATE Judge Agent"
_SPECIALIST_NAMES = [_SECURITY_NAME, _CODE_NAME, _ARCHITECTURE_NAME, _TESTING_NAME]


def _pull_request(**overrides) -> PullRequest:
    values = {
        "ref": REF,
        "title": "Add OAuth authentication",
        "body": "Implements Google OAuth login.",
        "base_ref": "main",
        "head_ref": "feature/oauth",
        "head_sha": "3f2c9ab",
        "changed_files": [
            ChangedFile(
                filename="app/auth/middleware.py",
                status=FileChangeStatus.MODIFIED,
                patch="@@ -1,3 +1,5 @@\n+import jwt\n",
            )
        ],
    }
    values.update(overrides)
    return PullRequest(**values)


class _FakeRunResult:
    def __init__(self, final_output):
        self.final_output = final_output

    def final_output_as(self, cls, raise_if_incorrect_type=False):
        if raise_if_incorrect_type and not isinstance(self.final_output, cls):
            raise TypeError(f"final_output is not a {cls.__name__}")
        return self.final_output


def _context_analysis(**overrides) -> PRContextAnalysis:
    values = {
        "summary": "Adds Google OAuth login handling.",
        "intent": "Let users sign in with Google.",
        "important_files": ["app/auth/middleware.py"],
        "affected_components": ["authentication"],
    }
    values.update(overrides)
    return PRContextAnalysis(**values)


def _analysis(**overrides) -> ReviewAnalysis:
    values = {
        "summary": "Adds OAuth login handling.",
        "findings": [
            Finding(
                category=Area.AUTHENTICATION,
                severity=Severity.HIGH,
                description="New JWT verification path added.",
                reason="Auth-critical code path.",
                file="app/auth/middleware.py",
                line=12,
            )
        ],
        "areas": [Area.AUTHENTICATION],
        "security_impact": True,
        "owasp_relevant": True,
    }
    values.update(overrides)
    return ReviewAnalysis(**values)


def _clean_analysis(**overrides) -> ReviewAnalysis:
    values = {
        "summary": "Nothing notable.",
        "findings": [],
        "areas": [],
        "security_impact": False,
        "owasp_relevant": False,
    }
    values.update(overrides)
    return ReviewAnalysis(**values)


@pytest.fixture
def runner() -> OpenAIAgentRunner:
    return OpenAIAgentRunner(model="gpt-5.6-sol")


class FakeSandbox:
    id = "fake-sandbox"


def _happy_path_fake_run(judge_output: ReviewAnalysis | None = None):
    """A fake `Runner.run` where every stage succeeds: Context, all four
    specialists (clean), and the Judge (`judge_output`, defaulting to a
    finding-bearing analysis)."""
    judge_output = judge_output or _analysis()

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(judge_output)
        return _FakeRunResult(_clean_analysis())

    return fake_run


# --- _build_input ---------------------------------------------------------


def test_build_input_includes_pr_metadata() -> None:
    text = _build_input(_pull_request())

    assert "acme/api" in text
    assert "#431" in text
    assert "Add OAuth authentication" in text
    assert "3f2c9ab" in text


def test_build_input_includes_the_description() -> None:
    text = _build_input(_pull_request(body="Fixes the login bug."))

    assert "Fixes the login bug." in text


def test_build_input_handles_a_missing_description() -> None:
    text = _build_input(_pull_request(body=None))

    assert "no description provided" in text


def test_build_input_includes_file_patches() -> None:
    text = _build_input(_pull_request())

    assert "app/auth/middleware.py" in text
    assert "import jwt" in text


def test_build_input_notes_missing_patches() -> None:
    files = [ChangedFile(filename="binary.png", status=FileChangeStatus.ADDED, patch=None)]
    text = _build_input(_pull_request(changed_files=files))

    assert "binary.png" in text
    assert "no inline diff available" in text


def test_build_input_caps_total_patch_size() -> None:
    # 11 files x 4,000 chars (the per-file cap) = 44,000 > the 40,000 total
    # budget, so at least the last file must be omitted rather than inlined.
    huge_files = [
        ChangedFile(filename=f"f{i}.py", status=FileChangeStatus.MODIFIED, patch="x" * 10_000)
        for i in range(11)
    ]
    text = _build_input(_pull_request(changed_files=huge_files))

    assert "omitted here to stay within budget" in text


def test_build_input_never_contains_a_sandbox_object_or_credential() -> None:
    """PR text belongs in the prompt; nothing sandbox- or credential-shaped does."""
    text = _build_input(_pull_request())

    for forbidden in ("sandbox", "token", "credential", "Bearer "):
        assert forbidden.lower() not in text.lower()


# --- _render_context_hint / _build_judge_input -----------------------------


def test_render_context_hint_summarizes_a_successful_context_run() -> None:
    hint = _render_context_hint(_context_analysis())

    assert "Adds Google OAuth login handling." in hint
    assert "Let users sign in with Google." in hint
    assert "app/auth/middleware.py" in hint
    assert "authentication" in hint


def test_render_context_hint_is_an_honest_placeholder_when_unavailable() -> None:
    hint = _render_context_hint(None)

    assert "unavailable" in hint.lower()


def test_build_judge_input_includes_each_survivor_labeled_by_name() -> None:
    successes = [
        _SpecialistOutcome(name="security", analysis=_analysis(), tool_calls_count=2, failed=False),
        _SpecialistOutcome(name="code", analysis=_clean_analysis(), tool_calls_count=0, failed=False),
    ]

    text = _build_judge_input(_context_analysis(), successes, [])

    assert "### security" in text
    assert "### code" in text
    assert "did not complete" not in text.lower()


def test_build_judge_input_names_the_specialists_that_did_not_complete() -> None:
    successes = [_SpecialistOutcome(name="security", analysis=_analysis(), tool_calls_count=1, failed=False)]

    text = _build_judge_input(_context_analysis(), successes, ["architecture", "testing"])

    assert "did not complete" in text.lower()
    assert "architecture" in text
    assert "testing" in text


def test_build_judge_input_does_not_re_inline_the_diff() -> None:
    """The Judge reconciles specialists' conclusions, not the raw diff again."""
    successes = [_SpecialistOutcome(name="security", analysis=_analysis(), tool_calls_count=1, failed=False)]

    text = _build_judge_input(_context_analysis(), successes, [])

    assert "```diff" not in text


# --- Construction: 6 agents, correct shapes --------------------------------


def test_context_agent_has_no_tools_and_outputs_pr_context_analysis(runner: OpenAIAgentRunner) -> None:
    assert list(runner._context_agent.tools) == []
    assert runner._context_agent.output_type is PRContextAnalysis


def test_judge_agent_has_no_tools_and_outputs_review_analysis(runner: OpenAIAgentRunner) -> None:
    assert list(runner._judge_agent.tools) == []
    assert runner._judge_agent.output_type is ReviewAnalysis


@pytest.mark.parametrize(
    "agent_attr",
    ["_security_agent", "_code_agent", "_architecture_agent", "_testing_agent"],
)
def test_each_specialist_has_exactly_the_three_read_only_sandbox_tools(
    runner: OpenAIAgentRunner, agent_attr: str
) -> None:
    agent = getattr(runner, agent_attr)
    tool_names = {tool.name for tool in agent.tools}

    assert tool_names == {"list_files", "read_file", "search_code"}
    assert agent.output_type is ReviewAnalysis


def test_context_agent_uses_the_utility_model_when_configured() -> None:
    runner = OpenAIAgentRunner(model="gpt-5.6-sol", utility_model="gpt-5.6-luna")

    assert runner._context_agent.model == "gpt-5.6-luna"
    assert runner._security_agent.model == "gpt-5.6-sol"
    assert runner._judge_agent.model == "gpt-5.6-sol"


def test_context_agent_falls_back_to_the_main_model_without_a_utility_model() -> None:
    runner = OpenAIAgentRunner(model="gpt-5.6-sol")

    assert runner._context_agent.model == "gpt-5.6-sol"


# --- analyze(): happy path --------------------------------------------------


async def test_analyze_returns_the_judges_final_output(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    judge_output = _analysis(summary="Judge synthesis of all specialists.")
    monkeypatch.setattr("app.agents.review_agent.Runner.run", _happy_path_fake_run(judge_output))

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.analysis is judge_output
    assert result.failed_specialists == []


async def test_analyze_reports_the_configured_model(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    monkeypatch.setattr("app.agents.review_agent.Runner.run", _happy_path_fake_run())

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.model == "gpt-5.6-sol"


async def test_analyze_aggregates_tool_calls_across_every_stage(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    """`tool_calls_count` sums each stage's own `SandboxToolContext`, not
    anything the fake `Runner.run` claims in its return value — proof this is
    wired from the counted side-channel, not self-reported output."""
    counts = {
        _CONTEXT_NAME: 1,
        _SECURITY_NAME: 2,
        _CODE_NAME: 3,
        _ARCHITECTURE_NAME: 0,
        _TESTING_NAME: 4,
        _JUDGE_NAME: 5,
    }

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        context.tool_calls_count = counts[agent.name]
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.tool_calls_count == sum(counts.values())


async def test_analyze_reports_zero_tool_calls_when_none_were_made(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    """Zero is a legitimate outcome for a trivial PR, not an error."""
    monkeypatch.setattr("app.agents.review_agent.Runner.run", _happy_path_fake_run())

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.tool_calls_count == 0


async def test_each_specialist_gets_its_own_sandbox_tool_context(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    contexts_by_agent = {}

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        contexts_by_agent[agent.name] = context
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    specialist_contexts = [contexts_by_agent[name] for name in _SPECIALIST_NAMES]
    assert len({id(ctx) for ctx in specialist_contexts}) == 4
    assert contexts_by_agent[_SECURITY_NAME].agent_name == "security"
    assert contexts_by_agent[_CODE_NAME].agent_name == "code"
    assert contexts_by_agent[_ARCHITECTURE_NAME].agent_name == "architecture"
    assert contexts_by_agent[_TESTING_NAME].agent_name == "testing"
    assert contexts_by_agent[_CONTEXT_NAME].agent_name == "context"
    assert contexts_by_agent[_JUDGE_NAME].agent_name == "judge"


async def test_specialists_run_concurrently_not_sequentially(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    """If the four specialists ran one after another, at most one would ever
    be in flight at a time. A real `await asyncio.sleep` inside each fake call
    lets the event loop interleave them — this only reaches 4 if `analyze()`
    genuinely launched them together via `asyncio.gather`."""
    concurrent = 0
    max_concurrent = 0

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        nonlocal concurrent, max_concurrent
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert max_concurrent == 4


async def test_analyze_passes_the_sandbox_only_through_context_never_the_prompt(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    captured_inputs = []
    captured_contexts = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        captured_inputs.append(input_text)
        captured_contexts.append(context)
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)
    sandbox = FakeSandbox()

    await runner.analyze(_pull_request(), sandbox, review_id="rev-1")

    assert all(ctx.sandbox is sandbox for ctx in captured_contexts)
    assert all("fake-sandbox" not in text for text in captured_inputs)
    assert all("FakeSandbox" not in text for text in captured_inputs)


async def test_analyze_passes_max_turns_to_every_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_max_turns = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        captured_max_turns.append(max_turns)
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)
    runner = OpenAIAgentRunner(model="gpt-5.6-sol", max_turns=7)

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    # Context + 4 specialists + Judge = 6 stages, all sharing the same budget.
    assert captured_max_turns == [7] * 6


# --- analyze(): partial-failure policy --------------------------------------


async def test_context_failure_is_recorded_but_non_fatal(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _CONTEXT_NAME:
            raise MaxTurnsExceeded("context timed out")
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.failed_specialists == ["context"]
    assert result.analysis is not None


async def test_context_failure_still_lets_specialists_run_with_a_placeholder_hint(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    specialist_inputs = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _CONTEXT_NAME:
            raise MaxTurnsExceeded("context timed out")
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        specialist_inputs.append(input_text)
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert len(specialist_inputs) == 4
    assert all("unavailable" in text.lower() for text in specialist_inputs)


async def test_one_specialist_failure_is_recorded_and_judge_still_runs_with_survivors(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    judge_inputs = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _CODE_NAME:
            raise MaxTurnsExceeded("code agent timed out")
        if agent.name == _JUDGE_NAME:
            judge_inputs.append(input_text)
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    result = await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert result.failed_specialists == ["code"]
    assert len(judge_inputs) == 1
    assert "code" in judge_inputs[0]
    assert judge_inputs[0].count("###") == 3  # security, architecture, testing survived


async def test_all_specialists_failing_raises_and_never_calls_the_judge(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    judge_called = False

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        nonlocal judge_called
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            judge_called = True
            return _FakeRunResult(_analysis())
        raise MaxTurnsExceeded("specialist timed out")

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    with pytest.raises(AgentError):
        await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert judge_called is False


async def test_judge_failure_is_always_fatal_even_when_every_specialist_succeeded(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            raise MaxTurnsExceeded("judge timed out")
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    with pytest.raises(AgentError):
        await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")


# --- SDK error / output-type translation (proven via the Judge stage,  ------
# --- which propagates unconditionally) --------------------------------------


async def test_max_turns_exceeded_becomes_agent_error(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _JUDGE_NAME:
            raise MaxTurnsExceeded("too many turns")
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    with pytest.raises(AgentError):
        await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")


async def test_model_behavior_error_becomes_agent_output_error(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _JUDGE_NAME:
            raise ModelBehaviorError("malformed output")
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    with pytest.raises(AgentOutputError):
        await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")


async def test_output_type_mismatch_raises_agent_output_error(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    """Belt-and-braces path: final_output_as itself rejects a mismatched type."""

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult("not-a-review-analysis")
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    with pytest.raises(AgentOutputError):
        await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")


# --- Custom base_url (organization gateway/proxy) --------------------------


def test_no_base_url_leaves_the_default_openai_client_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_set_client(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("app.agents.review_agent.set_default_openai_client", fake_set_client)

    OpenAIAgentRunner(model="gpt-5.6-sol")

    assert called is False


def test_base_url_installs_a_custom_client_with_tracing_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_set_client(client, *, use_for_tracing):
        captured["client"] = client
        captured["use_for_tracing"] = use_for_tracing

    monkeypatch.setattr("app.agents.review_agent.set_default_openai_client", fake_set_client)

    OpenAIAgentRunner(model="gpt-5.6-sol", api_key="org-key", base_url="https://gateway.acme.internal/v1")

    assert str(captured["client"].base_url) == "https://gateway.acme.internal/v1/"
    assert captured["use_for_tracing"] is False
    assert not isinstance(captured["client"], AsyncAzureOpenAI)


def test_api_version_installs_the_azure_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure OpenAI / Azure AI Foundry rejects requests missing `api-version`."""
    captured = {}
    monkeypatch.setattr(
        "app.agents.review_agent.set_default_openai_client",
        lambda client, *, use_for_tracing: captured.update(client=client, use_for_tracing=use_for_tracing),
    )

    OpenAIAgentRunner(
        model="gpt-5.6-sol",
        api_key="org-key",
        base_url="https://acme.services.ai.azure.com/api/projects/acme",
        api_version="2025-01-01-preview",
    )

    assert isinstance(captured["client"], AsyncAzureOpenAI)
    assert captured["client"]._api_version == "2025-01-01-preview"
    assert captured["use_for_tracing"] is False


async def test_base_url_disables_tracing_for_every_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agents.review_agent.set_default_openai_client", lambda *a, **k: None)
    run_configs = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        run_configs.append(kwargs.get("run_config"))
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)
    runner = OpenAIAgentRunner(model="gpt-5.6-sol", base_url="https://gateway.acme.internal/v1")

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert len(run_configs) == 6
    assert all(rc is not None and rc.tracing_disabled is True for rc in run_configs)


async def test_without_base_url_tracing_is_left_at_its_default_for_every_stage(
    monkeypatch: pytest.MonkeyPatch, runner: OpenAIAgentRunner
) -> None:
    run_configs = []

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        run_configs.append(kwargs.get("run_config"))
        if agent.name == _CONTEXT_NAME:
            return _FakeRunResult(_context_analysis())
        if agent.name == _JUDGE_NAME:
            return _FakeRunResult(_analysis())
        return _FakeRunResult(_clean_analysis())

    monkeypatch.setattr("app.agents.review_agent.Runner.run", fake_run)

    await runner.analyze(_pull_request(), FakeSandbox(), review_id="rev-1")

    assert len(run_configs) == 6
    assert all(rc is None for rc in run_configs)


# --- Prompts -----------------------------------------------------------------


def test_all_six_prompts_are_distinct_and_share_the_untrusted_input_preamble() -> None:
    prompts = [
        CONTEXT_SYSTEM_PROMPT,
        SECURITY_SYSTEM_PROMPT,
        CODE_SYSTEM_PROMPT,
        ARCHITECTURE_SYSTEM_PROMPT,
        TESTING_SYSTEM_PROMPT,
        JUDGE_SYSTEM_PROMPT,
    ]

    assert len(set(prompts)) == 6
    for prompt in prompts:
        assert UNTRUSTED_INPUT_PREAMBLE in prompt


@pytest.mark.parametrize(
    "prompt",
    [
        SECURITY_SYSTEM_PROMPT,
        CODE_SYSTEM_PROMPT,
        ARCHITECTURE_SYSTEM_PROMPT,
        TESTING_SYSTEM_PROMPT,
        JUDGE_SYSTEM_PROMPT,
    ],
)
def test_specialist_and_judge_prompts_forbid_deciding_risk(prompt: str) -> None:
    assert "risk level" in prompt.lower()


def test_context_prompt_states_it_does_not_decide_risk() -> None:
    assert "do not decide risk" in CONTEXT_SYSTEM_PROMPT.lower()


def test_judge_prompt_states_it_has_no_final_risk_authority() -> None:
    assert "no authority over the final risk level" in JUDGE_SYSTEM_PROMPT.lower()


def test_judge_prompt_asks_for_reconciliation_not_reinvestigation() -> None:
    assert "reconciliation" in JUDGE_SYSTEM_PROMPT.lower()
