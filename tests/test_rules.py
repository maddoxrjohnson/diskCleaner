import time

from diskcleaner.rules import categorize, prior
from diskcleaner.scanner import Candidate

NOW = time.time()
DAY = 86400


def cand(path, is_dir=False, ext="", idle_days=400):
    ts = NOW - idle_days * DAY
    return Candidate(path=path, size=100, is_dir=is_dir, mtime=ts, atime=ts, ext=ext)


def test_categories():
    assert categorize(cand("/p/app/node_modules", is_dir=True)) == "package_cache"
    assert categorize(cand("/p/app/target", is_dir=True)) == "build_artifact"
    assert categorize(cand("/h/Downloads/setup.exe", ext=".exe")) == "installer"
    assert categorize(cand("/h/Downloads/ubuntu.iso", ext=".iso")) == "disk_image"
    assert categorize(cand("/h/stuff/thing.mkv", ext=".mkv")) == "media"
    assert categorize(cand("/h/AppData/Local/Temp/blob", ext="")) == "temp"
    assert categorize(cand("/h/x/report.pdf", ext=".pdf"), is_duplicate=True) == "duplicate"


def test_old_downloads_score_higher_than_fresh_ones():
    old = cand("/h/Downloads/setup.exe", ext=".exe", idle_days=400)
    fresh = cand("/h/Downloads/setup.exe", ext=".exe", idle_days=40)
    assert prior(old, "installer", now=NOW) > prior(fresh, "installer", now=NOW)


def test_documents_start_low():
    doc = cand("/h/work/thesis.docx", ext=".docx")
    assert prior(doc, "document", now=NOW) < 0.3


def test_folders_above_the_scan_root_dont_count():
    # scanned /tmp/abc/home, so "tmp" shouldn't make this look like temp junk
    c = cand("/tmp/abc/home/Downloads/setup.exe", ext=".exe")
    c.depth = 2
    assert categorize(c) == "installer"
