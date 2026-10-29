"""deploy/update.sh against a local git remote, with a stub venv.

The stub python records what it was asked to do and succeeds, so these
tests check the script's decisions without installing anything.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "update.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("tar")), reason="needs git and tar"
)

STUB = """#!/bin/sh
echo "$*" >> "$(dirname "$0")/calls.log"
"""


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    commit(origin, "one")

    app = tmp_path / "app"
    git(tmp_path, "clone", "-q", str(origin), str(app))
    (app / "deploy").mkdir()
    shutil.copy(SCRIPT, app / "deploy" / "update.sh")
    bin_dir = app / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "ditta-ia-adapter"):
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    return origin, app


def run_update(app: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(app / "deploy" / "update.sh"), "--no-restart"],
                          cwd=app, capture_output=True, text=True, check=True)


def calls(app: Path) -> str:
    log = app / ".venv" / "bin" / "calls.log"
    return log.read_text() if log.exists() else ""


def test_installs_and_records_the_commit(setup) -> None:
    _, app = setup
    head = git(app, "rev-parse", "HEAD")
    result = run_update(app)
    assert f"installed {head}" in result.stdout
    assert (app / ".venv" / "installed-commit").read_text().strip() == head
    assert "-m pytest" in calls(app)


def test_nothing_to_do_once_installed(setup) -> None:
    _, app = setup
    run_update(app)
    (app / ".venv" / "bin" / "calls.log").unlink()
    result = run_update(app)
    assert "already running" in result.stdout
    assert calls(app) == ""


def test_manual_pull_still_installs(setup) -> None:
    # The bug: after a manual `git pull`, HEAD matched origin and the script
    # exited without installing, leaving the old code running.
    origin, app = setup
    run_update(app)
    newer = commit(origin, "two")
    git(app, "pull", "-q", "--ff-only")
    result = run_update(app)
    assert f"installed {newer}" in result.stdout
    assert (app / ".venv" / "installed-commit").read_text().strip() == newer
