"""Docker Sandboxes adapter: subprocess construction, file I/O, cleanup.

Every `asyncio.create_subprocess_exec` call is mocked — no real Docker
Desktop, no real `sbx` binary, is required to run this suite. Command shapes
asserted here are the ones verified live this session against the real
`sbx` CLI (create, exec, rm) — see `app/services/sandbox_service.py`'s
module docstring.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.errors import SandboxTimeoutError, SandboxUnavailableError
from app.services.sandbox_service import SbxSandbox, SbxSandboxFactory


def _fake_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """A minimal stand-in for `asyncio.subprocess.Process`."""
    process = AsyncMock()
    process.communicate.return_value = (stdout, stderr)
    process.returncode = returncode
    process.kill = lambda: None
    process.wait = AsyncMock(return_value=None)
    return process


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def sandbox(workspace: Path) -> SbxSandbox:
    return SbxSandbox("aida-mate-test1234", workspace=workspace, binary="sbx", default_timeout_s=30)


# --- Factory: create() --------------------------------------------------------


async def test_create_raises_when_sbx_is_not_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)

    with pytest.raises(SandboxUnavailableError, match="PATH"):
        await factory.create()


async def test_create_invokes_a_single_create_call(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Unlike the old `docker sandbox` plugin, `sbx create` both provisions
    and starts the sandbox — confirmed live: `sbx exec` worked immediately
    after `create`, with no intermediate `sbx run` call. Only one subprocess
    call should happen here."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    calls: list[list[str]] = []

    async def fake_subprocess_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _fake_process(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)
    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)

    sandbox = await factory.create(labels={"review_id": "abcd1234-xxxx"})

    assert len(calls) == 1
    assert calls[0][:2] == ["sbx", "create"]
    assert calls[0][2] == "shell"
    assert calls[0][-2:] == ["--name", sandbox.id]
    assert sandbox.id.startswith("aida-mate-")


async def test_create_generates_a_name_from_the_review_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process()))

    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)
    sandbox = await factory.create(labels={"review_id": "3d925cae-dc83-4408"})

    assert sandbox.id == "aida-mate-3d925cae"


async def test_create_generates_a_random_name_without_a_review_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process()))

    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)
    a = await factory.create()
    b = await factory.create()

    assert a.id != b.id
    assert a.id.startswith("aida-mate-")


async def test_create_makes_an_empty_workspace_directory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process()))

    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)
    sandbox = await factory.create()

    assert sandbox._workspace.is_dir()
    assert list(sandbox._workspace.iterdir()) == []


async def test_create_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_fake_process(returncode=1, stderr=b"daemon not running")),
    )

    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)

    with pytest.raises(SandboxUnavailableError, match="daemon not running"):
        await factory.create()


async def test_failed_create_cleans_up_the_workspace_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process(returncode=1))
    )
    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)

    with pytest.raises(SandboxUnavailableError):
        await factory.create()

    # No leaked directories under the configured root.
    assert list(tmp_path.iterdir()) == []


async def test_create_raises_sandbox_unavailable_when_the_subprocess_fails_to_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression: `shutil.which` narrows but doesn't eliminate the binary
    failing to launch (removed between the check and this call, a
    permissions problem, etc.) — must raise the domain-specific
    SandboxUnavailableError (chained from the OSError), not let the raw
    OSError escape, and must not leak the mkdtemp'd workspace directory."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sbx")

    async def raise_oserror(*a, **k):
        raise OSError("sbx not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_oserror)
    factory = SbxSandboxFactory(binary="sbx", workdir_root=tmp_path)

    with pytest.raises(SandboxUnavailableError) as exc_info:
        await factory.create()

    assert isinstance(exc_info.value.__cause__, OSError)
    assert list(tmp_path.iterdir()) == []


# --- upload_bytes / read_file (direct workspace I/O, no subprocess) ----------


async def test_upload_bytes_writes_into_the_workspace(sandbox: SbxSandbox, workspace: Path) -> None:
    await sandbox.upload_bytes("archive.tar.gz", b"fake-tarball-bytes")

    assert (workspace / "archive.tar.gz").read_bytes() == b"fake-tarball-bytes"


async def test_upload_bytes_creates_intermediate_directories(sandbox: SbxSandbox, workspace: Path) -> None:
    await sandbox.upload_bytes("nested/dir/file.bin", b"x")

    assert (workspace / "nested" / "dir" / "file.bin").read_bytes() == b"x"


async def test_read_file_reads_from_the_workspace(sandbox: SbxSandbox, workspace: Path) -> None:
    (workspace / "app.py").write_text("print('hi')")

    assert await sandbox.read_file("app.py") == "print('hi')"


async def test_read_file_bounds_output_at_max_bytes(sandbox: SbxSandbox, workspace: Path) -> None:
    (workspace / "big.txt").write_bytes(b"x" * 1000)

    assert len(await sandbox.read_file("big.txt", max_bytes=100)) == 100


async def test_read_file_of_a_missing_file_returns_empty_string(sandbox: SbxSandbox) -> None:
    assert await sandbox.read_file("does-not-exist.py") == ""


@pytest.mark.parametrize("escape_path", ["../outside.txt", "/etc/passwd", "../../etc/shadow"])
async def test_workspace_paths_cannot_escape_the_sandbox_root(sandbox: SbxSandbox, escape_path: str) -> None:
    """A path derived from agent-influenced input must never reach outside the workspace."""
    with pytest.raises(ValueError, match="escapes"):
        await sandbox.upload_bytes(escape_path, b"x")


async def test_workspace_root_itself_is_a_valid_target(sandbox: SbxSandbox, workspace: Path) -> None:
    # Exercises the boundary case: the resolved path equals the workspace root exactly.
    result = sandbox._resolve_workspace_path(".")
    assert result == workspace.resolve()


# --- exec ----------------------------------------------------------------


async def test_exec_returns_captured_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=_fake_process(returncode=0, stdout=b"hello\n", stderr=b"")),
    )

    result = await sandbox.exec("echo hello")

    assert result.exit_code == 0
    assert result.stdout == "hello\n"


async def test_exec_reports_a_nonzero_exit_without_raising(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    """A command failing (e.g. grep finding nothing) is not itself an error."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process(returncode=1))
    )

    result = await sandbox.exec("grep xyz file.txt")

    assert result.exit_code == 1


async def test_exec_command_is_run_through_a_shell(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    captured: list[list[str]] = []

    async def fake_subprocess_exec(*argv, **kwargs):
        captured.append(list(argv))
        return _fake_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await sandbox.exec("echo hi | wc -l")

    argv = captured[0]
    assert argv[:2] == ["sbx", "exec"]
    assert argv[-3:] == ["sh", "-c", "echo hi | wc -l"]
    assert argv[-4] == sandbox.id


async def test_exec_passes_cwd_as_workdir_flag(monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox) -> None:
    """A native --workdir flag exists; no shell-level `cd` workaround is needed."""
    captured: list[list[str]] = []

    async def fake_subprocess_exec(*argv, **kwargs):
        captured.append(list(argv))
        return _fake_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await sandbox.exec("ls", cwd="/workspace/repo")

    assert "--workdir" in captured[0]
    assert captured[0][captured[0].index("--workdir") + 1] == "/workspace/repo"


async def test_exec_without_cwd_omits_the_workdir_flag(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    """Regression: the previous adapter (against `docker sandbox`, whose own
    default cwd wasn't documented) always passed `--workdir` explicitly,
    computed from the host-side workspace path. Confirmed live this session
    that doing the equivalent against `sbx` is actively wrong on Windows —
    the container mounts the workspace at a POSIX-translated path, not the
    literal host path, so passing the host path as `--workdir` fails with
    "No such file or directory" (reproduced live). `sbx exec` already
    defaults its own cwd to the mounted workspace when `--workdir` is
    omitted (also confirmed live), so `cwd=None` must not add the flag at
    all rather than substituting a computed path."""
    captured: list[list[str]] = []

    async def fake_subprocess_exec(*argv, **kwargs):
        captured.append(list(argv))
        return _fake_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await sandbox.exec("ls")

    assert "--workdir" not in captured[0]


async def test_exec_times_out_and_kills_the_local_process(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    process = _fake_process()

    async def hang_forever():
        await asyncio.sleep(999)

    process.communicate = hang_forever
    killed = []
    process.kill = lambda: killed.append(True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    with pytest.raises(SandboxTimeoutError):
        await sandbox.exec("sleep 999", timeout_s=0.01)

    assert killed == [True]


async def test_exec_uses_the_default_timeout_when_none_given(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    sandbox = SbxSandbox("s1", workspace=workspace, binary="sbx", default_timeout_s=0.01)
    process = _fake_process()

    async def hang_forever():
        await asyncio.sleep(999)

    process.communicate = hang_forever
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    with pytest.raises(SandboxTimeoutError):
        await sandbox.exec("sleep 999")


# --- destroy ---------------------------------------------------------------


async def test_destroy_invokes_rm_with_force(monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox) -> None:
    """`--force` is required — confirmed live: `sbx rm` otherwise asks for
    interactive confirmation, which would hang this non-interactive call."""
    captured: list[list[str]] = []

    async def fake_subprocess_exec(*argv, **kwargs):
        captured.append(list(argv))
        return _fake_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await sandbox.destroy()

    assert captured[0] == ["sbx", "rm", sandbox.id, "--force"]


async def test_destroy_removes_the_workspace_directory(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox, workspace: Path
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process()))
    (workspace / "leftover.txt").write_text("x")

    await sandbox.destroy()

    assert not workspace.exists()


async def test_destroy_never_raises_on_nonzero_rm_exit(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process(returncode=1, stderr=b"gone"))
    )

    await sandbox.destroy()  # must not raise


async def test_destroy_never_raises_when_the_subprocess_itself_fails_to_launch(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox
) -> None:
    async def raise_oserror(*a, **k):
        raise OSError("sbx not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_oserror)

    await sandbox.destroy()  # must not raise


async def test_destroy_is_safe_to_call_when_workspace_already_gone(
    monkeypatch: pytest.MonkeyPatch, sandbox: SbxSandbox, workspace: Path
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_fake_process()))
    workspace.rmdir()

    await sandbox.destroy()  # must not raise
