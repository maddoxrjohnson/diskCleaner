from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from diskcleaner.safety import is_protected
from diskcleaner.worktree import gitdir

MB = 1024 * 1024

# Folders that are almost always regenerable. We size them as a single unit
# and don't descend, which keeps scans fast and makes them easy to group.
BULK_DIR_NAMES = {
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    "target",
    "build",
    "dist",
    "out",
    ".cache",
    "cache",
    "caches",
    "temp",
    "tmp",
    "obj",
    "deriveddata",
    "bower_components",
}

# These names are common enough that apps use them for their own stuff too
# (Eclipse keeps plugin jars in a folder called "target", for one). Only treat
# them as build output when the folder next to them looks like a project.
GENERIC_BUILD_NAMES = {"build", "dist", "out", "target", "obj"}
PROJECT_MARKERS = {
    ".git",
    "package.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "build.xml",
    "pubspec.yaml",
    "cargo.toml",
    "go.mod",
    "cmakelists.txt",
    "makefile",
    "meson.build",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "gemfile",
    "composer.json",
    "mix.exs",
}
PROJECT_MARKER_EXTS = (".sln", ".csproj", ".fsproj", ".vbproj", ".vcxproj")
# tool folders whose "build" subfolder is always generated
TOOL_PARENTS = {".dart_tool", ".gradle", ".angular", ".svelte-kit"}
VENV_NAMES = {".venv", "venv", "env"}


def managed_by(name: str, parent_name: str, is_dir: bool) -> str | None:
    """Virtual machines and emulators only make sense as a whole, and deleting
    their files by hand leaves the owning app with a broken entry. We keep
    them together as one item and point at the proper way to remove them."""
    name = name.lower()
    if is_dir:
        if name.endswith(".avd"):
            return "Android emulator: remove it in Android Studio > Device Manager"
        if parent_name == "virtualbox vms":
            return "VirtualBox VM: remove it in VirtualBox (Remove > Delete all files)"
        if name.endswith(".vmwarevm") or parent_name == "virtual machines":
            return "VMware VM: remove it in VMware (Manage > Delete from disk)"
        if name.endswith(".utm"):
            return "UTM VM: remove it in UTM"
        if name.endswith(".pvm"):
            return "Parallels VM: remove it in Parallels Control Center"
        if name == "hyper-v":
            return "Hyper-V VMs: remove them in Hyper-V Manager"
    elif name == "ext4.vhdx":
        return "WSL distro disk: remove it with `wsl --unregister <distro>`"
    return None


def _is_bulk_dir(entry: os.DirEntry, parent_name: str, siblings: set[str]) -> bool:
    name = entry.name.lower()
    if name not in BULK_DIR_NAMES:
        return False
    if name in VENV_NAMES:
        # a real virtualenv always has this; plenty of other things are called "env"
        return os.path.isfile(os.path.join(entry.path, "pyvenv.cfg"))
    if name in GENERIC_BUILD_NAMES:
        return (
            parent_name in TOOL_PARENTS
            or bool(siblings & PROJECT_MARKERS)
            or any(s.endswith(PROJECT_MARKER_EXTS) for s in siblings)
        )
    return True


@dataclass
class Candidate:
    path: str
    size: int
    is_dir: bool
    mtime: float
    atime: float
    file_count: int = 1
    depth: int = 0
    ext: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    root: str
    candidates: list[Candidate]
    dirs_scanned: int = 0
    files_seen: int = 0
    bytes_seen: int = 0
    errors: int = 0


def _is_link_like(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    # Windows junctions aren't symlinks as far as is_symlink() is concerned
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def dir_stats(path: str) -> tuple[int, int, float, float]:
    """Total size, file count, newest mtime and newest atime under a folder."""
    total = count = 0
    newest_m = newest_a = 0.0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if _is_link_like(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_size
                    count += 1
                    newest_m = max(newest_m, st.st_mtime)
                    newest_a = max(newest_a, st.st_atime)
        except OSError:
            continue
    return total, count, newest_m, newest_a


def scan(
    root: str | os.PathLike,
    *,
    min_file_size: int = 50 * MB,
    min_dir_size: int = 10 * MB,
    on_progress: Callable[[int, str], None] | None = None,
) -> ScanResult:
    root = str(Path(root).expanduser().resolve())
    result = ScanResult(root=root, candidates=[])
    root_depth = len(Path(root).parts)

    # iterative on purpose, deep trees (looking at you, node_modules) blow the recursion limit
    stack = [root]
    while stack:
        current = stack.pop()
        if is_protected(current):
            continue
        result.dirs_scanned += 1
        if on_progress and result.dirs_scanned % 200 == 0:
            on_progress(result.dirs_scanned, current)

        try:
            it = os.scandir(current)
        except OSError:
            result.errors += 1
            continue

        has_dlls = False
        found_here: list[Candidate] = []

        with it:
            try:
                entries = list(it)
            except OSError:
                result.errors += 1
                continue
        siblings = {e.name.lower() for e in entries}
        parent_name = os.path.basename(current).lower()

        # a linked git worktree (a .git *file*) is one checkout, judged as a whole
        if current != root and ".git" in siblings and gitdir(current):
            size, count, mtime, atime = dir_stats(current)
            result.files_seen += count
            result.bytes_seen += size
            if size >= min_dir_size:
                result.candidates.append(
                    Candidate(
                        path=current,
                        size=size,
                        is_dir=True,
                        mtime=mtime,
                        atime=atime,
                        file_count=count,
                        depth=len(Path(current).parts) - root_depth,
                        extra={"worktree": True},
                    )
                )
            continue

        for entry in entries:
            try:
                if _is_link_like(entry):
                    continue
                depth = len(Path(entry.path).parts) - root_depth
                if entry.name.lower().endswith((".dll", ".so", ".dylib")):
                    has_dlls = True

                if entry.is_dir(follow_symlinks=False):
                    if is_protected(entry.path):
                        continue
                    owner = managed_by(entry.name, parent_name, is_dir=True)
                    if owner or _is_bulk_dir(entry, parent_name, siblings):
                        size, count, mtime, atime = dir_stats(entry.path)
                        result.files_seen += count
                        result.bytes_seen += size
                        if size >= min_dir_size:
                            result.candidates.append(
                                Candidate(
                                    path=entry.path,
                                    size=size,
                                    is_dir=True,
                                    mtime=mtime,
                                    atime=atime,
                                    file_count=count,
                                    depth=depth,
                                    extra={"managed_by": owner} if owner else {},
                                )
                            )
                    else:
                        stack.append(entry.path)
                    continue

                st = entry.stat(follow_symlinks=False)
            except OSError:
                result.errors += 1
                continue

            result.files_seen += 1
            result.bytes_seen += st.st_size
            if st.st_size >= min_file_size:
                found_here.append(
                    Candidate(
                        path=entry.path,
                        size=st.st_size,
                        is_dir=False,
                        mtime=st.st_mtime,
                        atime=st.st_atime,
                        depth=depth,
                        ext=os.path.splitext(entry.name)[1].lower(),
                    )
                )
                owner = managed_by(entry.name, parent_name, is_dir=False)
                if owner:
                    found_here[-1].extra["managed_by"] = owner

        # An .exe sitting next to DLLs is an installed program, not a setup file.
        # rules.py uses this to tell e.g. a game's exe apart from an installer.
        if has_dlls:
            for c in found_here:
                c.extra["next_to_libraries"] = True
        result.candidates.extend(found_here)

    return result
