"""Host-filesystem sandbox: the Docker-free stopgap backend.

No subprocess, no Docker, no network — everything here runs against real
temp directories and a real `tarfile`, which is exactly what makes this
backend usable on a host with neither `git` nor POSIX shell utilities.
"""

import io
import tarfile
from pathlib import Path

import pytest

from app.services.local_sandbox_service import (
    _ARCHIVE_FILENAME,
    _EXTRACT_COMMAND,
    LocalSandbox,
    LocalSandboxFactory,
)
from app.tools.sandbox_tools import SANDBOX_REPO_DIR, _list_files_impl, _read_file_impl, _search_code_impl


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def sandbox(workspace: Path) -> LocalSandbox:
    return LocalSandbox("aida-mate-local-test", workspace=workspace, default_timeout_s=30)


def _make_tarball(files: dict[str, bytes], *, top_level_dir: str = "acme-api-3f2c9ab") -> bytes:
    """Build a `.tar.gz` shaped like a real GitHub archive: one wrapper directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative_path, content in files.items():
            info = tarfile.TarInfo(name=f"{top_level_dir}/{relative_path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


# --- Factory -------------------------------------------------------------------


async def test_create_returns_a_sandbox_with_a_scratch_workspace(tmp_path: Path) -> None:
    factory = LocalSandboxFactory(workdir_root=tmp_path)

    created = await factory.create(labels={"review_id": "abcd1234-xxxx"})

    assert created.id.startswith("aida-mate-local-")
    assert created.id.endswith("abcd1234")


async def test_create_without_a_review_id_still_gets_a_unique_name(tmp_path: Path) -> None:
    factory = LocalSandboxFactory(workdir_root=tmp_path)

    first = await factory.create()
    second = await factory.create()

    assert first.id != second.id


# --- upload_bytes / read_file ---------------------------------------------------


async def test_upload_then_read_round_trips(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("notes.txt", b"hello from the host")

    assert await sandbox.read_file("notes.txt") == "hello from the host"


async def test_read_file_missing_returns_empty_string(sandbox: LocalSandbox) -> None:
    assert await sandbox.read_file("does/not/exist.txt") == ""


async def test_upload_bytes_rejects_workspace_escape(sandbox: LocalSandbox) -> None:
    with pytest.raises(ValueError, match="escapes"):
        await sandbox.upload_bytes("../../etc/passwd", b"nope")


async def test_read_file_truncates_to_max_bytes(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("big.txt", b"x" * 100)

    assert len(await sandbox.read_file("big.txt", max_bytes=10)) == 10


# --- exec(): archive extraction --------------------------------------------------


async def test_exec_extracts_archive_and_strips_the_top_level_directory(sandbox: LocalSandbox) -> None:
    archive = _make_tarball({"README.md": b"# hi", "app/main.py": b"print('hi')"})
    await sandbox.upload_bytes(_ARCHIVE_FILENAME, archive)

    result = await sandbox.exec(_EXTRACT_COMMAND)

    assert result.exit_code == 0
    extracted = sandbox._resolve_workspace_path(SANDBOX_REPO_DIR)
    assert (extracted / "README.md").read_text() == "# hi"
    assert (extracted / "app" / "main.py").read_text() == "print('hi')"
    assert not (extracted / "acme-api-3f2c9ab").exists()


async def test_exec_extraction_reports_a_missing_archive(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(_EXTRACT_COMMAND)

    assert result.exit_code != 0


# --- exec(): find/list_files -----------------------------------------------------


async def test_exec_find_lists_files_under_the_target(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("repo/app/main.py", b"...")
    await sandbox.upload_bytes("repo/app/util.py", b"...")
    await sandbox.upload_bytes("repo/README.md", b"...")

    result = await sandbox.exec("find repo -maxdepth 4 -type f | head -n 500")

    assert result.exit_code == 0
    names = {Path(line).name for line in result.stdout.splitlines()}
    assert names == {"main.py", "util.py", "README.md"}


async def test_exec_find_respects_maxdepth(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("repo/a.py", b"...")
    await sandbox.upload_bytes("repo/deep/deeper/deepest/way/too/far.py", b"...")

    result = await sandbox.exec("find repo -maxdepth 1 -type f | head -n 500")

    names = {Path(line).name for line in result.stdout.splitlines()}
    assert names == {"a.py"}


async def test_exec_find_on_missing_target_fails(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec("find repo/nowhere -maxdepth 4 -type f | head -n 500")

    assert result.exit_code == 1


async def test_exec_find_rejects_a_workspace_escape(sandbox: LocalSandbox, workspace: Path) -> None:
    """Security-audit finding: `find` runs directly on the host filesystem
    for this backend (unlike `SbxSandbox`, isolated in a VM), so a
    `..`-climbing target must be rejected the same way `upload_bytes`/
    `read_file` already reject one via `_resolve_workspace_path` — this
    proves `_find_files` now goes through the same check instead of
    resolving `cwd / target` unchecked."""
    secret = workspace.parent / "secret.txt"
    secret.write_text("host secret outside the sandbox")

    result = await sandbox.exec("find ../secret.txt -maxdepth 4 -type f | head -n 500")

    assert result.exit_code == 2
    assert "escapes sandbox workspace" in result.stderr


# --- exec(): grep/search_code ----------------------------------------------------


async def test_exec_grep_finds_a_literal_match(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("repo/auth.py", b"def check():\n    return validate_token(t)\n")

    result = await sandbox.exec("grep -rn -F validate_token repo | head -n 200")

    assert result.exit_code == 0
    assert "auth.py:2:" in result.stdout
    assert "validate_token" in result.stdout


async def test_exec_grep_no_matches_is_not_an_error(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes("repo/auth.py", b"nothing interesting here\n")

    result = await sandbox.exec("grep -rn -F totally_absent_symbol repo | head -n 200")

    assert result.exit_code == 1
    assert result.stdout == ""


async def test_exec_grep_pattern_with_a_space_is_recovered_correctly(sandbox: LocalSandbox) -> None:
    """`shlex.quote` wraps a spaced pattern in quotes; the recognizer must undo that."""
    await sandbox.upload_bytes("repo/auth.py", b"# access token leaked in logs\n")

    result = await sandbox.exec("grep -rn -F 'access token' repo | head -n 200")

    assert result.exit_code == 0
    assert "access token" in result.stdout


async def test_exec_grep_rejects_a_workspace_escape(sandbox: LocalSandbox, workspace: Path) -> None:
    """Same finding as the `find` case above: before this fix, a crafted PR
    could induce `search_code` to grep real host files — e.g. a plaintext
    OAuth token file — and have the match published into a public PR
    comment via a `Finding`. Proves `_grep` now rejects the escape instead
    of silently reading outside the workspace."""
    secret = workspace.parent / "secret.txt"
    secret.write_text("api_token=super-secret-value")

    result = await sandbox.exec("grep -rn -F api_token ../secret.txt | head -n 200")

    assert result.exit_code == 2
    assert "escapes sandbox workspace" in result.stderr
    assert "super-secret-value" not in result.stdout


# --- exec(): unsupported commands ------------------------------------------------


async def test_exec_rejects_an_unrecognized_command(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec("rm -rf /")

    assert result.exit_code == 127
    assert "does not support" in result.stderr


# --- destroy() -------------------------------------------------------------------


async def test_destroy_removes_the_workspace(workspace: Path, sandbox: LocalSandbox) -> None:
    (workspace / "leftover.txt").write_text("x")

    await sandbox.destroy()

    assert not workspace.exists()


async def test_destroy_never_raises_when_workspace_already_gone(
    sandbox: LocalSandbox, workspace: Path
) -> None:
    workspace.rmdir()

    await sandbox.destroy()  # must not raise


# --- Integration: real tool functions against a real LocalSandbox ---------------
#
# The pure-unit tests for `_list_files_impl`/`_search_code_impl` (in
# test_sandbox_tools.py) use a fake sandbox. These prove the actual
# substitution works end-to-end: the exact command strings AIDA-MATE's tools
# build are the ones LocalSandbox.exec() must recognize.


async def test_list_files_impl_works_against_a_real_local_sandbox(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes(f"{SANDBOX_REPO_DIR}/app/auth.py", b"...")

    output = await _list_files_impl(sandbox, SANDBOX_REPO_DIR, "app")

    assert "auth.py" in output


async def test_read_file_impl_works_against_a_real_local_sandbox(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes(f"{SANDBOX_REPO_DIR}/app/auth.py", b"def login(): ...")

    output = await _read_file_impl(sandbox, SANDBOX_REPO_DIR, "app/auth.py", 20_000)

    assert "def login" in output


async def test_search_code_impl_works_against_a_real_local_sandbox(sandbox: LocalSandbox) -> None:
    await sandbox.upload_bytes(f"{SANDBOX_REPO_DIR}/app/auth.py", b"token.verify(skip_expiry=True)")

    output = await _search_code_impl(sandbox, SANDBOX_REPO_DIR, "skip_expiry", "app")

    assert "auth.py" in output
    assert "skip_expiry" in output
