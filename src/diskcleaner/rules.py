"""Hand-written heuristics.

These give every candidate a category and a starting guess at how likely it
is to be junk. Before there are enough labels to train on, this is the whole
ranking. Afterwards it's blended with the model (see model.py).
"""

from __future__ import annotations

import re
from pathlib import PurePath

from diskcleaner.scanner import Candidate

PACKAGE_CACHE_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "env",
    "bower_components",
    ".gradle",
    ".tox",
}
BUILD_DIRS = {
    "build",
    "dist",
    "out",
    "target",
    "obj",
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",
    "deriveddata",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
CACHE_DIRS = {"cache", "caches", ".cache"}
TEMP_DIRS = {"temp", "tmp"}

EXT_GROUPS = {
    "installer": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".msix"},
    "disk_image": {".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".qcow2"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst"},
    "video": {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".heic", ".raw", ".cr2", ".nef", ".psd", ".tiff"},
    "document": {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".txt", ".md", ".odt"},
    "log": {".log", ".dmp", ".etl", ".trace"},
    "data": {".csv", ".parquet", ".json", ".db", ".sqlite", ".h5", ".npy", ".pkl", ".pt", ".ckpt"},
    "temp": {".tmp", ".temp", ".bak", ".old", ".crdownload", ".part", ".partial"},
}

CATEGORIES = [
    "worktree",
    "package_cache",
    "build_artifact",
    "cache",
    "temp",
    "installer",
    "disk_image",
    "archive",
    "log",
    "duplicate",
    "media",
    "program",
    "data",
    "document",
    "other",
]

# Starting guesses at P(junk). Tuned by gut feel, which is kind of the point:
# the model is supposed to correct these over time.
PRIORS = {
    "worktree": 0.90,  # only ever a candidate once git says it's finished
    "package_cache": 0.75,
    "build_artifact": 0.80,
    "cache": 0.85,
    "temp": 0.90,
    "installer": 0.75,
    "disk_image": 0.55,
    "archive": 0.45,
    "log": 0.80,
    "duplicate": 0.70,
    "media": 0.20,
    "program": 0.05,
    "data": 0.25,
    "document": 0.10,
    "other": 0.25,
}


def ext_group(ext: str) -> str:
    ext = ext.lower()
    for group, exts in EXT_GROUPS.items():
        if ext in exts:
            return group
    return "other"


def categorize(c: Candidate, *, is_duplicate: bool = False) -> str:
    parts = [p.lower() for p in PurePath(c.path).parts]
    name = parts[-1] if parts else ""

    if c.is_dir:
        if c.extra.get("worktree"):
            return "worktree"
        if name in PACKAGE_CACHE_DIRS:
            return "package_cache"
        if name in BUILD_DIRS:
            return "build_artifact"
        if name in CACHE_DIRS:
            return "cache"
        if name in TEMP_DIRS:
            return "temp"
        return "other"

    if is_duplicate:
        return "duplicate"

    # Only look at folders from the scan root down. Otherwise scanning
    # something that happens to live under /tmp makes everything "temp".
    ancestors = parts[-(c.depth + 1) : -1] if c.depth > 0 else parts[:-1]
    group = ext_group(c.ext)
    if group == "temp" or any(p in TEMP_DIRS for p in ancestors):
        return "temp"
    if any(p in CACHE_DIRS for p in ancestors):
        return "cache"
    if group in ("video", "audio", "image"):
        return "media"
    if group == "installer":
        return "installer" if looks_like_installer(c, ancestors) else "program"
    if group in CATEGORIES:
        return group
    return "other"


_installer_name = re.compile(
    r"setup|install|redist|update|upgrade"
    r"|x64|x86|amd64|arm64|win32|win64"
    r"|\d+\.\d+|\d{8}"  # version numbers like 1.15.1, or dates like 20250830
)


def looks_like_installer(c: Candidate, ancestors: list[str]) -> bool:
    """Package formats are always installers. A bare .exe needs more evidence,
    since games and portable apps are .exe files too."""
    if c.ext != ".exe":
        return True
    if c.extra.get("next_to_libraries"):
        return False
    stem = PurePath(c.path).stem.lower()
    if _installer_name.search(stem):
        return True
    # a loose exe in Downloads or on the Desktop was almost certainly downloaded
    parent = ancestors[-1] if ancestors else ""
    return parent in ("downloads", "desktop")


def prior(c: Candidate, category: str, *, now: float) -> float:
    p = PRIORS.get(category, 0.25)
    if category == "worktree":
        return p  # finished is finished, age doesn't say anything here
    parts = {x.lower() for x in PurePath(c.path).parts}
    # mtime, not atime: Windows Defender and search indexing bump access
    # times all the time, so atime makes everything look recently used.
    days_idle = (now - c.mtime) / 86400

    # Stuff in Downloads that hasn't been touched in months is the classic case
    if "downloads" in parts and days_idle > 90:
        p += 0.10
    if days_idle > 365:
        p += 0.05
    elif days_idle < 90:
        p -= 0.10

    return min(max(p, 0.01), 0.99)


def explain(c: Candidate, category: str, *, now: float) -> list[str]:
    """Short human-readable reasons, used before the model has learned anything."""
    reasons = [category.replace("_", " ")]
    days_idle = int((now - c.mtime) / 86400)
    if days_idle > 60:
        reasons.append(f"not modified in {days_idle} days")
    if "downloads" in {x.lower() for x in PurePath(c.path).parts}:
        reasons.append("in Downloads")
    if c.is_dir and c.file_count > 1000:
        reasons.append(f"{c.file_count:,} files")
    return reasons
