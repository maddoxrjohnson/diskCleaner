"""The learning part.

A plain logistic regression on top of the features in features.py. I went
with LR over something fancier because (a) there will only ever be a few
hundred labels, and (b) its coefficients are easy to turn into a "here's why"
for each suggestion, which matters a lot more for a tool like this than a
couple points of accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score, recall_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from diskcleaner.features import FEATURE_NAMES

MIN_LABELS = 20
# Mistakenly telling someone to delete a thing they wanted is worse than
# missing some junk, so "keep" labels count for more during training.
KEEP_WEIGHT = 2.0
# How fast the model takes over from the hand-written priors.
BLEND_HALF_POINT = 60

READABLE = {
    "log_size": "large",
    "log_days_since_modified": "not modified in a while",
    "log_days_since_accessed": "not opened in a while",
    "is_dir": "is a folder",
    "log_file_count": "lots of files",
    "depth": "deeply nested",
    "is_duplicate": "duplicate",
    "in_downloads": "in Downloads",
    "in_desktop": "on Desktop",
    "in_temp": "in a temp folder",
    "in_cache": "in a cache folder",
    "in_appdata": "app data",
    "in_projects": "in a projects folder",
}


def readable(name: str) -> str:
    if name in READABLE:
        return READABLE[name]
    if name.startswith("cat="):
        return name[4:].replace("_", " ")
    if name.startswith("ext="):
        return f"{name[4:].replace('_', ' ')} file"
    if name.startswith("path_token_"):
        return "similar location to past choices"
    return name


@dataclass
class TrainReport:
    n_labels: int
    n_delete: int
    n_keep: int
    precision: float | None = None
    recall: float | None = None


class PreferenceModel:
    def __init__(self) -> None:
        self.pipeline = None
        self.n_labels = 0

    @property
    def trained(self) -> bool:
        return self.pipeline is not None

    def _new_pipeline(self):
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000),
        )

    @staticmethod
    def _weights(y: np.ndarray) -> np.ndarray:
        return np.where(y == 0, KEEP_WEIGHT, 1.0)

    def fit(self, X: np.ndarray, y: np.ndarray) -> TrainReport:
        """y is 1 for delete, 0 for keep."""
        y = np.asarray(y, dtype=int)
        n_delete = int(y.sum())
        n_keep = int(len(y) - n_delete)
        report = TrainReport(n_labels=len(y), n_delete=n_delete, n_keep=n_keep)

        if len(y) < MIN_LABELS or min(n_delete, n_keep) == 0:
            self.pipeline = None
            self.n_labels = len(y)
            return report

        folds = min(5, n_delete, n_keep)
        if folds >= 2:
            cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
            preds = cross_val_predict(
                self._new_pipeline(),
                X,
                y,
                cv=cv,
                params={"logisticregression__sample_weight": self._weights(y)},
            )
            report.precision = float(precision_score(y, preds, zero_division=0))
            report.recall = float(recall_score(y, preds, zero_division=0))

        self.pipeline = self._new_pipeline()
        self.pipeline.fit(X, y, logisticregression__sample_weight=self._weights(y))
        self.n_labels = len(y)
        return report

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self.trained or len(X) == 0:
            return np.full(len(X), np.nan)
        return self.pipeline.predict_proba(X)[:, 1]

    def blend(self, model_p: np.ndarray, prior_p: np.ndarray) -> np.ndarray:
        """Mix model output with the rule-based priors.

        With few labels we mostly trust the rules, and the model gets more say
        as labels pile up. It never gets to 100% - the priors keep acting as a
        sanity check.
        """
        if not self.trained:
            return prior_p
        w = self.n_labels / (self.n_labels + BLEND_HALF_POINT)
        return w * model_p + (1 - w) * prior_p

    def explain(self, x: np.ndarray, top: int = 3) -> tuple[list[str], list[str]]:
        """Top reasons pushing toward delete, and toward keep, for one row."""
        if not self.trained:
            return [], []
        scaler = self.pipeline.named_steps["standardscaler"]
        lr = self.pipeline.named_steps["logisticregression"]
        contrib = scaler.transform(x.reshape(1, -1))[0] * lr.coef_[0]

        def pick(order) -> list[str]:
            seen, out = set(), []
            for i in order:
                if abs(contrib[i]) < 0.05:
                    break
                label = readable(FEATURE_NAMES[i])
                if label not in seen:
                    seen.add(label)
                    out.append(label)
                if len(out) == top:
                    break
            return out

        order = np.argsort(contrib)
        return pick(order[::-1]), pick(order)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"pipeline": self.pipeline, "n_labels": self.n_labels}, path)

    @classmethod
    def load(cls, path: Path) -> PreferenceModel:
        m = cls()
        if path.exists():
            try:
                data = joblib.load(path)
                lr = data["pipeline"].named_steps["logisticregression"]
                if lr.coef_.shape[1] == len(FEATURE_NAMES):
                    m.pipeline = data["pipeline"]
                    m.n_labels = data["n_labels"]
            except Exception:
                # stale or corrupt model file, just start fresh
                pass
        return m
