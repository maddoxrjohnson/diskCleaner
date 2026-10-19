import random
import time

import numpy as np

from diskcleaner import analyze
from diskcleaner.model import MIN_LABELS, PreferenceModel
from diskcleaner.scanner import Candidate
from diskcleaner.store import Label

NOW = time.time()
DAY = 86400


def fake_labels(n=120, seed=0):
    """A user who always keeps things under /projects/ and dumps old installers."""
    rng = random.Random(seed)
    labels = []
    for i in range(n):
        idle = rng.uniform(40, 900) * DAY
        if i % 2 == 0:
            path, ext, cat, label = (
                f"/home/me/projects/ml{i}/data/train{i}.zip",
                ".zip",
                "archive",
                "keep",
            )
        else:
            path, ext, cat, label = f"/home/me/Downloads/setup{i}.zip", ".zip", "archive", "delete"
        c = Candidate(path, rng.randint(10**8, 10**10), False, NOW - idle, NOW - idle, ext=ext)
        labels.append(Label(c, label, cat, False, NOW))
    return labels


def test_cold_start_uses_priors():
    model = PreferenceModel()
    X, y = analyze.label_matrix(fake_labels(MIN_LABELS - 1))
    report = model.fit(X, y)
    assert not model.trained
    assert report.precision is None
    priors = np.array([0.3, 0.9])
    assert np.array_equal(model.blend(np.array([np.nan, np.nan]), priors), priors)


def test_one_class_does_not_train():
    labels = [lab for lab in fake_labels(80) if lab.label == "delete"]
    model = PreferenceModel()
    model.fit(*analyze.label_matrix(labels))
    assert not model.trained


def test_learns_a_location_preference():
    model = PreferenceModel()
    X, y = analyze.label_matrix(fake_labels(120))
    report = model.fit(X, y)

    assert model.trained
    assert report.precision > 0.9

    test = analyze.label_matrix(fake_labels(40, seed=1))[0]
    p = model.predict(test)
    # even rows are the "keep" folder
    assert p[0::2].max() < 0.5 < p[1::2].min()


def test_explain_gives_reasons():
    model = PreferenceModel()
    X, y = analyze.label_matrix(fake_labels(120))
    model.fit(X, y)
    why, why_not = model.explain(X[1])
    assert why
    assert "in Downloads" in why or "similar location to past choices" in why


def test_blend_shifts_toward_model_with_more_labels():
    model = PreferenceModel()
    model.fit(*analyze.label_matrix(fake_labels(40)))
    few = model.blend(np.array([1.0]), np.array([0.0]))[0]
    model.fit(*analyze.label_matrix(fake_labels(400)))
    many = model.blend(np.array([1.0]), np.array([0.0]))[0]
    assert 0 < few < many < 1


def test_save_and_load_roundtrip(tmp_path):
    model = PreferenceModel()
    X, y = analyze.label_matrix(fake_labels(60))
    model.fit(X, y)
    path = tmp_path / "m.joblib"
    model.save(path)

    loaded = PreferenceModel.load(path)
    assert loaded.trained
    assert np.allclose(loaded.predict(X), model.predict(X))


def test_load_garbage_starts_fresh(tmp_path):
    path = tmp_path / "m.joblib"
    path.write_bytes(b"not a model")
    assert not PreferenceModel.load(path).trained
