"""Agent tools over a sandbox: path scoping and typed find/grep semantics.

Tests exercise `_list_files_impl`/`_read_file_impl`/`_search_code_impl`
directly rather than going through the `@function_tool`-decorated wrappers'
`on_invoke_tool` — that SDK entry point requires internal `ToolContext`
machinery (tool_name, tool_call_id, run tracing state) that isn't worth
depending on to test "does this command get built and scoped correctly."
`list_files`/`read_file`/`search_code` themselves are one-line delegations to
these impls, covered implicitly.

None of these tools reach a shell any more — `find_files`/`grep_files` are
typed `ISandbox` methods taking plain arguments (a path, a pattern, a
depth/limit), not commands assembled into a string. `FakeSandbox` mirrors
that: it records calls to those methods directly rather than a command string
a shell would have parsed.
"""

from dataclasses import dataclass

import pytest

from app.tools.sandbox_tools import (
    SANDBOX_REPO_DIR,
    PathEscapesRepositoryError,
    _list_files_impl,
    _read_file_impl,
    _scoped_path,
    _search_code_impl,
    _validated_relative_path,
)


@dataclass
class _ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class FakeSandbox:
    """Records find_files()/grep_files()/read_file() calls and returns scripted results."""

    id = "fake-sandbox"

    def __init__(self) -> None:
        self.find_calls: list[dict] = []
        self.grep_calls: list[dict] = []
        self.read_calls: list[str] = []
        self._find_result = _ExecResult(0)
        self._grep_result = _ExecResult(0)
        self._read_result = ""

    def script_find(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self._find_result = _ExecResult(exit_code, stdout, stderr)

    def script_grep(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self._grep_result = _ExecResult(exit_code, stdout, stderr)

    def script_read(self, content: str) -> None:
        self._read_result = content

    async def find_files(self, path: str, *, max_depth: int, limit: int):
        self.find_calls.append({"path": path, "max_depth": max_depth, "limit": limit})
        return self._find_result

    async def grep_files(self, pattern: str, path: str, *, limit: int):
        self.grep_calls.append({"pattern": pattern, "path": path, "limit": limit})
        return self._grep_result

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        self.read_calls.append(path)
        return self._read_result

    async def upload_bytes(self, dest_path, content):
        raise NotImplementedError

    async def destroy(self):
        raise NotImplementedError


# --- _scoped_path -----------------------------------------------------------


def test_scoped_path_joins_repo_dir_and_path() -> None:
    assert _scoped_path("repo", "app/main.py") == "repo app/main.py".replace(" ", "/")


def test_scoped_path_defaults_blank_to_dot() -> None:
    assert _scoped_path("repo", "  ") == "repo/."


def test_scoped_path_does_not_alter_shell_metacharacters() -> None:
    """No quoting step any more — `find_files`/`grep_files` take this as a
    plain typed argument, never a shell command string, so there is nothing
    to escape and nothing that would corrupt a legitimately unusual path."""
    assert _scoped_path("repo", "$(whoami)") == "repo/$(whoami)"


@pytest.mark.parametrize(
    "escaping_path",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "a/../../b",
        "/etc/passwd",
        "/../etc/passwd",
        "../repository_sibling/x",  # sibling dir sharing a string prefix with "repo", not a subdirectory
    ],
)
def test_scoped_path_rejects_paths_that_escape_the_repo_root(escaping_path: str) -> None:
    """Security-audit finding: a `..`-climbing or absolute path must never
    reach a shell command at all — not just be quoted safely within one.
    `shlex.quote` alone (the previous, sole defense) protects against shell
    metacharacters, not against a legitimately-quoted `../../etc/passwd`.
    The sibling-dir case guards against a naive `startswith(repo_dir)` check
    (without the trailing `/`) falsely treating "repository_sibling" as
    inside "repo" purely because of the shared string prefix."""
    with pytest.raises(PathEscapesRepositoryError):
        _scoped_path("repo", escaping_path)


@pytest.mark.parametrize(
    "safe_path",
    [".", "app/main.py", "a/../b", "repo_sibling", "repository_named_subdir"],
)
def test_scoped_path_accepts_paths_that_stay_within_the_repo_root(safe_path: str) -> None:
    """Paths that merely *contain* '..' but still resolve inside the repo
    root, or whose *name* happens to contain "repo" as a substring while
    still being a genuine subdirectory of it, must not be falsely rejected."""
    _scoped_path("repo", safe_path)  # must not raise


def test_validated_relative_path_returns_the_cleaned_path() -> None:
    assert _validated_relative_path("repo", "app/main.py") == "app/main.py"


def test_validated_relative_path_rejects_escapes() -> None:
    with pytest.raises(PathEscapesRepositoryError):
        _validated_relative_path("repo", "../../etc/passwd")


# --- list_files ---------------------------------------------------------------


async def test_list_files_scopes_the_call_to_the_repo_dir() -> None:
    sandbox = FakeSandbox()
    sandbox.script_find(0, "repo/a.py\nrepo/b.py\n")

    result = await _list_files_impl(sandbox, SANDBOX_REPO_DIR, ".")

    assert sandbox.find_calls[0]["path"] == "repo/."
    assert result == "repo/a.py\nrepo/b.py"


async def test_list_files_reports_the_error_on_failure() -> None:
    sandbox = FakeSandbox()
    sandbox.script_find(1, "", "no such directory")

    result = await _list_files_impl(sandbox, SANDBOX_REPO_DIR, "nope")

    assert "no such directory" in result


async def test_list_files_reports_empty_result_clearly() -> None:
    sandbox = FakeSandbox()
    sandbox.script_find(0, "")

    result = await _list_files_impl(sandbox, SANDBOX_REPO_DIR, "empty")

    assert "No files found" in result


async def test_list_files_passes_the_depth_and_count_bounds_through() -> None:
    sandbox = FakeSandbox()
    sandbox.script_find(0, "")

    await _list_files_impl(sandbox, SANDBOX_REPO_DIR, ".")

    assert sandbox.find_calls[0]["max_depth"] == 4
    assert sandbox.find_calls[0]["limit"] == 500


# --- read_file ------------------------------------------------------------


async def test_read_file_delegates_to_sandbox_read_file() -> None:
    sandbox = FakeSandbox()
    sandbox.script_read("print('hello')")

    result = await _read_file_impl(sandbox, SANDBOX_REPO_DIR, "app/main.py", 20_000)

    assert result == "print('hello')"
    assert sandbox.read_calls == ["repo/app/main.py"]


async def test_read_file_rejects_an_absolute_looking_path() -> None:
    """A model that supplies an absolute-looking path must not escape the repo
    root. Previously this was silently reinterpreted as relative (stripping
    one leading slash); now it's rejected outright — `read_file`'s own
    docstring promises "relative to the repository root," and a genuinely
    relative path from the model would never start with '/' in the first
    place, so there's no legitimate case this rejection could break."""
    sandbox = FakeSandbox()

    result = await _read_file_impl(sandbox, SANDBOX_REPO_DIR, "/etc/passwd", 20_000)

    assert sandbox.read_calls == []
    assert "outside the repository root" in result


async def test_read_file_reports_missing_files_clearly() -> None:
    sandbox = FakeSandbox()
    sandbox.script_read("")

    result = await _read_file_impl(sandbox, SANDBOX_REPO_DIR, "ghost.py", 20_000)

    assert "empty or does not exist" in result


async def test_read_file_passes_max_bytes_through() -> None:
    sandbox = FakeSandbox()
    sandbox.script_read("x" * 100)
    calls: list[int] = []

    async def tracking_read_file(path, *, max_bytes=200_000):
        calls.append(max_bytes)
        return sandbox._read_result

    sandbox.read_file = tracking_read_file

    await _read_file_impl(sandbox, SANDBOX_REPO_DIR, "big.txt", 500)

    assert calls == [500]


# --- search_code ------------------------------------------------------------


async def test_search_code_passes_an_unusual_pattern_through_verbatim() -> None:
    """A pattern with quotes/semicolons/etc. used to be the one LLM-controlled
    input reaching a shell string, and had to be `shlex.quote()`'d to avoid
    breaking out. `grep_files` takes `pattern` as a plain typed argument now
    — there is no shell to break out of, so it should reach the sandbox call
    completely unchanged, not re-encoded or escaped."""
    sandbox = FakeSandbox()
    sandbox.script_grep(0, "")

    unusual_pattern = "x'; rm -rf / #"
    await _search_code_impl(sandbox, SANDBOX_REPO_DIR, unusual_pattern, ".")

    assert sandbox.grep_calls[0]["pattern"] == unusual_pattern


async def test_search_code_scopes_the_call_to_the_repo_dir() -> None:
    sandbox = FakeSandbox()
    sandbox.script_grep(0, "repo/a.py:3:token")

    await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "token", ".")

    assert sandbox.grep_calls[0]["path"] == "repo/."
    assert sandbox.grep_calls[0]["limit"] == 200


async def test_search_code_treats_exit_code_1_as_no_matches_not_an_error() -> None:
    """grep returns 1 for 'nothing found' — that's a normal result, not a tool failure."""
    sandbox = FakeSandbox()
    sandbox.script_grep(1, "")

    result = await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "nonexistent_xyz", ".")

    assert "No matches" in result


async def test_search_code_treats_exit_code_2_as_a_real_failure() -> None:
    sandbox = FakeSandbox()
    sandbox.script_grep(2, "", "grep: invalid option")

    result = await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "x", ".")

    assert "Search failed" in result


async def test_search_code_returns_matches() -> None:
    sandbox = FakeSandbox()
    sandbox.script_grep(0, "repo/auth.py:10:password == input_password")

    result = await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "password ==", ".")

    assert "repo/auth.py:10" in result


@pytest.mark.parametrize("unusual_path", ["$(whoami)", "`id`", "a; rm -rf /"])
async def test_search_code_path_with_shell_metacharacters_passes_through_unchanged(
    unusual_path: str,
) -> None:
    """These particular strings don't climb outside the repo root lexically
    (no `..`), so they pass the traversal check and reach `grep_files` as a
    plain argument — with no shell downstream, there's nothing to escape and
    nothing that could reinterpret them as a second command."""
    sandbox = FakeSandbox()
    sandbox.script_grep(0, "")

    await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "x", unusual_path)

    assert sandbox.grep_calls[0]["path"] == f"repo/{unusual_path}"


async def test_search_code_traversal_path_is_rejected() -> None:
    """Security-audit finding: a `..`-climbing `path` must never reach `grep`
    at all. Previously this was passed through as a literal (safely quoted,
    but still an actual traversal argument) — on `LocalSandbox`, whose
    `find`/`grep` run directly on the host filesystem, that meant a crafted
    PR could make `search_code` read arbitrary host files, with the result
    published verbatim into a public PR comment via a `Finding`."""
    sandbox = FakeSandbox()

    result = await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "x", "../../etc/passwd")

    assert sandbox.grep_calls == []
    assert "outside the repository root" in result


# --- Tool-call counting --------------------------------------------------------
#
# `tool_calls_count` is what lets a review's published metadata prove real
# tool use rather than take it on faith — see `AgentRunOutcome`'s docstring.
# Tested against `_log_tool_call` directly rather than through the
# `@function_tool`-decorated wrappers, for the same SDK-internals reason the
# module docstring gives for testing the `_impl` functions directly.


async def test_a_tool_call_increments_the_counter() -> None:
    from agents import RunContextWrapper

    from app.tools.sandbox_tools import SandboxToolContext, _log_tool_call

    ctx = RunContextWrapper(context=SandboxToolContext(sandbox=FakeSandbox(), review_id="rev-1"))

    _log_tool_call(ctx, "read_file", path="app/main.py")

    assert ctx.context.tool_calls_count == 1


async def test_multiple_tool_calls_accumulate() -> None:
    from agents import RunContextWrapper

    from app.tools.sandbox_tools import SandboxToolContext, _log_tool_call

    ctx = RunContextWrapper(context=SandboxToolContext(sandbox=FakeSandbox(), review_id="rev-1"))

    _log_tool_call(ctx, "list_files", path=".")
    _log_tool_call(ctx, "read_file", path="app/main.py")
    _log_tool_call(ctx, "search_code", pattern="fetchUser", path=".")

    assert ctx.context.tool_calls_count == 3


async def test_no_tool_calls_leaves_the_counter_at_zero() -> None:
    """A trivial PR needing no investigation is legitimate, not a defect."""
    from app.tools.sandbox_tools import SandboxToolContext

    context = SandboxToolContext(sandbox=FakeSandbox(), review_id="rev-1")

    assert context.tool_calls_count == 0


async def test_logged_tool_call_identifies_which_specialist_made_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four specialists run concurrently against the same log stream — the log
    line must say which one, or the trace is useless for debugging a review."""
    from agents import RunContextWrapper

    from app.tools.sandbox_tools import SandboxToolContext, _log_tool_call

    captured = {}

    def fake_info(event, *, extra):
        captured.update(extra)

    monkeypatch.setattr("app.tools.sandbox_tools.logger.info", fake_info)
    ctx = RunContextWrapper(
        context=SandboxToolContext(sandbox=FakeSandbox(), review_id="rev-1", agent_name="security")
    )

    _log_tool_call(ctx, "read_file", path="app/main.py")

    assert captured["agent"] == "security"
    assert captured["review_id"] == "rev-1"
    assert captured["tool"] == "read_file"


async def test_agent_name_defaults_to_none_when_not_set() -> None:
    from app.tools.sandbox_tools import SandboxToolContext

    context = SandboxToolContext(sandbox=FakeSandbox(), review_id="rev-1")

    assert context.agent_name is None
