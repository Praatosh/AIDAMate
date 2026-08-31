"""Docker Sandboxes adapter, implementing `ISandbox`/`ISandboxFactory`.

Wraps the standalone `sbx` CLI — Docker's current "Docker Sandboxes" product,
confirmed live this session as the actual replacement for the `docker
sandbox` CLI plugin this file previously wrapped, which Docker has since
deprecated and removed (see CLAUDE.md §6). `sbx` is its own top-level binary
with its own subcommands, not a `docker <subcommand>` plugin invocation —
verified against its own `--help` output for every subcommand used here, and
against a full live create -> exec -> rm cycle, not assumed from Docker's
public marketing docs.

The real command surface, confirmed this session:

    sbx create shell WORKSPACE --name NAME   # creates AND starts — no separate "run" step
    sbx exec [--workdir DIR] SANDBOX COMMAND [ARG...]   # auto-starts if stopped; non-interactive by default
    sbx rm SANDBOX --force                   # --force skips the confirmation prompt `sbx rm` asks otherwise
    sbx ls --json

`create shell WORKSPACE` bind-mounts a host directory into the sandbox, so
`upload_bytes`/`read_file` operate on that mounted directory directly rather
than shelling out to copy anything — unchanged from the previous adapter.
There is still no `cp`-based transfer needed for this app's use case.

One live-verified, Windows-specific gotcha `exec()` accounts for: the
container does NOT mount the workspace at the literal host path. On this
Windows host, `C:\\Users\\...\\workspace` is visible inside the sandbox at a
POSIX-translated path (e.g. `/c/Users/.../workspace`) — passing the raw host
path as `--workdir` fails with "No such file or directory" (reproduced
live). `sbx exec` already defaults its own working directory to the
sandbox's mounted workspace when `--workdir` is omitted, so `exec()` simply
omits the flag in the common case (every caller in this codebase calls
`exec()` with `cwd=None`) rather than trying to compute the in-container
path itself.

The workspace directory is created empty per sandbox and holds only whatever
this service writes into it (the downloaded PR archive) — AIDA-MATE never
mounts a real project directory from this host.
"""

import asyncio
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import SandboxTimeoutError, SandboxUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_HELP_OUTPUT_CHARS = 2000


@dataclass(frozen=True)
class SandboxExecResult:
    """Result of one command executed inside a sandbox. Satisfies the Protocol structurally."""

    exit_code: int
    stdout: str
    stderr: str


class SbxSandbox:
    """A single Docker Sandbox instance, satisfying `ISandbox`.

    Holds the host-side workspace directory alongside the sandbox's own name
    so `upload_bytes`/`read_file` can operate on it directly as a local path —
    no subprocess round-trip needed for file transfer, only for `exec`.
    """

    def __init__(
        self,
        id: str,
        *,
        workspace: Path,
        binary: str,
        default_timeout_s: float,
    ) -> None:
        self.id = id
        self._workspace = workspace
        self._binary = binary
        self._default_timeout_s = default_timeout_s

    async def upload_bytes(self, dest_path: str, content: bytes) -> None:
        """Write `content` into the sandbox's mounted workspace at `dest_path`.

        `dest_path` is relative to the workspace root. No sandbox subprocess is
        invoked — the mount makes host writes visible inside immediately.
        """
        target = self._resolve_workspace_path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)

    async def exec(
        self, command: str, *, cwd: str | None = None, timeout_s: float | None = None
    ) -> SandboxExecResult:
        """Run `command` inside the sandbox via a POSIX shell, non-interactively.

        `sbx exec` runs non-interactively and captures output by default —
        confirmed via its `--help` text (`-i`/`-t` are opt-in flags) and live
        testing. Commands are always passed through `sh -c` (rather than split
        into argv) so callers can use shell features (pipes, redirects) the
        same way the sandbox tools already assume; the *contents* of `command`
        are the caller's responsibility to have shell-escaped (see
        `app/tools/sandbox_tools.py`).

        `--workdir` is only passed when `cwd` is explicitly given. When `cwd`
        is None (every caller in this codebase), `sbx exec` is left to apply
        its own default, which is already the sandbox's mounted workspace —
        confirmed live. Explicitly computing and passing the workspace path
        here (the previous adapter's approach against `docker sandbox`, whose
        undocumented default couldn't be trusted) would be actively wrong on
        this host: the container mounts the workspace at a POSIX-translated
        path, not the literal Windows host path this class holds — see the
        module docstring. A caller that supplies its own `cwd` is responsible
        for passing a path valid *inside* the container.
        """
        argv = [self._binary, "exec"]
        if cwd is not None:
            argv += ["--workdir", cwd]
        argv += [self.id, "sh", "-c", command]

        effective_timeout = timeout_s if timeout_s is not None else self._default_timeout_s

        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SandboxTimeoutError(
                f"Command exceeded {effective_timeout}s in sandbox {self.id}"
            ) from exc

        return SandboxExecResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        """Read a file from the sandbox's mounted workspace directly.

        Bypasses `exec` entirely for this common case: the workspace mount
        means the file is already a real path on the host filesystem, so there
        is no reason to pay for a subprocess round-trip to read it.
        """
        target = self._resolve_workspace_path(path)
        try:
            data = await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError:
            return ""
        return data[:max_bytes].decode(errors="replace")

    async def destroy(self) -> None:
        """Remove the sandbox and its host-side workspace directory.

        Never raises, per the `ISandbox` contract: this always runs from a
        `finally` block, and a cleanup failure must never mask the error that
        triggered cleanup in the first place. Both the `sbx rm` call and the
        workspace removal are individually best-effort. `--force` is required
        — confirmed live: `sbx rm` otherwise asks for interactive confirmation,
        which would hang this non-interactive subprocess indefinitely.
        """
        argv = [self._binary, "rm", self.id, "--force"]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                logger.warning(
                    "sbx rm returned nonzero",
                    extra={"sandbox_id": self.id, "stderr": stderr.decode(errors="replace")[:500]},
                )
        except OSError as exc:
            logger.warning(
                "Failed to invoke sbx rm",
                extra={"sandbox_id": self.id, "error": str(exc)},
            )

        try:
            shutil.rmtree(self._workspace, ignore_errors=True)
        except OSError as exc:
            logger.warning(
                "Failed to remove sandbox workspace directory",
                extra={"sandbox_id": self.id, "error": str(exc)},
            )

    def _resolve_workspace_path(self, relative_path: str) -> Path:
        """Resolve a caller-supplied relative path against the workspace root.

        Rejects escapes (`..`, absolute paths) so a path derived from
        agent-influenced input can never write or read outside the sandbox's
        own workspace directory on the host.
        """
        candidate = (self._workspace / relative_path).resolve()
        if self._workspace.resolve() not in candidate.parents and candidate != self._workspace.resolve():
            raise ValueError(f"Path escapes sandbox workspace: {relative_path!r}")
        return candidate


class SbxSandboxFactory:
    """Creates `SbxSandbox` instances via `sbx create shell`."""

    def __init__(
        self,
        *,
        binary: str = "sbx",
        workdir_root: str | Path | None = None,
        default_timeout_s: float = 900,
    ) -> None:
        self._binary = binary
        self._workdir_root = Path(workdir_root) if workdir_root else Path(tempfile.gettempdir())
        self._default_timeout_s = default_timeout_s

    async def create(self, *, labels: dict[str, str] | None = None) -> SbxSandbox:
        """Provision a fresh, agent-less sandbox with an empty mounted workspace.

        A single `sbx create shell` call both provisions and starts the
        sandbox — unlike the old `docker sandbox` plugin, there is no separate
        "run" step (confirmed live: `sbx exec` succeeded immediately after
        `create`, with no intermediate `sbx run` call).

        Raises:
            SandboxUnavailableError: if the `sbx` binary isn't on PATH, or
                `sbx create` fails (commonly because Docker Desktop / the
                sandbox daemon isn't running, or the one-time interactive
                `sbx login` this app cannot perform on its own hasn't been done).
        """
        if shutil.which(self._binary) is None:
            raise SandboxUnavailableError(
                f"'{self._binary}' is not on PATH. Install Docker Sandboxes "
                "(https://www.docker.com/products/docker-sandboxes) and ensure Docker Desktop is running."
            )

        review_id = (labels or {}).get("review_id", "")
        name = f"aida-mate-{review_id[:8] or uuid.uuid4().hex[:8]}"

        workspace = await asyncio.to_thread(
            lambda: Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self._workdir_root))
        )

        argv = [self._binary, "create", "shell", str(workspace), "--name", name]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            shutil.rmtree(workspace, ignore_errors=True)
            raise SandboxUnavailableError(
                f"sbx create failed (exit {process.returncode}): "
                f"{stderr.decode(errors='replace')[:_MAX_HELP_OUTPUT_CHARS]}"
            )

        logger.info("Sandbox created", extra={"sandbox_id": name, "workspace": str(workspace)})
        return SbxSandbox(
            name, workspace=workspace, binary=self._binary, default_timeout_s=self._default_timeout_s
        )
