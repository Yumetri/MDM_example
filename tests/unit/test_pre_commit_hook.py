import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT_HOOK = REPOSITORY_ROOT / ".githooks" / "pre-commit"


def _run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _prepare_repository(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run("git", "init", "--quiet", cwd=repository)

    (repository / ".gitignore").write_text(".ignored/\n")
    tracked_file = repository / "tracked.txt"
    tracked_file.write_text("staged\n")
    _run("git", "add", ".gitignore", "tracked.txt", cwd=repository)

    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    invocation_log = tmp_path / "make-invocation.txt"
    git_environment_log = tmp_path / "make-git-environment.txt"
    make_stub = binary_directory / "make"
    make_stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" > "$MAKE_INVOCATION_LOG"\n'
        'printf "GIT_DIR=%s\\nGIT_INDEX_FILE=%s\\n" "${GIT_DIR-unset}" '
        '"${GIT_INDEX_FILE-unset}" > "$GIT_ENVIRONMENT_LOG"\n'
    )
    make_stub.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{binary_directory}:{environment['PATH']}"
    environment["MAKE_INVOCATION_LOG"] = str(invocation_log)
    environment["GIT_ENVIRONMENT_LOG"] = str(git_environment_log)
    return repository, invocation_log, environment


def _run_hook(repository: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PRE_COMMIT_HOOK)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_pre_commit_checks_a_staged_only_snapshot(tmp_path: Path) -> None:
    repository, invocation_log, environment = _prepare_repository(tmp_path)

    result = _run_hook(repository, environment)

    assert result.returncode == 0
    assert invocation_log.read_text() == "ci-check\n"


@pytest.mark.unit
def test_pre_commit_does_not_leak_git_local_environment_to_ci_check(tmp_path: Path) -> None:
    repository, invocation_log, environment = _prepare_repository(tmp_path)
    environment["GIT_DIR"] = str(repository / ".git")
    environment["GIT_INDEX_FILE"] = str(repository / ".git" / "index")

    result = _run_hook(repository, environment)

    assert result.returncode == 0
    assert invocation_log.read_text() == "ci-check\n"
    assert (tmp_path / "make-git-environment.txt").read_text() == (
        "GIT_DIR=unset\nGIT_INDEX_FILE=unset\n"
    )


@pytest.mark.unit
def test_pre_commit_rejects_unstaged_changes_to_a_staged_file(tmp_path: Path) -> None:
    repository, invocation_log, environment = _prepare_repository(tmp_path)
    (repository / "tracked.txt").write_text("unstaged\n")

    result = _run_hook(repository, environment)

    assert result.returncode != 0
    assert not invocation_log.exists()


@pytest.mark.unit
def test_pre_commit_rejects_non_ignored_untracked_files(tmp_path: Path) -> None:
    repository, invocation_log, environment = _prepare_repository(tmp_path)
    (repository / "untracked.txt").write_text("untracked\n")

    result = _run_hook(repository, environment)

    assert result.returncode != 0
    assert not invocation_log.exists()


@pytest.mark.unit
def test_pre_commit_allows_ignored_files(tmp_path: Path) -> None:
    repository, invocation_log, environment = _prepare_repository(tmp_path)
    ignored_directory = repository / ".ignored"
    ignored_directory.mkdir()
    (ignored_directory / "cache.txt").write_text("ignored\n")

    result = _run_hook(repository, environment)

    assert result.returncode == 0
    assert invocation_log.read_text() == "ci-check\n"
