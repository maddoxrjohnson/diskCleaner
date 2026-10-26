"""Git worktrees that have done their job.

Coding agents (and people) make a worktree per task, then never clean them
up. Each one is a full checkout, often with its own node_modules. They're
usually brand new, so the 30-day rule would hide them, but whether one is
finished doesn't depend on age, git can tell us directly:

  * no uncommitted or untracked changes
  * no ignored files that look like secrets or data (.env, *.db, ...)
  * the commit it's on is also on some other branch, local or remote

Anything we can't check counts as not finished.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass

GIT_TIMEOUT = 30

# ignored files that probably aren't regenerable, so a worktree holding them isn't finished
_risky = re.compile(
    r"(^|/)(\.env[^/]*|[^/]*\.(pem|key|p12|pfx|db|sqlite3?)|[^/]*(secret|credential)[^/]*)$",
    re.IGNORECASE,
)
_windows_path = re.compile(r"^[a-zA-Z]:[\\/]")


@dataclass
class Check:
    finished: bool
    reason: str


def gitdir(path: str) -> str | None:
    """Where a linked worktree's .git file points, or None if it isn't one."""
    try:
        with open(os.path.join(path, ".git"), encoding="utf-8") as f:
            line = f.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    target = line[len("gitdir:") :].strip()
    # submodules have a .git file too, but theirs points into .git/modules
    if "/worktrees/" not in target.replace("\\", "/"):
        return None
    return target


def _git(path: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def check(path: str) -> Check:
    target = gitdir(path)
    if target is None:
        return Check(False, "not a git worktree")
    if sys.platform != "win32" and _windows_path.match(target):
        # made by Windows git; WSL's git can't follow C:/ paths (and hangs trying)
        return Check(False, "created on Windows, scan it from Windows to check it")

    status = _git(path, "status", "--porcelain", "--ignored")
    if status is None:
        return Check(False, "git couldn't read it")
    # paths with spaces come back "quoted"
    lines = [ln[:3] + ln[3:].strip('"') for ln in status.splitlines() if ln.strip()]
    changed = [ln for ln in lines if not ln.startswith("!!")]
    if changed:
        return Check(False, f"{len(changed)} uncommitted change(s), e.g. {changed[0][3:]}")
    risky = [
        ln[3:].rstrip("/")
        for ln in lines
        if ln.startswith("!!") and _risky.search(ln[3:].rstrip("/"))
    ]
    if risky:
        return Check(False, f"has files git doesn't track, e.g. {risky[0]}")

    own = (_git(path, "symbolic-ref", "-q", "HEAD") or "").strip()
    refs = _git(
        path,
        "for-each-ref",
        "--contains",
        "HEAD",
        "--format=%(refname)",
        "refs/heads",
        "refs/remotes",
    )
    if refs is None:
        return Check(False, "git couldn't read it")
    others = [r for r in refs.split() if r != own and not r.endswith("/HEAD")]
    if not others:
        return Check(False, "has commits that aren't on any other branch")
    # a remote copy is the reassuring one, so name that if there is one
    best = next((r for r in others if r.startswith("refs/remotes/")), others[0])
    short = best.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    return Check(True, f"finished git worktree: nothing uncommitted, commits are on {short}")
