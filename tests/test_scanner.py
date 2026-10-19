import os

import pytest
from conftest import make_file

from diskcleaner.scanner import MB, scan


def test_finds_big_files_only(tmp_path):
    make_file(tmp_path / "big.bin", 60 * MB)
    make_file(tmp_path / "small.bin", 1 * MB)
    make_file(tmp_path / "nested" / "deep" / "huge.iso", 200 * MB)

    result = scan(tmp_path)
    found = {os.path.basename(c.path): c for c in result.candidates}

    assert set(found) == {"big.bin", "huge.iso"}
    assert found["huge.iso"].size == 200 * MB
    assert found["huge.iso"].ext == ".iso"
    assert found["huge.iso"].depth == 3
    assert result.files_seen == 3


def test_bulk_dirs_are_one_candidate(tmp_path):
    nm = tmp_path / "proj" / "node_modules"
    for i in range(30):
        make_file(nm / f"pkg{i}" / "index.js", 1 * MB)

    result = scan(tmp_path)

    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.is_dir
    assert c.path.endswith("node_modules")
    assert c.size == 30 * MB
    assert c.file_count == 30


def test_small_bulk_dirs_are_ignored(tmp_path):
    make_file(tmp_path / "proj" / "__pycache__" / "x.pyc", 1000)
    assert scan(tmp_path).candidates == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on windows")
def test_does_not_follow_symlinks(tmp_path):
    make_file(tmp_path / "real" / "big.bin", 60 * MB)
    (tmp_path / "real" / "loop").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "link.bin").symlink_to(tmp_path / "real" / "big.bin")

    result = scan(tmp_path)

    assert [os.path.basename(c.path) for c in result.candidates] == ["big.bin"]


def test_skips_git_internals(tmp_path):
    make_file(tmp_path / "repo" / ".git" / "objects" / "pack.pack", 100 * MB)
    assert scan(tmp_path).candidates == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="chmod semantics")
def test_unreadable_dirs_dont_crash(tmp_path):
    locked = tmp_path / "locked"
    make_file(locked / "big.bin", 60 * MB)
    make_file(tmp_path / "ok.bin", 60 * MB)
    locked.chmod(0)
    try:
        result = scan(tmp_path)
    finally:
        locked.chmod(0o755)
    assert [os.path.basename(c.path) for c in result.candidates] == ["ok.bin"]
    assert result.errors >= 1


def test_marks_files_next_to_libraries(tmp_path):
    make_file(tmp_path / "Game" / "game.exe", 60 * MB)
    make_file(tmp_path / "Game" / "engine.dll", 1 * MB)
    make_file(tmp_path / "Downloads" / "setup.exe", 60 * MB)

    found = {os.path.basename(c.path): c for c in scan(tmp_path).candidates}

    assert found["game.exe"].extra.get("next_to_libraries")
    assert not found["setup.exe"].extra.get("next_to_libraries")


def test_skips_steam_library(tmp_path):
    make_file(tmp_path / "Steam" / "steamapps" / "common" / "Game" / "game.exe", 60 * MB)
    assert scan(tmp_path).candidates == []
