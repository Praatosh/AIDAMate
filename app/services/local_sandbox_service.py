"""Host-filesystem sandbox — a stopgap for machines where Docker Sandboxes
cannot run at all.

NOT a security boundary. `SbxSandbox` isolates PR content inside a real VM;
this backend runs the exact same read-only inspection operations (list
files, grep, extract the downloaded archive) directly on the host
filesystem inside a scratch temp directory. Nothing from the PR is ever
executed as code here — only read, listed, or grepped, the same guarantee
the sandbox tools already provide — but the OS-level isolation `sbx` gives
is gone.

Exists for hosts that genuinely cannot run Docker Sandboxes at all (e.g. no
admin rights to install a WSL2 kernel update Docker Desktop's engine needs)
— not this project's own development machine specifically, despite an
earlier session's note to that effect: `sbx` (the current CLI; see
`sandbox_service.py`) is live-verified working here. Selected via
`SANDBOX_MODE=local`; the default remains `docker`. Swap back to `docker`
mode wherever `sbx` will actually run — nothing downstream (tools,
orchestrator, risk engine) needs to change, since both backends satisfy the
same `ISandbox`/`ISandboxFactory` Protocols.

`exec()` here does not run an arbitrary shell. AIDA-MATE's own code is the only
caller — the LLM only ever supplies tool *arguments* (a path, a search
pattern), never a raw command string — and it only ever issues one of three
fixed command shapes: the archive-extraction line built in
`app/agents/orchestrator.py`, and the `find`/`grep` templates built in
`app/tools/sandbox_tools.py`. Each is recognized and given a native Python
implementation; anything else is rejected rather than silently mishandled.

Known limitation: paths or search patterns containing a literal space are
not reconstructed with full POSIX-shell fidelity by the regexes below (real
PR file paths essentially never contain one). A command that cannot be
recognized returns a clear `exit_code=127` rather than misbehaving silently.
"""

import asyncio
import re
import shlex
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from app.core.logging import get_logger
from app.services.sandbox_service import SandboxExecResult
from app.tools.sandbox_tools import SANDBOX_REPO_DIR

logger = get_logger(__name__)

#: Must match `app.agents.orchestrator._ARCHIVE_FILENAME` — both name the
#: same fixed, non-configurable filename the downloaded archive is written
#: to inside the sandbox workspace.
_ARCHIVE_FILENAME = "archive.tar.gz"

_EXTRACT_COMMAND = (
    f"mkdir -p {SANDBOX_REPO_DIR} && tar -xzf {_ARCHIVE_FILENAME} -C {SANDBOX_REPO_DIR} --strip-components=1"
)

_FIND_RE = re.compile(r"^find (?P<target>.+?) -maxdepth (?P<depth>\d+) -type f \| head -n (?P<limit>\d+)$")
_GREP_RE = re.compile(r"^grep -rn -F (?P<rest>.+) \| head -n (?P<limit>\d+)$")


def _split_shell_words(text: str) -> list[str] | None:
    """Tokenize a fragment of the shell command AIDA-MATE itself built.

    Returns None instead of raising on malformed input — an unrecognized
    shape should fall through to the "unsupported command" response, not
    crash the review.
    """
    try:
        return shlex.split(text)
    except ValueError:
        return None


class LocalSandbox:
    """Host-filesystem stand-in for `SbxSandbox`. See module docstring."""

    def __init__(self, id: str, *, workspace: Path, default_timeout_s: float) -> None:
        self.id = id
        self._workspace = workspace
        self._default_timeout_s = default_timeout_s

    async def upload_bytes(self, dest_path: str, content: bytes) -> None:
        target = self._resolve_workspace_path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        target = self._resolve_workspace_path(path)
        try:
            data = await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError:
            return ""
        return data[:max_bytes].decode(errors="replace")

    async def exec(
        self, command: str, *, cwd: str | None = None, timeout_s: float | None = None
    ) -> SandboxExecResult:
        """Recognize and natively execute one of AIDA-MATE's own fixed command shapes."""
        effective_cwd = Path(cwd) if cwd is not None else self._workspace.resolve()

        if command == _EXTRACT_COMMAND:
            return await asyncio.to_thread(self._extract_archive, effective_cwd)
        if match := _FIND_RE.match(command):
            return await asyncio.to_thread(self._find_files, effective_cwd, match)
        if match := _GREP_RE.match(command):
            return await asyncio.to_thread(self._grep, effective_cwd, match)

        logger.warning(
            "Local sandbox received an unrecognized command",
            extra={"sandbox_id": self.id, "command": command[:200]},
        )
        return SandboxExecResult(
            exit_code=127,
            stdout="",
            stderr=f"local sandbox does not support this command: {command!r}",
        )

    async def destroy(self) -> None:
        try:
            await asyncio.to_thread(shutil.rmtree, self._workspace, True)
        except OSError as exc:
            logger.warning(
                "Failed to remove local sandbox workspace",
                extra={"sandbox_id": self.id, "error": str(exc)},
            )

    # -- exec() command implementations --------------------------------------

    def _extract_archive(self, cwd: Path) -> SandboxExecResult:
        dest = cwd / SANDBOX_REPO_DIR
        archive_path = cwd / _ARCHIVE_FILENAME
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                for member in tar.getmembers():
                    # GitHub tarballs wrap content in one `owner-repo-sha/`
                    # directory; drop it, mirroring `--strip-components=1`.
                    parts = PurePosixPath(member.name).parts
                    if len(parts) <= 1:
                        continue
                    member.name = str(PurePosixPath(*parts[1:]))
                    tar.extract(member, dest, filter="data")
        except (tarfile.TarError, OSError) as exc:
            return SandboxExecResult(exit_code=1, stdout="", stderr=str(exc))
        return SandboxExecResult(exit_code=0, stdout="", stderr="")

    def _find_files(self, cwd: Path, match: re.Match) -> SandboxExecResult:
        tokens = _split_shell_words(match.group("target"))
        if not tokens:
            return SandboxExecResult(exit_code=2, stdout="", stderr="could not parse target path")
        try:
            target = self._enforce_within_workspace(cwd / tokens[0])
        except ValueError as exc:
            return SandboxExecResult(exit_code=2, stdout="", stderr=str(exc))
        max_depth = int(match.group("depth"))
        limit = int(match.group("limit"))

        if not target.exists():
            return SandboxExecResult(exit_code=1, stdout="", stderr=f"{target}: No such file or directory")

        root_depth = len(target.resolve().parts)
        files: list[str] = []
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if len(path.resolve().parts) - root_depth > max_depth:
                continue
            files.append(str(path))
            if len(files) >= limit:
                break
        return SandboxExecResult(exit_code=0, stdout="\n".join(files), stderr="")

    def _grep(self, cwd: Path, match: re.Match) -> SandboxExecResult:
        tokens = _split_shell_words(match.group("rest"))
        if not tokens or len(tokens) != 2:
            return SandboxExecResult(exit_code=2, stdout="", stderr="could not parse pattern/target")
        pattern, target_token = tokens
        try:
            target = self._enforce_within_workspace(cwd / target_token)
        except ValueError as exc:
            return SandboxExecResult(exit_code=2, stdout="", stderr=str(exc))
        limit = int(match.group("limit"))

        if not target.exists():
            return SandboxExecResult(exit_code=2, stdout="", stderr=f"{target}: No such file or directory")

        paths = [target] if target.is_file() else [p for p in sorted(target.rglob("*")) if p.is_file()]
        matches: list[str] = []
        for path in paths:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append(f"{path}:{lineno}:{line}")
                    if len(matches) >= limit:
                        return SandboxExecResult(exit_code=0, stdout="\n".join(matches), stderr="")

        exit_code = 0 if matches else 1  # grep's own convention: 1 means "no matches", not a failure
        return SandboxExecResult(exit_code=exit_code, stdout="\n".join(matches), stderr="")

    def _resolve_workspace_path(self, relative_path: str) -> Path:
        return self._enforce_within_workspace(self._workspace / relative_path)

    def _enforce_within_workspace(self, candidate: Path) -> Path:
        """Resolve `candidate` and confirm it stays within the workspace root.

        Shared by `upload_bytes`/`read_file` (via `_resolve_workspace_path`)
        and `_find_files`/`_grep` — the latter pair used to skip this check
        entirely, the gap a security audit found: `find`/`grep` run directly
        on the host filesystem here (unlike `SbxSandbox`, where the same
        commands stay inside an isolated VM), so an unenforced `..`-climbing
        `path`/`pattern` argument could read arbitrary host files and have
        the result published into a public PR comment via a `Finding`.
        Redundant with (not a replacement for) `sandbox_tools.py`'s own
        `_validated_relative_path` check — the two are separate layers on
        purpose, given what leaking through this path would expose.
        """
        resolved = candidate.resolve()
        workspace = self._workspace.resolve()
        if workspace not in resolved.parents and resolved != workspace:
            raise ValueError(f"Path escapes sandbox workspace: {candidate!r}")
        return resolved


class LocalSandboxFactory:
    """Creates `LocalSandbox` instances backed by a scratch host temp directory."""

    def __init__(self, *, workdir_root: str | Path | None = None, default_timeout_s: float = 900) -> None:
        self._workdir_root = Path(workdir_root) if workdir_root else Path(tempfile.gettempdir())
        self._default_timeout_s = default_timeout_s

    async def create(self, *, labels: dict[str, str] | None = None) -> LocalSandbox:
        review_id = (labels or {}).get("review_id", "")
        name = f"aida-mate-local-{review_id[:8] or uuid.uuid4().hex[:8]}"

        workspace = await asyncio.to_thread(
            lambda: Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self._workdir_root))
        )

        logger.info("Local sandbox created", extra={"sandbox_id": name, "workspace": str(workspace)})
        return LocalSandbox(name, workspace=workspace, default_timeout_s=self._default_timeout_s)
