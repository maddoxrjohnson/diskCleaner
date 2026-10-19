"""Glue between the scanner, the safety rules, the heuristics and the model."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from diskcleaner import rules, safety
from diskcleaner.dupes import duplicate_paths
from diskcleaner.features import featurize
from diskcleaner.model import PreferenceModel
from diskcleaner.scanner import ScanResult
from diskcleaner.store import Label, StoredCandidate

DEFAULT_THRESHOLD = 0.7


@dataclass
class Suggestion:
    item: StoredCandidate
    score: float
    model_p: float | None
    reasons: list[str] = field(default_factory=list)
    keep_reasons: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.item.candidate.path

    @property
    def size(self) -> int:
        return self.item.candidate.size

    @property
    def group_key(self) -> str:
        """Things that get rolled up together in the review screen."""
        c = self.item.candidate
        if c.is_dir:
            return c.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        return self.item.category


def classify(
    result: ScanResult,
    *,
    min_age_days: int = safety.DEFAULT_MIN_AGE_DAYS,
    now: float | None = None,
) -> list[StoredCandidate]:
    """Run duplicate detection, safety checks and heuristics over a fresh scan."""
    now = time.time() if now is None else now
    dupes = duplicate_paths(result.candidates)
    out = []
    for c in result.candidates:
        dup_of = dupes.get(c.path)
        rejected = safety.reject_reason(
            c.path,
            c.mtime,
            is_dir=c.is_dir,
            is_duplicate=dup_of is not None,
            managed_by=c.extra.get("managed_by"),
            min_age_days=min_age_days,
            now=now,
        )
        category = rules.categorize(c, is_duplicate=dup_of is not None)
        prior = rules.prior(c, category, now=now)
        out.append(StoredCandidate(c, category, prior, dup_of, rejected))
    return out


def _features(items: list[StoredCandidate], now: float | list[float]) -> np.ndarray:
    return featurize(
        [i.candidate for i in items],
        [i.category for i in items],
        {i.candidate.path for i in items if i.duplicate_of},
        now=now,
    )


def label_matrix(labels: list[Label]) -> tuple[np.ndarray, np.ndarray]:
    usable = [lab for lab in labels if lab.label in ("keep", "delete")]
    items = [
        StoredCandidate(lab.candidate, lab.category, 0.0, "?" if lab.is_duplicate else None, None)
        for lab in usable
    ]
    X = _features(items, [lab.labeled_at for lab in usable])
    y = np.array([lab.label == "delete" for lab in usable], dtype=int)
    return X, y


def score(
    items: list[StoredCandidate],
    model: PreferenceModel,
    *,
    now: float | None = None,
) -> list[Suggestion]:
    """Score everything and sort by expected payoff (size x probability)."""
    now = time.time() if now is None else now
    # belt and braces: rejected items should already be filtered out by the store
    items = [i for i in items if i.rejected is None]
    if not items:
        return []

    X = _features(items, now)
    priors = np.array([i.prior for i in items])
    model_p = model.predict(X)
    final = model.blend(model_p, priors)

    out = []
    for idx, item in enumerate(items):
        if model.trained:
            why, why_not = model.explain(X[idx])
            mp = float(model_p[idx])
        else:
            why, why_not, mp = rules.explain(item.candidate, item.category, now=now), [], None
        if item.duplicate_of:
            why = [*why, f"copy of {item.duplicate_of}"]
        out.append(Suggestion(item, float(final[idx]), mp, why, why_not))

    out.sort(key=lambda s: s.size * s.score, reverse=True)
    return out


def suggestions(scored: list[Suggestion], threshold: float = DEFAULT_THRESHOLD) -> list[Suggestion]:
    return [s for s in scored if s.score >= threshold]
