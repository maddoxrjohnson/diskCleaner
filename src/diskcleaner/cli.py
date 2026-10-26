from __future__ import annotations

import os
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from diskcleaner import analyze, safety, usage
from diskcleaner.export import ExportItem, human_size, write_script
from diskcleaner.model import PreferenceModel, TrainReport
from diskcleaner.scanner import MB, scan
from diskcleaner.store import Store, data_dir

app = typer.Typer(
    help="Find big files you probably don't need. Never deletes anything on its own.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def model_path() -> Path:
    return data_dir() / "model.joblib"


def _require_scan(store: Store) -> tuple[int, str, float]:
    latest = store.latest_scan()
    if latest is None:
        console.print("[yellow]No scans yet.[/] Run [bold]diskcleaner scan[/] first.")
        raise typer.Exit(1)
    return latest


def _retrain(store: Store) -> tuple[PreferenceModel, TrainReport]:
    model = PreferenceModel()
    X, y = analyze.label_matrix(store.labels())
    report = model.fit(X, y)
    model.save(model_path())
    return model, report


def _load_model(store: Store) -> PreferenceModel:
    model = PreferenceModel.load(model_path())
    # a model saved before the feature set changed gets thrown out on load,
    # so rebuild it from the saved labels rather than silently going rules-only
    if not model.trained and store.labels():
        model, _ = _retrain(store)
    return model


def _print_report(report: TrainReport) -> None:
    console.print(f"labels: {report.n_labels} ({report.n_delete} delete / {report.n_keep} keep)")
    if report.n_labels < 20 or min(report.n_delete, report.n_keep) == 0:
        console.print(
            "[dim]Not enough labels to train yet (need 20, with at least one of each). "
            "Using the built-in rules for now.[/]"
        )
        return
    if report.precision is not None:
        console.print(
            f"cross-validated precision on 'delete': [bold]{report.precision:.2f}[/]  "
            f"recall: {report.recall:.2f}"
        )
        console.print(
            "[dim]precision = when it says delete, how often you agreed. "
            "That's the number that matters here.[/]"
        )


def _export(store: Store, fmt: str | None, out_dir: Path) -> tuple[Path | None, int, int]:
    scan_id, _, scanned_at = _require_scan(store)
    labels = store.label_map()
    items = [
        ExportItem(sc.candidate.path, sc.candidate.size, sc.candidate.is_dir)
        for sc in store.candidates(scan_id)
        if labels.get(sc.candidate.path) == "delete"
    ]
    if not items:
        return None, 0, 0
    path = write_script(items, scan_time=scanned_at, fmt=fmt, out_dir=out_dir)
    return path, len(items), sum(i.size for i in items)


@app.command("scan")
def scan_cmd(
    path: Annotated[Path | None, typer.Argument(help="Folder to scan. Defaults to home.")] = None,
    min_size: Annotated[int, typer.Option(help="Smallest file to consider, in MB.")] = 50,
    min_age: Annotated[
        int, typer.Option(help="Ignore anything modified in the last N days.")
    ] = safety.DEFAULT_MIN_AGE_DAYS,
    top: Annotated[int, typer.Option(help="How many results to print.")] = 15,
) -> None:
    """Scan a folder and rank what looks safe to clean up."""
    root = (path or Path.home()).expanduser().resolve()
    if safety.is_protected(root):
        console.print(f"[red]{root} is a protected system location, not scanning it.[/]")
        raise typer.Exit(1)
    if not root.is_dir():
        console.print(f"[red]{root} is not a folder[/]")
        raise typer.Exit(1)

    started = time.time()
    with console.status(f"scanning {root} ...") as status:
        result = scan(
            root,
            min_file_size=min_size * MB,
            on_progress=lambda n, cur: status.update(f"scanned {n:,} folders  [dim]{cur}[/]"),
        )
        status.update("checking for duplicates ...")
        classified = analyze.classify(result, min_age_days=min_age)

    with Store() as store:
        store.save_scan(result, classified)
        model = _load_model(store)
        ranked = analyze.score([c for c in classified if c.rejected is None], model)

    shown = analyze.suggestions(ranked)
    rejected = sum(1 for c in classified if c.rejected)
    console.print(
        f"\nScanned {result.files_seen:,} files ({human_size(result.bytes_seen)}) "
        f"in {time.time() - started:.1f}s. "
        f"{len(classified)} big items, {rejected} ruled out by safety checks, "
        f"[bold]{len(shown)}[/] suggested ({human_size(sum(s.size for s in shown))})."
    )
    if result.errors:
        console.print(f"[dim]{result.errors} folders couldn't be read (permissions), skipped.[/]")

    table = Table(show_lines=False, header_style="bold")
    table.add_column("size", justify="right")
    table.add_column("score", justify="right")
    table.add_column("kind")
    table.add_column("path", overflow="fold")
    for s in shown[:top]:
        table.add_row(
            human_size(s.size), f"{s.score:.2f}", s.item.category.replace("_", " "), s.path
        )
    if shown:
        console.print(table)
        console.print("Run [bold]diskcleaner review[/] to go through them.")

    managed = sorted(
        (c for c in classified if c.candidate.extra.get("managed_by")),
        key=lambda c: -c.candidate.size,
    )
    if managed:
        console.print(
            "\n[bold]Also big, but not suggested[/] - these belong to another app, "
            "so remove them there instead of deleting files:"
        )
        for c in managed:
            console.print(
                f"  {human_size(c.candidate.size):>9}  {c.candidate.path}\n"
                f"             [dim]{c.candidate.extra['managed_by']}[/]"
            )


def _bar(fraction: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _signed_size(n: int) -> str:
    return ("+" if n >= 0 else "-") + human_size(abs(n))


def _print_spot(amount: Text, node: usage.Node) -> None:
    line = amount + Text(f"  {node.path}")  # Text, so [brackets] in paths aren't markup
    note = usage.note_for(node.name)
    if note:
        line.append(f"  - {note}", style="dim")
    console.print(line, soft_wrap=True)


@app.command()
def space(
    path: Annotated[Path | None, typer.Argument(help="Folder or drive. Defaults to home.")] = None,
    depth: Annotated[int, typer.Option(help="How many levels of folders to show.")] = 3,
    top: Annotated[int, typer.Option(help="Biggest N entries to show per folder.")] = 8,
    since: Annotated[int, typer.Option(help="What counts as recent, in days.")] = 7,
) -> None:
    """Show what's taking up space and what grew. Only measures, never suggests."""
    root = (path or Path.home()).expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]{root} is not a folder[/]")
        raise typer.Exit(1)

    with console.status(f"measuring {root} ...") as status:
        result = usage.measure(
            root,
            since_days=since,
            on_progress=lambda n, cur: status.update(f"read {n:,} folders  [dim]{cur}[/]"),
        )
    tree_root = result.root

    with Store() as store:
        previous = store.last_usage(tree_root.path)
        store.save_usage(
            tree_root.path,
            usage.snapshot_rows(tree_root),
            usage.KEEP_DEPTH + 1,  # +1 for big files below the deepest folder
        )

    def grew(n: usage.Node) -> int | None:
        return usage.growth(n, previous[2], previous[1]) if previous else None

    # anything smaller than this isn't worth a line
    floor = max(tree_root.size // 200, 10 * MB)

    def label(n: usage.Node, parent_size: int) -> Text:
        frac = n.size / parent_size if parent_size else 0
        line = Text(f"{human_size(n.size):>9}  ")
        line.append(_bar(frac), style="cyan")
        line.append(f" {frac:4.0%}  ")
        line.append(n.name + ("" if n.is_dir else "  (file)"), style="bold" if n.is_dir else "")
        g = grew(n)
        if g is not None and abs(g) >= 100 * MB:
            line.append(f"  {_signed_size(g)}", style="red" if g > 0 else "green")
        elif previous is None and n.recent >= 100 * MB:
            line.append(f"  {human_size(n.recent)} recent", style="yellow")
        note = usage.note_for(n.name)
        if note:
            line.append(f"  - {note}", style="dim")
        return line

    def add(branch: Tree, node: usage.Node, level: int) -> None:
        kids = sorted(node.children, key=lambda c: -c.size)
        shown = [c for c in kids if c.size >= floor][:top]
        for c in shown:
            sub = branch.add(label(c, node.size))
            # a folder that's nearly all one child (Users > you) doesn't use up a level
            nxt = level if c.size >= 0.9 * node.size else level + 1
            if nxt <= depth and c.children:
                add(sub, c, nxt)
        rest = node.size - sum(c.size for c in shown)
        if shown and rest >= floor:
            branch.add(Text(f"{human_size(rest):>9}  everything else", style="dim"))

    heading = Text(f"{tree_root.path}  {human_size(tree_root.size)}", style="bold")
    if os.path.ismount(tree_root.path):
        du = shutil.disk_usage(tree_root.path)
        heading.append(
            f"   (drive: {human_size(du.used)} used of {human_size(du.total)}, "
            f"{human_size(du.free)} free)",
            style="",
        )
    tree = Tree(heading)
    add(tree, tree_root, 1)
    console.print(tree)

    console.print(
        f"[dim]{result.files:,} files in {result.seconds:.1f}s"
        + (f", {result.errors:,} things couldn't be read" if result.errors else "")
        + "[/]"
    )
    if os.path.ismount(tree_root.path):
        hidden = du.used - tree_root.size
        if hidden >= 1024 * MB:
            console.print(
                f"[dim]{human_size(hidden)} of the drive's used space isn't in any folder we "
                "could read (restore points, other accounts, system files). Run as "
                "administrator to see more of it.[/]"
            )

    threshold = max(100 * MB, tree_root.size // 500)
    if previous is not None:
        when = time.strftime("%b %d %H:%M", time.localtime(previous[0]))
        up = usage.gross(tree_root, grew)
        down = usage.gross(tree_root, lambda n: None if grew(n) is None else -grew(n))
        console.print(
            f"\n[bold]Since the last check ({when}):[/] "
            f"{human_size(up.get(id(tree_root), 0))} added, "
            f"{human_size(down.get(id(tree_root), 0))} removed"
        )
        spots = usage.hotspots(tree_root, lambda n: up.get(id(n)), threshold=threshold)
        if not spots:
            console.print("  nothing grew by more than " + human_size(threshold))
        for n, g in spots:
            _print_spot(Text(f"  {_signed_size(g):>10}", style="red"), n)
        freed = usage.hotspots(tree_root, lambda n: down.get(id(n)), threshold=threshold, limit=3)
        if freed:
            console.print("[dim]  biggest drops:[/]")
            for n, g in freed:
                _print_spot(Text(f"  {_signed_size(-g):>10}", style="green"), n)
    else:
        console.print(f"\n[bold]Written to in the last {since} days:[/]")
        spots = usage.hotspots(tree_root, lambda n: n.recent, threshold=threshold)
        if not spots:
            console.print("  nothing big")
        for n, r in spots:
            _print_spot(Text(f"  {human_size(r):>10}", style="yellow"), n)
        console.print(
            "[dim]That includes files that were only updated, not new. "
            "From the next run on this shows exactly what grew since now.[/]"
        )


@app.command()
def review(
    threshold: Annotated[
        float, typer.Option(help="Only show items scored at least this high (0-1).")
    ] = analyze.DEFAULT_THRESHOLD,
    show_all: Annotated[
        bool, typer.Option("--all", help="Show everything, ignore the threshold.")
    ] = False,
) -> None:
    """Go through suggestions and mark what to keep or clean up."""
    from diskcleaner.tui import ReviewApp  # textual is slow to import

    with Store() as store:
        scan_id, root, _ = _require_scan(store)
        model = _load_model(store)
        ranked = analyze.score(store.candidates(scan_id), model)
        items = ranked if show_all else analyze.suggestions(ranked, threshold)
        if not items:
            console.print("Nothing above the threshold. Try [bold]--all[/] or a lower --threshold.")
            return

        def on_label(s: analyze.Suggestion, label: str | None) -> None:
            if label is None:
                store.clear_label(s.path)
            else:
                store.set_label(s.item, label)

        def on_export() -> str:
            path, n, size = _export(store, None, Path.cwd())
            if path is None:
                return "Nothing marked for cleanup yet."
            return f"Wrote {path.name}: {n} items, {human_size(size)}. Review it before running!"

        tui = ReviewApp(items, store.label_map(), on_label, on_export)
        tui.run()

        if tui.changed:
            _, report = _retrain(store)
            console.print("[green]Saved your choices.[/]")
            _print_report(report)


@app.command()
def train() -> None:
    """Retrain the preference model from your labels."""
    with Store() as store:
        _, report = _retrain(store)
    _print_report(report)


@app.command("export")
def export_cmd(
    fmt: Annotated[
        str | None, typer.Option("--format", help="ps1 or sh. Defaults to your OS.")
    ] = None,
    out: Annotated[Path, typer.Option(help="Folder to write the script to.")] = Path("."),
) -> None:
    """Write a cleanup script for everything you marked. You run it, not us."""
    if fmt not in (None, "ps1", "sh"):
        console.print("[red]--format must be ps1 or sh[/]")
        raise typer.Exit(1)
    with Store() as store:
        path, n, size = _export(store, fmt, out)
    if path is None:
        console.print("Nothing is marked for cleanup. Use [bold]diskcleaner review[/] first.")
        return
    console.print(f"Wrote [bold]{path}[/] ({n} items, {human_size(size)}).")
    console.print("Read through it, remove anything you're not sure about, then run it yourself.")


@app.command()
def skipped(
    top: Annotated[int, typer.Option(help="How many to print.")] = 40,
    reason: Annotated[
        str | None, typer.Option(help="Only show reasons containing this, e.g. 'days'.")
    ] = None,
) -> None:
    """Big items from the last scan that the safety rules kept out, and why."""
    with Store() as store:
        scan_id, _, _ = _require_scan(store)
        items = [
            sc for sc in store.candidates(scan_id, include_rejected=True) if sc.rejected is not None
        ]
    if reason:
        items = [sc for sc in items if reason.lower() in sc.rejected.lower()]
    if not items:
        console.print("Nothing was ruled out.")
        return
    items.sort(key=lambda sc: -sc.candidate.size)

    by_reason: Counter[str] = Counter()
    for sc in items:
        # "modified in the last 30 days" etc. would otherwise split on the number
        by_reason[sc.rejected.split(":")[0]] += sc.candidate.size
    for why, size in by_reason.most_common():
        console.print(f"  {human_size(size):>9}  {why}")

    table = Table(header_style="bold")
    table.add_column("size", justify="right")
    table.add_column("why it's not suggested")
    table.add_column("path", overflow="fold")
    for sc in items[:top]:
        table.add_row(human_size(sc.candidate.size), sc.rejected, sc.candidate.path)
    console.print(table)
    if len(items) > top:
        console.print(f"[dim]{len(items) - top} more, use --top to see them[/]")
    console.print(
        "[dim]These are never suggested. Most are recent (they'll show up once they're "
        "older) or belong to the system or another app.[/]"
    )


@app.command()
def stats() -> None:
    """What you've labeled so far and what the latest scan found."""
    with Store() as store:
        counts = store.label_counts()
        console.print(
            f"labels: {counts.get('delete', 0)} delete, {counts.get('keep', 0)} keep, "
            f"{counts.get('skip', 0)} skip"
        )
        latest = store.latest_scan()
        if latest is None:
            return
        scan_id, root, scanned_at = latest
        items = store.candidates(scan_id)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(scanned_at))
        console.print(f"latest scan: {root} ({when}), {len(items)} candidates")

        by_cat: Counter[str] = Counter()
        for sc in items:
            by_cat[sc.category] += sc.candidate.size
        table = Table(header_style="bold")
        table.add_column("kind")
        table.add_column("size", justify="right")
        for cat, size in by_cat.most_common():
            table.add_row(cat.replace("_", " "), human_size(size))
        console.print(table)

    m = PreferenceModel.load(model_path())
    if m.trained:
        console.print(f"model: trained on {m.n_labels} labels")
    else:
        console.print("model: not trained yet, using built-in rules")


@app.command()
def forget(
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete all saved scans, labels and the model."""
    folder = data_dir()
    if not yes and not typer.confirm(f"Delete everything in {folder}?"):
        raise typer.Exit()
    shutil.rmtree(folder, ignore_errors=True)
    console.print("Done, all diskcleaner data removed.")
