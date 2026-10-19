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


def test_installer_vs_program():
    def exe(path, depth=2, **extra):
        c = cand(path, ext=".exe")
        c.depth = depth
        c.extra.update(extra)
        return categorize(c)

    # real installers, named or located like downloads
    assert exe("/h/Desktop/msys2-x86_64-20250830.exe", depth=1) == "installer"
    assert exe("/h/Downloads/DiscordSetup.exe", depth=1) == "installer"
    assert exe("/h/Downloads/somegame.exe", depth=1) == "installer"
    assert exe("/h/stuff/node-v22.15.0-x64.exe") == "installer"
    # game / app executables are not
    assert exe("/h/Games/Balatro/Balatro.exe") == "program"
    assert exe("/h/Games/Bloons/btdb2_game.exe", next_to_libraries=True) == "program"
    # an exe next to DLLs is installed, even if the name looks setup-ish
    assert exe("/h/Tools/App/updater-x64.exe", next_to_libraries=True) == "program"
    # package formats are always installers
    msi = cand("/h/stuff/tool.msi", ext=".msi")
    msi.depth = 2
    assert categorize(msi) == "installer"
