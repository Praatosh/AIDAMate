"""Docker Sandboxes adapter, implementing `ISandbox`/`ISandboxFactory`.

Wraps the `docker sandbox` CLI plugin (verified installed and working via its
own `--help` output — not the public docs, which describe a differently-shaped
standalone `sbx` binary with a `cp` subcommand that does not exist here).

The real command surface, confirmed this session:

    docker sandbox create [--name NAME] shell WORKSPACE
    docker sandbox run SANDBOX
    docker sandbox exec [-w WORKDIR] SANDBOX COMMAND [ARG...]   # non-interactive by default
    docker sandbox rm SANDBOX
    docker sandbox ls --json

There is no `cp` subcommand. `create shell WORKSPACE` bind-mounts a host
directory into the sandbox at the *identical path* instead — so `upload_bytes`
writes directly to that mounted directory rather than shelling out to copy
anything. This is simpler than the `cp`-based design the plan started from,
and was adopted specifically because it matches what's actually installed.

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

#: Path inside the sandbox VM that mirrors the host workspace directory.
#: `create shell WORKSPACE` documents this as "exposed at the same path as on
#: the host" — so the in-sandbox path is only knowable once the host temp
#: directory exists, which is why `create()` generates it first.
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

        `docker sandbox exec` runs non-interactively and captures output by
        default — confirmed via its `--help` text, which lists `-i`/`-t` as
        opt-in flags rather than the only mode. Commands are always passed
        through `sh -c` (rather than split into argv) so callers can use shell
        features (pipes, redirects) the same way the sandbox tools already
        assume; the *contents* of `command` are the caller's responsibility to
        have shell-escaped (see `app/tools/sandbox_tools.py`).

        `cwd` defaults to the sandbox's mounted workspace root, not whatever
        `docker sandbox exec` would otherwise pick — that default isn't
        documented, and callers (the extraction step, the sandbox tools) build
        paths relative to the workspace, so leaving this to chance would make
        relative-path resolution depend on an unconfirmed CLI behavior instead
        of something this adapter controls and can be tested.
        """
        effective_cwd = cwd if cwd is not None else str(self._workspace.resolve())
        argv = [self._binary, "sandbox", "exec", "--workdir", effective_cwd, self.id, "sh", "-c", command]

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
        triggered cleanup in the first place. Both the `docker sandbox rm` call
        and the workspace removal are individually best-effort.
        """
        argv = [self._binary, "sandbox", "rm", self.id]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                logger.warning(
                    "docker sandbox rm returned nonzero",
                    extra={"sandbox_id": self.id, "stderr": stderr.decode(errors="replace")[:500]},
                )
        except OSError as exc:
            logger.warning(
                "Failed to invoke docker sandbox rm",
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
    """Creates `SbxSandbox` instances via `docker sandbox create shell`."""

    def __init__(
        self,
        *,
        binary: str = "docker",
        workdir_root: str | Path | None = None,
        default_timeout_s: float = 900,
    ) -> None:
        self._binary = binary
        self._workdir_root = Path(workdir_root) if workdir_root else Path(tempfile.gettempdir())
        self._default_timeout_s = default_timeout_s

    async def create(self, *, labels: dict[str, str] | None = None) -> SbxSandbox:
        """Provision a fresh, agent-less sandbox with an empty mounted workspace.

        Raises:
            SandboxUnavailableError: if the `docker` binary isn't on PATH, or
                `docker sandbox create` fails (commonly because Docker Desktop
                / the sandbox daemon isn't running — `docker sandbox version`
                reports "Server Version: Unavailable" in that case).
        """
        if shutil.which(self._binary) is None:
            raise SandboxUnavailableError(
                f"'{self._binary}' is not on PATH. Install Docker Desktop and ensure it's running."
            )

        review_id = (labels or {}).get("review_id", "")
        name = f"aida-mate-{review_id[:8] or uuid.uuid4().hex[:8]}"

        workspace = await asyncio.to_thread(
            lambda: Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self._workdir_root))
        )

        argv = [self._binary, "sandbox", "create", "--name", name, "shell", str(workspace)]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            shutil.rmtree(workspace, ignore_errors=True)
            stderr_text = stderr.decode(errors="replace")
            if "deprecated" in stderr_text.lower() and "removed" in stderr_text.lower():
                # Docker has discontinued the `docker sandbox` CLI plugin this
                # whole adapter is built on (confirmed live — see CLAUDE.md §6).
                # A generic exit-code-and-stderr dump would send an operator
                # hunting for a Docker Desktop / WSL2 problem that doesn't
                # exist; every SANDBOX_MODE=docker review would otherwise fail
                # this way with no indication of the real, permanent cause.
                raise SandboxUnavailableError(
                    "The 'docker sandbox' CLI plugin has been deprecated and removed by Docker "
                    "(unrelated to Docker Desktop not running). This adapter has no working "
                    "replacement yet — set SANDBOX_MODE=local (not an isolation boundary, but "
                    "functional) or disable the sandbox stage until one is built. "
                    f"Docker's own message: {stderr_text[:_MAX_HELP_OUTPUT_CHARS]}"
                )
            raise SandboxUnavailableError(
                f"docker sandbox create failed (exit {process.returncode}): "
                f"{stderr_text[:_MAX_HELP_OUTPUT_CHARS]}"
            )

        # `create` provisions the sandbox definition; `run` starts it so `exec`
        # has something live to attach to. Confirmed as two distinct steps via
        # `docker sandbox run --help` ("Create the sandbox if it does not
        # exist" implies `create` alone does not start it).
        run_argv = [self._binary, "sandbox", "run", name]
        run_process = await asyncio.create_subprocess_exec(
            *run_argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, run_stderr = await run_process.communicate()
        if run_process.returncode != 0:
            shutil.rmtree(workspace, ignore_errors=True)
            raise SandboxUnavailableError(
                f"docker sandbox run failed (exit {run_process.returncode}): "
                f"{run_stderr.decode(errors='replace')[:_MAX_HELP_OUTPUT_CHARS]}"
            )

        logger.info("Sandbox created", extra={"sandbox_id": name, "workspace": str(workspace)})
        return SbxSandbox(
            name, workspace=workspace, binary=self._binary, default_timeout_s=self._default_timeout_s
        )
