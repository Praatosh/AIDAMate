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

`exec()` here does not run an arbitrary shell — in fact, as of this version it
runs nothing at all. AIDA-MATE's own code is the only caller of any sandbox
operation — the LLM only ever supplies tool *arguments* (a path, a search
pattern), never a raw command string — and every operation this backend needs
to support (`find_files`, `grep_files`, `extract_archive`, alongside
`upload_bytes`/`read_file`) is now a first-class typed `ISandbox` method with
its own native Python implementation, none of them going through a shell or
external binary (`find`/`grep`/`tar`) at all. An earlier version of this
backend recognized two fixed `find`/`grep` *command-string* shapes built by
`app/tools/sandbox_tools.py` via regex matching inside `exec()` — that
indirection is gone now that the tools call `find_files`/`grep_files`
directly; `exec()` is kept only because `ISandbox` still declares it as a
general capability (and `SbxSandbox` still has a real use for it, running
inside its own isolated container), and always returns "unsupported" here.

Known limitation: none specific to path/pattern spacing anymore, now that
paths and patterns are passed as plain typed arguments rather than
reconstructed from a shell command string.
"""

import asyncio
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from app.core.logging import get_logger
from app.services.sandbox_service import SandboxExecResult

logger = get_logger(__name__)

#: Must match `app.agents.orchestrator._ARCHIVE_FILENAME` — both name the
#: same fixed, non-configurable filename the downloaded archive is written
#: to inside the sandbox workspace.
_ARCHIVE_FILENAME = "archive.tar.gz"


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
        """Not supported by this backend — see the module docstring.

        Every operation this backend needs is a typed `ISandbox` method
        instead; nothing in the codebase calls `exec()` on a `LocalSandbox`
        any more, but it stays implemented (returning a clear "unsupported"
        result rather than raising) since `ISandbox` still declares it.
        """
        logger.warning(
            "Local sandbox received an exec() call; this backend supports no commands",
            extra={"sandbox_id": self.id, "command": command[:200]},
        )
        return SandboxExecResult(
            exit_code=127,
            stdout="",
            stderr="local sandbox does not support exec() — use a typed ISandbox operation instead",
        )

    async def destroy(self) -> None:
        try:
            await asyncio.to_thread(shutil.rmtree, self._workspace, True)
        except OSError as exc:
            logger.warning(
                "Failed to remove local sandbox workspace",
                extra={"sandbox_id": self.id, "error": str(exc)},
            )

    async def extract_archive(self, archive_path: str, dest_dir: str) -> SandboxExecResult:
        """Extract the `.tar.gz` at `archive_path` into `dest_dir`. See `ISandbox.extract_archive`."""
        archive_file = self._resolve_workspace_path(archive_path)
        dest = self._resolve_workspace_path(dest_dir)
        return await asyncio.to_thread(self._extract_archive, archive_file, dest)

    def _extract_archive(self, archive_path: Path, dest: Path) -> SandboxExecResult:
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

    async def find_files(self, path: str, *, max_depth: int, limit: int) -> SandboxExecResult:
        """List files under `path`, depth-bounded. See `ISandbox.find_files`."""
        return await asyncio.to_thread(self._find_files, path, max_depth, limit)

    def _find_files(self, path: str, max_depth: int, limit: int) -> SandboxExecResult:
        try:
            target = self._resolve_workspace_path(path)
        except ValueError as exc:
            return SandboxExecResult(exit_code=2, stdout="", stderr=str(exc))

        if not target.exists():
            return SandboxExecResult(exit_code=1, stdout="", stderr=f"{target}: No such file or directory")

        root_depth = len(target.parts)
        files: list[str] = []
        for candidate in sorted(target.rglob("*")):
            if not candidate.is_file():
                continue
            if len(candidate.parts) - root_depth > max_depth:
                continue
            files.append(self._relative_output_path(candidate))
            if len(files) >= limit:
                break
        return SandboxExecResult(exit_code=0, stdout="\n".join(files), stderr="")

    async def grep_files(self, pattern: str, path: str, *, limit: int) -> SandboxExecResult:
        """Literal content search under `path`. See `ISandbox.grep_files`."""
        return await asyncio.to_thread(self._grep_files, pattern, path, limit)

    def _grep_files(self, pattern: str, path: str, limit: int) -> SandboxExecResult:
        try:
            target = self._resolve_workspace_path(path)
        except ValueError as exc:
            return SandboxExecResult(exit_code=2, stdout="", stderr=str(exc))

        if not target.exists():
            return SandboxExecResult(exit_code=2, stdout="", stderr=f"{target}: No such file or directory")

        candidates = [target] if target.is_file() else [p for p in sorted(target.rglob("*")) if p.is_file()]
        matches: list[str] = []
        for candidate in candidates:
            try:
                text = candidate.read_text(errors="replace")
            except OSError:
                continue
            relative = self._relative_output_path(candidate)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append(f"{relative}:{lineno}:{line}")
                    if len(matches) >= limit:
                        return SandboxExecResult(exit_code=0, stdout="\n".join(matches), stderr="")

        exit_code = 0 if matches else 1  # grep's own convention: 1 means "no matches", not a failure
        return SandboxExecResult(exit_code=exit_code, stdout="\n".join(matches), stderr="")

    def _relative_output_path(self, candidate: Path) -> str:
        """Render a matched path relative to the workspace root, POSIX-style.

        `find_files`/`grep_files` used to build their output from an absolute
        host path (inherited from the old shell-command-string era) — this is
        what fixed that: results now line up with `ChangedFile.filename`'s own
        shape (e.g. `"repo/app/main.py"`) and never leak the host's directory
        layout (temp dir name, local username, ...) into agent context or,
        ultimately, a published review comment.
        """
        return candidate.relative_to(self._workspace.resolve()).as_posix()

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
