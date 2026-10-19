import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import make_file

from diskcleaner.export import ExportItem, render, to_windows_path, write_script

NASTY = '/home/me/Downloads/it\'s a "weird" name $(rm -rf ~) ünïcode.zip'


def test_windows_path_conversion():
    assert to_windows_path("/mnt/c/Users/me/a b.zip") == "C:\\Users\\me\\a b.zip"
    assert to_windows_path("/home/me/x") == "/home/me/x"


def test_ps1_quoting_and_guards():
    text = render([ExportItem(NASTY, 123, False)], scan_time=1_700_000_000, fmt="ps1")
    assert (
        "Move-ToRecycleBin '/home/me/Downloads/it''s a \"weird\" name $(rm -rf ~) ünïcode.zip'"
        in text
    )
    assert "SendToRecycleBin" in text
    assert "changed since scan" in text
    assert "Remove-Item" not in text


def test_sh_quoting_and_guards():
    text = render([ExportItem(NASTY, 123, False)], scan_time=1_700_000_000, fmt="sh")
    assert "rm -rf ~)" in text  # present, but only inside a quoted string
    assert "move_to_trash '/home/me/Downloads/it'\"'\"'s a" in text
    assert "changed since scan" in text
    assert "\nrm " not in text


def test_unknown_format():
    with pytest.raises(ValueError):
        render([], scan_time=0, fmt="bat")


def test_ps1_written_with_bom(tmp_path):
    path = write_script([ExportItem(NASTY, 1, False)], scan_time=0, fmt="ps1", out_dir=tmp_path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
def test_sh_script_skips_changed_and_missing_files(tmp_path):
    # make sure we never actually call a real trash command here
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "trash-put"
    fake.write_text('#!/bin/sh\necho "$2" >> "$TRASH_LOG"\n')
    fake.chmod(0o755)

    keep_same = make_file(tmp_path / "data" / "old.bin", 1000, age_days=100)
    changed = make_file(tmp_path / "data" / "changed.bin", 1000, age_days=100)
    items = [
        ExportItem(str(keep_same), 1000, False),
        ExportItem(str(changed), 1000, False),
        ExportItem(str(tmp_path / "data" / "gone.bin"), 1000, False),
    ]
    script = write_script(items, scan_time=time.time() - 50 * 86400, fmt="sh", out_dir=tmp_path)
    changed.write_bytes(b"new stuff")  # touched after the scan

    # PATH with only the basics plus our fake, so a real gio/trash is never picked up
    for tool in ("find", "stat", "touch", "mktemp", "rm", "uname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    log = tmp_path / "trash.log"
    env = {**os.environ, "PATH": str(bin_dir), "TRASH_LOG": str(log)}
    out = subprocess.run(
        [shutil.which("bash"), str(script)], env=env, capture_output=True, text=True, check=True
    ).stdout

    assert log.read_text().splitlines() == [str(keep_same)]
    assert "skip (changed since scan)" in out
    assert "skip (no longer exists)" in out
