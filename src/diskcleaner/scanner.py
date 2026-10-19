from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from diskcleaner.safety import is_protected

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

        with it:
            for entry in it:
                try:
                    if _is_link_like(entry):
                        continue
                    depth = len(Path(entry.path).parts) - root_depth

                    if entry.is_dir(follow_symlinks=False):
                        if is_protected(entry.path):
                            continue
                        if entry.name.lower() in BULK_DIR_NAMES:
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
                    result.candidates.append(
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

    return result
