"""Turn a candidate into numbers the model can use."""

from __future__ import annotations

import math
import re
from pathlib import PurePath

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

from diskcleaner.rules import CATEGORIES, EXT_GROUPS, ext_group
from diskcleaner.scanner import Candidate

PATH_HASH_DIMS = 256
LOCATION_FLAGS = ["downloads", "desktop", "temp", "cache", "appdata", "projects"]
EXT_GROUP_NAMES = [*EXT_GROUPS.keys(), "other"]

NUMERIC_NAMES = [
    "log_size",
    "log_days_since_modified",
    "log_days_since_accessed",
    "is_dir",
    "log_file_count",
    "depth",
    "is_duplicate",
]

FEATURE_NAMES = (
    NUMERIC_NAMES
    + [f"cat={c}" for c in CATEGORIES]
    + [f"ext={g}" for g in EXT_GROUP_NAMES]
    + [f"in_{loc}" for loc in LOCATION_FLAGS]
    + [f"path_token_{i}" for i in range(PATH_HASH_DIMS)]
)

_vectorizer = HashingVectorizer(
    n_features=PATH_HASH_DIMS,
    analyzer=lambda s: s,  # we tokenize ourselves
    alternate_sign=False,
    norm="l2",
)

_split = re.compile(r"[\\/_\-. ]+")


def path_tokens(path: str) -> list[str]:
    # folder names only; the file name itself is too unique to generalize from
    parent = str(PurePath(path).parent).lower()
    return [t for t in _split.split(parent) if t and not t.isdigit() and len(t) > 1]


def _days(now: float, ts: float) -> float:
    return max(now - ts, 0) / 86400


def featurize(
    candidates: list[Candidate],
    categories: list[str],
    duplicates: set[str],
    *,
    now: float | list[float],
) -> np.ndarray:
    """One row per candidate. `now` can be per-row so old labels get featurized
    as of when they were made, not as of today."""
    if not candidates:
        return np.zeros((0, len(FEATURE_NAMES)))
    nows = now if isinstance(now, list) else [now] * len(candidates)

    rows = []
    for c, cat, t in zip(candidates, categories, nows, strict=True):
        parts = {p.lower() for p in PurePath(c.path).parts}
        joined = c.path.lower()
        numeric = [
            math.log10(c.size + 1),
            math.log1p(_days(t, c.mtime)),
            math.log1p(_days(t, c.atime)),
            float(c.is_dir),
            math.log10(c.file_count + 1),
            min(c.depth, 20) / 20,
            float(c.path in duplicates),
        ]
        cat_onehot = [float(cat == x) for x in CATEGORIES]
        group = ext_group(c.ext) if not c.is_dir else "other"
        ext_onehot = [float(group == g) for g in EXT_GROUP_NAMES]
        flags = [float(loc in parts or loc in joined) for loc in LOCATION_FLAGS]
        rows.append(numeric + cat_onehot + ext_onehot + flags)

    dense = np.asarray(rows, dtype=float)
    hashed = _vectorizer.transform([path_tokens(c.path) for c in candidates]).toarray()
    return np.hstack([dense, hashed])
