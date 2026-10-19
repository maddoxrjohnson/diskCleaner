"""Duplicate file detection.

Classic three-pass approach so we don't hash gigabytes for nothing:
group by size, then by a cheap hash of the first and last chunk, and only
then do a full hash on whatever still collides.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict

from diskcleaner.scanner import Candidate

CHUNK = 64 * 1024


def _quick_hash(path: str, size: int) -> str | None:
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            h.update(f.read(CHUNK))
            if size > CHUNK * 2:
                f.seek(-CHUNK, os.SEEK_END)
                h.update(f.read(CHUNK))
    except OSError:
        return None
    return h.hexdigest()


def _full_hash(path: str) -> str | None:
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _group(items: list[Candidate], key) -> list[list[Candidate]]:
    buckets: dict[object, list[Candidate]] = defaultdict(list)
    for c in items:
        k = key(c)
        if k is not None:
            buckets[k].append(c)
    return [g for g in buckets.values() if len(g) > 1]


def find_duplicates(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Groups of identical files. Folders are ignored."""
    files = [c for c in candidates if not c.is_dir and c.size > 0]
    groups = []
    for same_size in _group(files, lambda c: c.size):
        for same_quick in _group(same_size, lambda c: _quick_hash(c.path, c.size)):
            groups.extend(_group(same_quick, lambda c: _full_hash(c.path)))
    return groups


def duplicate_paths(candidates: list[Candidate]) -> dict[str, str]:
    """Map of duplicate path -> the copy we consider the "original".

    The original is the oldest file (ties broken by shortest path), and it's
    never included as a key, so at least one copy always survives.
    """
    out = {}
    for group in find_duplicates(candidates):
        group.sort(key=lambda c: (c.mtime, len(c.path), c.path))
        original = group[0].path
        for c in group[1:]:
            out[c.path] = original
    return out
