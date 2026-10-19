from conftest import make_file

from diskcleaner.dupes import duplicate_paths
from diskcleaner.scanner import scan


def test_finds_identical_files_and_keeps_oldest(tmp_path):
    payload = b"x" * 300_000
    make_file(tmp_path / "a" / "movie.mp4", 0, age_days=500, content=payload)
    make_file(tmp_path / "b" / "movie copy.mp4", 0, age_days=100, content=payload)
    make_file(tmp_path / "c" / "movie (1).mp4", 0, age_days=50, content=payload)

    candidates = scan(tmp_path, min_file_size=1).candidates
    dupes = duplicate_paths(candidates)

    original = str(tmp_path / "a" / "movie.mp4")
    assert original not in dupes
    assert set(dupes.values()) == {original}
    assert len(dupes) == 2


def test_same_size_different_content_is_not_a_dupe(tmp_path):
    # same head and tail, different middle - only the full hash can tell
    a = b"h" * 70_000 + b"A" * 100_000 + b"t" * 70_000
    b = b"h" * 70_000 + b"B" * 100_000 + b"t" * 70_000
    make_file(tmp_path / "one.bin", 0, content=a)
    make_file(tmp_path / "two.bin", 0, content=b)

    assert duplicate_paths(scan(tmp_path, min_file_size=1).candidates) == {}
