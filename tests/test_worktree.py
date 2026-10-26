import shutil
import subprocess

import pytest
from conftest import make_file

from diskcleaner import analyze, worktree
from diskcleaner.scanner import MB, scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "proj"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text("node_modules/\n.env\n")
    (main / "app.py").write_text("print('hi')\n")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "first")
    return main


def add_worktree(repo, name, *, new_commit=False):
    path = repo / ".claude" / "worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    # a fresh checkout, the 30-day rule would normally hide all of this
    make_file(path / "node_modules" / "big.bin", 30 * MB, age_days=0)
    if new_commit:
        (path / "new.py").write_text("x = 1\n")
        git(path, "add", ".")
        git(path, "commit", "-q", "-m", "only here")
    return path


def verdicts(root):
    items = analyze.classify(scan(root))
    return {i.candidate.path.rsplit("worktrees", 1)[-1][1:]: i for i in items}


def test_finished_worktree_is_suggested_even_when_new(repo):
    add_worktree(repo, "done")

    item = verdicts(repo)["done"]

    assert item.rejected is None
    assert item.category == "worktree"
    assert "nothing uncommitted" in item.candidate.extra["finished"]


def test_uncommitted_changes_keep_it(repo):
    path = add_worktree(repo, "busy")
    (path / "app.py").write_text("print('changed')\n")

    item = verdicts(repo)["busy"]

    assert item.rejected.startswith("git worktree that isn't finished: 1 uncommitted")


def test_untracked_file_keeps_it(repo):
    path = add_worktree(repo, "notes")
    (path / "todo.txt").write_text("remember this\n")

    assert "uncommitted" in verdicts(repo)["notes"].rejected


def test_ignored_env_file_keeps_it(repo):
    path = add_worktree(repo, "secrets")
    (path / ".env").write_text("KEY=abc\n")

    assert ".env" in verdicts(repo)["secrets"].rejected


def test_commit_only_on_its_own_branch_counts_as_finished_only_if_shared(repo):
    add_worktree(repo, "solo", new_commit=True)
    assert "aren't on any other branch" in verdicts(repo)["solo"].rejected

    git(repo, "branch", "backup", "solo")
    assert verdicts(repo)["solo"].rejected is None


def test_submodule_git_file_is_not_a_worktree(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert worktree.gitdir(str(sub)) is None
