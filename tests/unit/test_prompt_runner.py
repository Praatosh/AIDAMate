"""ScheduledPromptRunner: single-agent construction and SDK error translation.

`agents.Runner.run` is monkeypatched throughout — no real OpenAI API key or
network call is needed, same seam `test_review_agent.py` uses for the
multi-agent runner.
"""

import pytest
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from openai import AsyncAzureOpenAI

from app.agents.prompt_runner import ScheduledPromptRunner
from app.core.errors import AgentError, AgentOutputError
from app.prompts.review_prompt import UNTRUSTED_INPUT_PREAMBLE
from app.prompts.scheduled_prompt import SCHEDULED_PROMPT_SYSTEM_PROMPT


class _FakeRunResult:
    def __init__(self, final_output):
        self.final_output = final_output


class FakeSandbox:
    id = "fake-sandbox"


@pytest.fixture
def runner() -> ScheduledPromptRunner:
    return ScheduledPromptRunner(model="gpt-5.6-sol")


# --- Construction ------------------------------------------------------------


def test_agent_has_exactly_the_three_read_only_sandbox_tools(runner: ScheduledPromptRunner) -> None:
    tool_names = {tool.name for tool in runner._agent.tools}
    assert tool_names == {"list_files", "read_file", "search_code"}


def test_agent_has_no_output_type(runner: ScheduledPromptRunner) -> None:
    """Freeform markdown, not a schema — `result.final_output` must stay a plain str."""
    assert runner._agent.output_type is None


def test_temperature_is_set_when_no_reasoning_effort_is_configured(runner: ScheduledPromptRunner) -> None:
    assert runner._agent.model_settings.temperature == 0.0
    assert runner._agent.model_settings.reasoning is None


def test_reasoning_effort_is_used_instead_of_temperature() -> None:
    """Regression: found live against a real reasoning-model deployment
    (gpt-5.6-terra with MODEL_REASONING_EFFORT=low) — the model rejected an
    explicit `temperature` with a 400 ('Unsupported parameter: temperature'),
    the same accommodation `OpenAIAgentRunner` already makes."""
    runner = ScheduledPromptRunner(model="gpt-5.6-terra", reasoning_effort="low")

    assert runner._agent.model_settings.temperature is None
    assert runner._agent.model_settings.reasoning.effort == "low"


# --- run(): happy path ---------------------------------------------------------


async def test_run_returns_the_agents_text_output(
    monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        return _FakeRunResult("## Findings\nNothing concerning.")

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)

    result = await runner.run("Audit for secrets", FakeSandbox(), timeout_s=30.0, review_id="sched-1")

    assert result == "## Findings\nNothing concerning."


async def test_run_passes_the_sandbox_through_context_never_the_prompt(
    monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner
) -> None:
    captured = {}

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        captured["input_text"] = input_text
        captured["context"] = context
        return _FakeRunResult("done")

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)
    sandbox = FakeSandbox()

    await runner.run("Audit for secrets", sandbox, timeout_s=30.0, review_id="sched-1")

    assert captured["context"].sandbox is sandbox
    assert "fake-sandbox" not in captured["input_text"]


async def test_run_times_out(monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner) -> None:
    import asyncio

    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        await asyncio.sleep(10)
        return _FakeRunResult("too slow")

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)

    with pytest.raises(AgentError):
        await runner.run("Audit for secrets", FakeSandbox(), timeout_s=0.01, review_id="sched-1")


# --- run(): SDK error / output-type translation -------------------------------


async def test_max_turns_exceeded_becomes_agent_error(
    monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        raise MaxTurnsExceeded("too many turns")

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)

    with pytest.raises(AgentError):
        await runner.run("Audit for secrets", FakeSandbox(), timeout_s=30.0, review_id="sched-1")


async def test_model_behavior_error_becomes_agent_output_error(
    monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        raise ModelBehaviorError("malformed output")

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)

    with pytest.raises(AgentOutputError):
        await runner.run("Audit for secrets", FakeSandbox(), timeout_s=30.0, review_id="sched-1")


async def test_non_text_output_raises_agent_output_error(
    monkeypatch: pytest.MonkeyPatch, runner: ScheduledPromptRunner
) -> None:
    async def fake_run(agent, input_text, *, context=None, max_turns=None, **kwargs):
        return _FakeRunResult({"not": "text"})

    monkeypatch.setattr("app.agents.prompt_runner.Runner.run", fake_run)

    with pytest.raises(AgentOutputError):
        await runner.run("Audit for secrets", FakeSandbox(), timeout_s=30.0, review_id="sched-1")


# --- Custom base_url / Azure client selection ---------------------------------


def test_no_base_url_leaves_the_default_openai_client_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_set_client(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("app.agents.prompt_runner.set_default_openai_client", fake_set_client)

    ScheduledPromptRunner(model="gpt-5.6-sol")

    assert called is False


def test_api_version_installs_the_azure_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "app.agents.prompt_runner.set_default_openai_client",
        lambda client, *, use_for_tracing: captured.update(client=client, use_for_tracing=use_for_tracing),
    )

    ScheduledPromptRunner(
        model="gpt-5.6-sol",
        api_key="org-key",
        base_url="https://acme.services.ai.azure.com/api/projects/acme",
        api_version="2025-01-01-preview",
    )

    assert isinstance(captured["client"], AsyncAzureOpenAI)
    assert captured["use_for_tracing"] is False


# --- Prompt --------------------------------------------------------------------


def test_prompt_shares_the_untrusted_input_preamble() -> None:
    assert UNTRUSTED_INPUT_PREAMBLE in SCHEDULED_PROMPT_SYSTEM_PROMPT
