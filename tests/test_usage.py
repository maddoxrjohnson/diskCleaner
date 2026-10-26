import os

from conftest import make_file
from typer.testing import CliRunner

from diskcleaner import usage
from diskcleaner.cli import app
from diskcleaner.scanner import MB

runner = CliRunner()


def find(root: usage.Node, name: str) -> usage.Node:
    return next(n for n in root.walk() if n.name == name)


def test_sizes_add_up(tmp_path):
    make_file(tmp_path / "a" / "one.bin", 3 * MB)
    make_file(tmp_path / "a" / "deep" / "two.bin", 2 * MB, age_days=1)
    make_file(tmp_path / "b" / "three.bin", 1 * MB)

    tree = usage.measure(tmp_path).root

    assert tree.size == 6 * MB
    assert find(tree, "a").size == 5 * MB
    assert find(tree, "a").recent == 2 * MB
    assert find(tree, "b").recent == 0


def test_big_files_get_their_own_row(tmp_path):
    make_file(tmp_path / "pagefile.sys", 200 * MB)
    make_file(tmp_path / "small.txt", 1 * MB)

    tree = usage.measure(tmp_path).root

    assert [c.name for c in tree.children] == ["pagefile.sys"]
    assert tree.size == 201 * MB


def test_folders_below_keep_depth_count_towards_ancestor(tmp_path):
    deep = tmp_path.joinpath(*[f"d{i}" for i in range(6)])
    make_file(deep / "x.bin", 4 * MB)

    tree = usage.measure(tmp_path, keep_depth=2).root

    assert max(n.depth for n in tree.walk()) == 2
    assert find(tree, "d1").size == 4 * MB


def test_symlinks_not_followed(tmp_path):
    make_file(tmp_path / "real" / "x.bin", 4 * MB)
    try:
        os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # no symlink rights on this Windows box
    assert usage.measure(tmp_path).root.size == 4 * MB


def test_hotspots_narrow_down_to_the_folder_that_grew(tmp_path):
    pkgs = tmp_path / "Users" / "me" / "code" / "app" / "node_modules"
    make_file(pkgs / "huge-package" / "blob.bin", 300 * MB, age_days=1)
    make_file(tmp_path / "Users" / "me" / "Documents" / "old.pdf", 300 * MB)

    tree = usage.measure(tmp_path).root
    spots = usage.hotspots(tree, lambda n: n.recent, threshold=100 * MB)

    # stops at node_modules since it's a known name, rather than at the blob
    assert [(n.name, v) for n, v in spots] == [("node_modules", 300 * MB)]


def test_hotspots_report_spread_out_growth_at_the_parent(tmp_path):
    for i in range(10):
        make_file(tmp_path / "logs" / f"day{i}" / "log.txt", 20 * MB, age_days=1)

    tree = usage.measure(tmp_path).root
    spots = usage.hotspots(tree, lambda n: n.recent, threshold=100 * MB)

    assert [n.name for n, _ in spots] == ["logs"]


def test_space_command_shows_growth_on_second_run(tmp_path):
    root = tmp_path / "home"
    make_file(root / "projects" / "a.bin", 50 * MB)

    first = runner.invoke(app, ["space", str(root)])
    assert first.exit_code == 0, first.output
    assert "projects" in first.output
    assert "Written to in the last 7 days" in first.output

    make_file(root / "Downloads" / "big.iso", 400 * MB)
    second = runner.invoke(app, ["space", str(root)])

    assert second.exit_code == 0, second.output
    assert "Since the last check" in second.output
    assert "+400.0 MB" in second.output
    assert "big.iso" in second.output


def test_hotspots_look_inside_mixed_folders(tmp_path):
    make_file(tmp_path / "Temp" / "DiagOutputDir" / "trace.etl", 300 * MB, age_days=1)
    make_file(tmp_path / "Temp" / "other.txt", 1 * MB, age_days=1)

    tree = usage.measure(tmp_path).root
    spots = usage.hotspots(tree, lambda n: n.recent, threshold=100 * MB)

    assert [n.name for n, _ in spots] == ["trace.etl"]


def test_growth_not_hidden_by_a_deletion_next_to_it(tmp_path):
    local = tmp_path / "AppData" / "Local"
    big = make_file(local / "Temp" / "traces.etl", 500 * MB)
    tree = usage.measure(tmp_path).root
    previous = dict(usage.snapshot_rows(tree))

    big.unlink()
    make_file(local / "SomeApp" / "vm.vhdx", 300 * MB)
    tree = usage.measure(tmp_path).root

    def grew(n):
        return usage.growth(n, previous, usage.KEEP_DEPTH + 1)

    assert grew(find(tree, "Local")) == -200 * MB  # net, which hides the new 300 MB
    up = usage.gross(tree, grew)
    spots = usage.hotspots(tree, lambda n: up.get(id(n)), threshold=100 * MB)
    assert [(n.name, v) for n, v in spots] == [("vm.vhdx", 300 * MB)]
