import asyncio
import time

from diskcleaner.analyze import Suggestion
from diskcleaner.scanner import Candidate
from diskcleaner.store import StoredCandidate
from diskcleaner.tui import ReviewApp

OLD = time.time() - 400 * 86400


def sugg(path, size, category="installer", is_dir=False):
    c = Candidate(path, size, is_dir, OLD, OLD)
    return Suggestion(StoredCandidate(c, category, 0.9, None, None), 0.9, None, ["old"])


def run(app, keys):
    async def go():
        async with app.run_test() as pilot:
            for k in keys:
                await pilot.press(k)
            await pilot.pause()

    asyncio.run(go())


def test_labels_and_groups():
    items = [
        sugg("/h/Downloads/a.exe", 500),
        sugg("/h/p1/node_modules", 200, "package_cache", is_dir=True),
        sugg("/h/p2/node_modules", 100, "package_cache", is_dir=True),
    ]
    calls = []
    app = ReviewApp(items, {}, lambda s, lab: calls.append((s.path, lab)), lambda: "ok")
    # first row: a.exe, second row: the node_modules group
    run(app, ["d", "k"])

    assert app.labels == {
        "/h/Downloads/a.exe": "delete",
        "/h/p1/node_modules": "keep",
        "/h/p2/node_modules": "keep",
    }
    assert len(calls) == 3
    assert app.changed == 3


def test_undo_and_expand():
    items = [
        sugg("/h/p1/node_modules", 200, "package_cache", is_dir=True),
        sugg("/h/p2/node_modules", 100, "package_cache", is_dir=True),
    ]
    app = ReviewApp(items, {}, lambda s, lab: None, lambda: "ok")
    # expand the group, move to its first child, mark it, then undo
    run(app, ["space", "down", "d", "up", "u"])
    assert app.labels == {}
    assert len(app.rows) == 3  # group + two children
