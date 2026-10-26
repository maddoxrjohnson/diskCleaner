"""Where did all my space go? A read-only size breakdown of a folder.

Unlike the scanner this looks at everything, system folders included, since
it only measures. It also notes how much of each folder was written to
recently, and compares against the previous run to show what grew.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from diskcleaner.scanner import MB, _is_link_like

DAY = 86400

# How deep we keep a per-folder breakdown. Anything below this still counts
# towards its ancestor, we just don't remember it separately.
KEEP_DEPTH = 8

# things that show up near the top of most full drives, and what to do about them
NOTES = {
    "pagefile.sys": "Windows virtual memory, sized automatically (Advanced system settings)",
    "hiberfil.sys": "hibernation file, `powercfg /h off` removes it if you never hibernate",
    "swapfile.sys": "swap for Store apps, managed by Windows",
    "ext4.vhdx": "WSL disk, only ever grows: clean inside WSL then compact it",
    "docker_data.vhdx": "Docker's disk: `docker system prune`, then compact it",
    "swap.vhdx": "WSL swap, in use while WSL is running",
    "system volume information": "restore points (System Protection settings)",
    "$recycle.bin": "the Recycle Bin, empty it",
    "windows.old": "previous Windows install, remove with Disk Cleanup",
    "winsxs": "Windows components, Disk Cleanup > Windows Update Cleanup trims it",
    "softwaredistribution": "Windows Update downloads",
    "installer": "if this is C:\\Windows\\Installer, uninstallers need it, leave it",
    "steamapps": "Steam games, uninstall from Steam",
    "node_modules": "npm packages, `npm install` brings them back",
    ".gradle": "Gradle caches, re-downloaded on the next build",
    ".m2": "Maven cache, re-downloaded on the next build",
    "npm-cache": "npm download cache, `npm cache clean --force`",
    ".cache": "app caches, usually safe to clear",
    "temp": "temporary files",
    "avd": "Android emulators, manage them in Android Studio > Device Manager",
    "system-images": "Android emulator images, remove in Android Studio > SDK Manager",
    "service worker": "offline website data, cleared with the browser cache",
    "docker": "Docker Desktop data",
    "onedrive": "synced files, right click > Free up space keeps them online only",
}


# notes for folders that hold a mix of things, worth looking inside
MIXED = {"temp", ".cache", "docker", "onedrive"}


def note_for(name: str) -> str | None:
    name = name.lower()
    if name.endswith(".avd"):
        return "Android emulator, delete or wipe it in Android Studio > Device Manager"
    return NOTES.get(name)


@dataclass
class Node:
    path: str
    name: str
    is_dir: bool = True
    depth: int = 0
    size: int = 0  # whole subtree, filled in by _total()
    recent: int = 0  # bytes in files modified within the window
    own_size: int = 0
    own_recent: int = 0
    children: list[Node] = field(default_factory=list)

    def walk(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.children)


@dataclass
class Usage:
    root: Node
    files: int = 0
    errors: int = 0
    seconds: float = 0.0


def _total(node: Node) -> None:
    # post-order without recursion; the tree is shallow but let's not risk it
    order = list(node.walk())
    for n in reversed(order):
        n.size = n.own_size + sum(c.size for c in n.children)
        n.recent = n.own_recent + sum(c.recent for c in n.children)


def measure(
    root: str | os.PathLike,
    *,
    since_days: float = 7,
    big_file: int = 100 * MB,
    keep_depth: int = KEEP_DEPTH,
    now: float | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> Usage:
    started = time.time()
    now = started if now is None else now
    cutoff = now - since_days * DAY
    root = os.path.abspath(os.path.expanduser(str(root)))
    top = Node(root, root)
    usage = Usage(top)

    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        root_dev = 0

    dirs = 0
    # (folder to read, node its sizes go to, depth of that folder)
    stack: list[tuple[str, Node, int]] = [(root, top, 0)]
    while stack:
        current, node, depth = stack.pop()
        dirs += 1
        if on_progress and dirs % 500 == 0:
            on_progress(dirs, current)
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            usage.errors += 1
            continue

        for entry in entries:
            try:
                if _is_link_like(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    # don't wander into other drives mounted below this one (/proc, /mnt/c, ...).
                    # Windows leaves st_dev at 0 here, which is fine, it can't mount like that
                    dev = entry.stat(follow_symlinks=False).st_dev
                    if root_dev and dev and dev != root_dev:
                        continue
                    if depth + 1 <= keep_depth:
                        child = Node(entry.path, entry.name, depth=depth + 1)
                        node.children.append(child)
                        stack.append((entry.path, child, depth + 1))
                    else:
                        stack.append((entry.path, node, depth + 1))
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                usage.errors += 1
                continue

            usage.files += 1
            recent = st.st_size if st.st_mtime >= cutoff else 0
            if st.st_size >= big_file:
                # big files get their own row so e.g. pagefile.sys shows up by name,
                # even when they're deeper than we keep folders for
                node.children.append(
                    Node(
                        entry.path,
                        entry.name,
                        False,
                        depth + 1,
                        own_size=st.st_size,
                        own_recent=recent,
                    )
                )
            else:
                node.own_size += st.st_size
                node.own_recent += recent

    _total(top)
    usage.seconds = time.time() - started
    return usage


def growth(node: Node, previous: dict[str, int], snapshot_depth: int) -> int | None:
    """How much a folder grew since the last snapshot. None if we can't tell."""
    if node.depth > snapshot_depth:
        return None
    # the snapshot skips tiny folders, so a missing path means it was (close to) empty
    return node.size - previous.get(node.path, 0)


def gross(root: Node, value: Callable[[Node], int | None]) -> dict[int, int]:
    """Only the increases, per node, keyed by id(node).

    Net change hides things: if you deleted 20 GB from Temp and something else
    in AppData grew 10 GB, AppData looks like it shrank and the 10 GB never
    shows up. So add up the positive parts from the bottom instead.
    Pass a negated value to get the decreases.
    """
    out: dict[int, int] = {}
    for n in reversed(list(root.walk())):
        v = value(n)
        if v is None:
            continue
        kids = [(c, value(c)) for c in n.children]
        own = v - sum(cv for _, cv in kids if cv is not None)
        out[id(n)] = max(own, 0) + sum(out.get(id(c), 0) for c, _ in kids)
    return out


def hotspots(
    root: Node,
    value: Callable[[Node], int | None],
    *,
    threshold: int,
    limit: int = 10,
    max_depth: int = 6,
) -> list[tuple[Node, int]]:
    """Narrow a number down to the folders responsible for it.

    Keep going into children while they explain most of their parent's value,
    so instead of "C:\\Users grew 20 GB" you get the actual folder that grew.
    Stops at single things we have a note for (node_modules, an emulator) and
    after a few levels, since lib/x86_64/foo isn't more useful than its app.
    """

    def visit(node: Node) -> list[tuple[Node, int]]:
        v = value(node)
        if v is None or v < threshold:
            return []
        known = node is not root and node.name.lower() not in MIXED and note_for(node.name)
        if node.depth - root.depth >= max_depth or known:
            return [(node, v)]
        found = []
        for c in node.children:
            found += visit(c)
        explained = sum(value(c) or 0 for c in node.children if (value(c) or 0) >= threshold)
        if found and explained >= 0.5 * v:
            return found
        return [(node, v)]

    spots = visit(root)
    spots.sort(key=lambda s: -s[1])
    return spots[:limit]


def snapshot_rows(root: Node, *, min_size: int = MB) -> list[tuple[str, int]]:
    return [(n.path, n.size) for n in root.walk() if n.size >= min_size]
