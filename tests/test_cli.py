import os

from conftest import make_file
from typer.testing import CliRunner

from diskcleaner.cli import app
from diskcleaner.scanner import MB
from diskcleaner.store import Store

runner = CliRunner()


def test_scan_label_train_export(tmp_path):
    root = tmp_path / "home"
    make_file(root / "Downloads" / "setup.exe", 80 * MB, age_days=300)
    make_file(root / "Downloads" / "fresh.zip", 80 * MB, age_days=2)
    for i in range(20):
        make_file(root / "code" / "app" / "node_modules" / f"p{i}" / "x.js", 1 * MB, age_days=200)

    result = runner.invoke(app, ["scan", str(root)])
    assert result.exit_code == 0, result.output
    assert "installer" in result.output
    assert "1 ruled out by safety" in result.output

    with Store() as store:
        scan_id, _, _ = store.latest_scan()
        items = store.candidates(scan_id)
        assert {os.path.basename(i.candidate.path) for i in items} == {"setup.exe", "node_modules"}
        for item in items:
            store.set_label(item, "delete")

    result = runner.invoke(app, ["train"])
    assert result.exit_code == 0
    assert "Not enough labels" in result.output

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = runner.invoke(app, ["export", "--format", "sh", "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    script = next(out_dir.glob("cleanup-*.sh")).read_text()
    assert "setup.exe" in script
    assert "node_modules" in script
    assert "fresh.zip" not in script


def test_refuses_protected_root():
    result = runner.invoke(app, ["scan", "/etc"])
    assert result.exit_code == 1
    assert "protected" in result.output


def test_export_without_scan():
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 1
    assert "No scans yet" in result.output


def test_scan_lists_vms_separately(tmp_path):
    root = tmp_path / "home"
    make_file(root / ".android" / "avd" / "Pixel_6.avd" / "userdata.img", 80 * MB, age_days=300)
    make_file(root / "Downloads" / "setup.exe", 80 * MB, age_days=300)

    result = runner.invoke(app, ["scan", str(root)])

    assert result.exit_code == 0, result.output
    assert "Also big, but not suggested" in result.output
    assert "Device Manager" in result.output
    with Store() as store:
        scan_id, _, _ = store.latest_scan()
        suggestable = [os.path.basename(i.candidate.path) for i in store.candidates(scan_id)]
    assert suggestable == ["setup.exe"]
