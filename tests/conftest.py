import os
import time
from pathlib import Path

import pytest

DAY = 86400


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # keep the db/model out of the real user data dir
    monkeypatch.setenv("DISKCLEANER_HOME", str(tmp_path / "_dc_home"))


def make_file(path: Path, size: int, age_days: float = 400, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if content is not None:
            f.write(content)
        else:
            f.truncate(size)  # sparse, so tests don't actually eat disk
    ts = time.time() - age_days * DAY
    os.utime(path, (ts, ts))
    return path
