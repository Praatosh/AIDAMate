"""Agent tools that inspect a sandboxed repository checkout.

Each tool is a thin wrapper over `ISandbox`, reached through the OpenAI Agents
SDK's `RunContextWrapper` — never through the prompt string, so the sandbox
handle itself is not something the model can see or reason about, only use.

None of `list_files`/`read_file`/`search_code` reach a shell any more: all
three call typed `ISandbox` methods (`find_files`/`read_file`/`grep_files`)
that each backend implements natively against its own workspace directory.
Earlier versions of `list_files`/`search_code` built `find`/`grep` shell
command *strings* (`sh -c "find ... | head ..."`, with `search_code`'s
`pattern`/`path` `shlex.quote()`'d before being embedded) and ran them via
`ISandbox.exec()` — that indirection, and the escaping it required, is gone
now that the arguments are passed as plain typed values instead of
reconstructed from a string.

`_scoped_path` still rejects any `path` that would resolve outside
`repo_dir` (a `..`-climbing or absolute path) before it ever reaches a
sandbox call — this is the one place both `LocalSandbox` and `SbxSandbox`
share, so the check belongs here rather than duplicated per backend. Found
by a security audit: `LocalSandbox`'s `find`/`grep` implementations run
directly on the host filesystem and did not enforce this on their own (only
`read_file`/`upload_bytes` did, via `ISandbox._resolve_workspace_path`) — an
attacker-controlled PR file whose content induced an agent to call
`list_files`/`search_code` with a traversal path could have read arbitrary
host files (e.g. `LINEAR_TOKEN_STORE_PATH`'s plaintext OAuth tokens) and had
the result published, via a `Finding`, straight into the public GitHub PR
comment. `LocalSandbox` now also enforces its own boundary independently
(see its module docstring) — this fix and that one are deliberately
redundant, not either/or, given what leaking through this path would expose.

Each `@function_tool`-decorated function is a thin wrapper around a plain
`_..._impl` async function. The split exists so tests can exercise the actual
logic directly — the SDK's `FunctionTool.on_invoke_tool` requires internal
`ToolContext` machinery (`tool_name`, `tool_call_id`, run tracing state) that
isn't worth depending on for a unit test of "does this command get built and
scoped correctly."
"""

import posixpath
from dataclasses import dataclass

from agents import RunContextWrapper, function_tool

from app.core.events import AGENT_TOOL_CALL
from app.core.interfaces import ISandbox
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Root of the extracted PR tree inside the sandbox workspace. Matches the
#: directory `orchestrator.py` extracts the downloaded archive into.
SANDBOX_REPO_DIR = "repo"

#: Bounds on tool output so one call cannot exhaust the agent's context.
_MAX_LIST_FILES = 500
_MAX_LIST_DEPTH = 4
_MAX_SEARCH_MATCHES = 200
_DEFAULT_READ_BYTES = 20_000


@dataclass
class SandboxToolContext:
    """Request-scoped state the tools close over. Never serialized into a prompt."""

    sandbox: ISandbox
    repo_dir: str = SANDBOX_REPO_DIR
    #: For log correlation only (AGENT_TOOL_CALL) — never read by the tools
    #: themselves, and never reaches the prompt.
    review_id: str | None = None
    #: Which specialist this context belongs to (e.g. "security", "code"), for
    #: the same log-correlation reason as `review_id`. Each specialist in the
    #: multi-agent pipeline gets its own `SandboxToolContext` rather than
    #: sharing one — see `review_agent.py` — so this is unambiguous even
    #: though several specialists run concurrently.
    agent_name: str | None = None
    #: Counted here, not self-reported by the model: this is what lets
    #: `OpenAIAgentRunner.analyze()` report how many tool calls actually
    #: happened without trusting anything the LLM says about its own behavior.
    tool_calls_count: int = 0


class PathEscapesRepositoryError(ValueError):
    """Raised when a tool-supplied path would resolve outside the repo root."""


def _scoped_path(repo_dir: str, path: str) -> str:
    """Join a tool-supplied relative path onto the repo root.

    `path` is caller-controlled (ultimately model-controlled).
    `_validated_relative_path` rejects any path that would lexically resolve
    outside `repo_dir` — a `..`-climbing or absolute path — computed with
    `posixpath.normpath` against the *string*, not the real filesystem, so
    the check is identical regardless of which sandbox backend eventually
    handles the call. No shell-quoting step: the result is passed to a typed
    `ISandbox` method (`find_files`/`grep_files`) as a plain argument, never
    reconstructed into a shell command string.
    """
    cleaned = _validated_relative_path(repo_dir, path)
    return f"{repo_dir}/{cleaned}"


def _validated_relative_path(repo_dir: str, path: str) -> str:
    """Return `path` cleaned, after confirming it stays within `repo_dir`.

    Shared by all three tools — `_scoped_path` additionally joins it onto
    `repo_dir` for `find_files`/`grep_files`; `_read_file_impl` uses this
    directly since it builds that same join itself for `ISandbox.read_file`.
    """
    cleaned = path.strip() or "."
    joined = posixpath.normpath(posixpath.join(repo_dir, cleaned))
    if joined != repo_dir and not joined.startswith(f"{repo_dir}/"):
        raise PathEscapesRepositoryError(path)
    return cleaned


async def _list_files_impl(sandbox: ISandbox, repo_dir: str, path: str) -> str:
    """List files under `path` (relative to the repository root)."""
    try:
        target = _scoped_path(repo_dir, path)
    except PathEscapesRepositoryError:
        return f"'{path}' is outside the repository root."
    result = await sandbox.find_files(target, max_depth=_MAX_LIST_DEPTH, limit=_MAX_LIST_FILES)

    if result.exit_code != 0:
        return f"Could not list '{path}': {result.stderr.strip() or 'unknown error'}"
    return result.stdout.strip() or f"No files found under '{path}'."


async def _read_file_impl(sandbox: ISandbox, repo_dir: str, path: str, max_bytes: int) -> str:
    """Read a file's contents (relative to the repository root), truncated to `max_bytes`."""
    try:
        cleaned = _validated_relative_path(repo_dir, path)
    except PathEscapesRepositoryError:
        return f"'{path}' is outside the repository root."
    target = f"{repo_dir}/{cleaned}"
    content = await sandbox.read_file(target, max_bytes=max_bytes)
    return content or f"'{path}' is empty or does not exist."


async def _search_code_impl(sandbox: ISandbox, repo_dir: str, pattern: str, path: str) -> str:
    """Search for `pattern` (a literal string, not a regex) under `path`."""
    try:
        target = _scoped_path(repo_dir, path)
    except PathEscapesRepositoryError:
        return f"'{path}' is outside the repository root."
    result = await sandbox.grep_files(pattern, target, limit=_MAX_SEARCH_MATCHES)

    # grep exits 1 for "no matches" — that is a normal, non-error result, not
    # a tool failure. Only exit codes >= 2 indicate something actually broke.
    if result.exit_code >= 2:
        return f"Search failed: {result.stderr.strip() or 'unknown error'}"
    return result.stdout.strip() or f"No matches for {pattern!r} under '{path}'."


def _log_tool_call(ctx: RunContextWrapper[SandboxToolContext], tool: str, **args: object) -> None:
    """Record that the agent actually invoked a tool, with what it asked for.

    This is the concrete, checkable evidence that the model is reasoning
    over real repository content — not the deterministic path scan, and not
    a claim taken on faith. The count is read back by `OpenAIAgentRunner`
    after the run and published in `ReviewResult.tool_calls_count`.
    """
    ctx.context.tool_calls_count += 1
    logger.info(
        AGENT_TOOL_CALL,
        extra={
            "review_id": ctx.context.review_id,
            "agent": ctx.context.agent_name,
            "tool": tool,
            **args,
        },
    )


@function_tool
async def list_files(ctx: RunContextWrapper[SandboxToolContext], path: str = ".") -> str:
    """List files under `path` (relative to the repository root) in the sandbox.

    Use this to orient yourself in an unfamiliar part of the tree before
    deciding what to read in detail — not as a substitute for the diff you
    were already given, which already shows what changed.
    """
    _log_tool_call(ctx, "list_files", path=path)
    return await _list_files_impl(ctx.context.sandbox, ctx.context.repo_dir, path)


@function_tool
async def read_file(
    ctx: RunContextWrapper[SandboxToolContext], path: str, max_bytes: int = _DEFAULT_READ_BYTES
) -> str:
    """Read a file's contents (relative to the repository root), truncated to `max_bytes`."""
    _log_tool_call(ctx, "read_file", path=path)
    return await _read_file_impl(ctx.context.sandbox, ctx.context.repo_dir, path, max_bytes)


@function_tool
async def search_code(
    ctx: RunContextWrapper[SandboxToolContext], pattern: str, path: str = "."
) -> str:
    """Search for `pattern` (a literal string, not a regex) under `path` in the repository.

    Use this to check whether a concerning pattern you noticed in the diff is
    isolated or repeated elsewhere in the codebase.
    """
    _log_tool_call(ctx, "search_code", pattern=pattern, path=path)
    return await _search_code_impl(ctx.context.sandbox, ctx.context.repo_dir, pattern, path)
