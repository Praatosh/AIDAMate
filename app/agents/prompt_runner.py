"""OpenAI Agents SDK runner for scheduled prompts. See CLAUDE.md §1d.

Distinct from `review_agent.py`'s `OpenAIAgentRunner`: this is **one** agent,
no Context/specialists/Judge, plain markdown text output rather than a
`ReviewAnalysis`. A scheduled prompt has no PR, no diff, and nothing to feed a
risk engine — it exists to run one freeform, human-described task against a
repo snapshot and hand back readable text, so the multi-agent reconciliation
machinery `review_agent.py` needs has nothing to reconcile here.

The Azure/OpenAI client-selection block below is copied from
`OpenAIAgentRunner.__init__`, not shared via a base class — the two runners'
agent sets are shaped too differently (one Agent vs. six) for a shared
constructor to pull its weight over just repeating four lines.
"""

import asyncio

from agents import Agent, ModelSettings, RunConfig, Runner, set_default_openai_client
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.core.errors import AgentError, AgentOutputError
from app.core.interfaces import ISandbox
from app.prompts.scheduled_prompt import SCHEDULED_PROMPT_SYSTEM_PROMPT
from app.tools.sandbox_tools import SandboxToolContext, list_files, read_file, search_code


class ScheduledPromptRunner:
    """Runs one freeform prompt against a prepared sandbox, returning markdown text."""

    def __init__(
        self,
        *,
        model: str,
        max_turns: int = 15,
        reasoning_effort: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
    ) -> None:
        # See OpenAIAgentRunner.__init__ (review_agent.py) for why this
        # selection exists: a custom `base_url` is set once, globally, since
        # the SDK has no per-`Runner.run()` way to select a client, and
        # `api_version` selects the Azure client specifically because Azure
        # OpenAI / Azure AI Foundry endpoints require the `api-version` query
        # parameter that only `AsyncAzureOpenAI` adds automatically.
        # Tied to `api_version` as well as `base_url`: an Azure/Azure AI
        # Foundry key generally can't authenticate against OpenAI's own
        # tracing-upload endpoint any more than a plain gateway key can, so
        # tracing must stay disabled whenever the Azure branch is taken —
        # independent of whether `base_url is not None` alone would already
        # imply it (Settings' own validator requires the two go together in
        # practice, but this stays correct even for a runner constructed
        # directly, e.g. in a test, with `api_version` set and `base_url` None).
        self._tracing_disabled = base_url is not None or api_version is not None
        if api_version:
            client = AsyncAzureOpenAI(base_url=base_url, api_key=api_key, api_version=api_version)
            set_default_openai_client(client, use_for_tracing=False)
        elif base_url is not None:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            set_default_openai_client(client, use_for_tracing=False)

        # Same reasoning-model accommodation as OpenAIAgentRunner: a reasoning
        # model generally rejects an explicit `temperature` alongside
        # `reasoning.effort`, so `temperature` is only set when there's no
        # reasoning effort configured. Found live: gpt-5.6-terra with
        # MODEL_REASONING_EFFORT=low rejected `temperature=0.0` with a 400
        # ("Unsupported parameter: 'temperature'") the first time this ran
        # against a real deployment — the unit suite's fakes never call a
        # real model, so this never surfaced there.
        model_settings = ModelSettings(
            temperature=0.0 if not reasoning_effort else None,
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            verbosity="low" if reasoning_effort else None,
            parallel_tool_calls=True if reasoning_effort else None,
        )
        self._agent = Agent(
            name="AIDA-MATE Scheduled Prompt Agent",
            instructions=SCHEDULED_PROMPT_SYSTEM_PROMPT,
            model=model,
            tools=[list_files, read_file, search_code],
            model_settings=model_settings,
            # No `output_type` — a scheduled prompt's result is freeform
            # markdown for a human reader, not a schema `risk_engine.py`
            # (or anything else) parses. `result.final_output` is a plain str.
        )
        self._max_turns = max_turns

    async def run(self, prompt: str, sandbox: ISandbox, *, timeout_s: float | None, review_id: str) -> str:
        """Run `prompt` against `sandbox`'s repo checkout, returning the agent's markdown output.

        Raises:
            AgentError: the run failed, timed out, or exceeded its turn budget.
            AgentOutputError: the SDK could not coerce a final text output.
        """
        context = SandboxToolContext(sandbox=sandbox, review_id=review_id, agent_name="scheduled_prompt")
        run_config = RunConfig(tracing_disabled=True) if self._tracing_disabled else None
        coro = Runner.run(
            self._agent, prompt, context=context, max_turns=self._max_turns, run_config=run_config
        )
        try:
            if timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                result = await coro
        except TimeoutError as exc:
            raise AgentError(f"{self._agent.name} exceeded {timeout_s}s") from exc
        except MaxTurnsExceeded as exc:
            raise AgentError(f"{self._agent.name} exceeded {self._max_turns} turns") from exc
        except ModelBehaviorError as exc:
            raise AgentOutputError(
                f"{self._agent.name} output did not match the expected schema: {exc}"
            ) from exc
        except AgentsException as exc:
            raise AgentError(f"{self._agent.name} failed: {type(exc).__name__}: {exc}") from exc

        output = result.final_output
        if not isinstance(output, str):
            raise AgentOutputError(f"{self._agent.name} final_output was not text: {type(output).__name__}")
        return output
