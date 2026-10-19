"""Interactive review screen (Textual)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from diskcleaner.analyze import Suggestion
from diskcleaner.export import human_size, to_windows_path

MARKS = {
    "delete": Text("DEL", style="bold red"),
    "keep": Text("KEEP", style="bold green"),
    "skip": Text("skip", style="dim"),
    None: Text(""),
}


def copy_to_system_clipboard(text: str) -> bool:
    """Native clipboard first, since not every terminal supports OSC 52."""
    if sys.platform == "win32":
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    elif "microsoft" in os.uname().release.lower():
        cmd = ["clip.exe"]
    else:
        cmd = ["wl-copy"] if os.environ.get("WAYLAND_DISPLAY") else ["xclip", "-sel", "clip"]
    try:
        # clip.exe reads the console code page, utf-16 keeps non-ascii paths intact
        data = text.encode("utf-16-le") if cmd[0] in ("clip", "clip.exe") else text.encode()
        subprocess.run(cmd, input=data, check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def reveal_in_file_manager(path: str) -> None:
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif "microsoft" in os.uname().release.lower():
            subprocess.Popen(["explorer.exe", "/select,", to_windows_path(path)])
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except OSError:
        pass


class ReviewApp(App):
    CSS = """
    #main { height: 1fr; }
    DataTable { width: 2fr; }
    #details { width: 1fr; padding: 1 2; border-left: solid $primary; }
    #status { height: 1; padding: 0 1; background: $boost; }
    """

    BINDINGS = [
        Binding("d", "label('delete')", "Delete"),
        Binding("k", "label('keep')", "Keep"),
        Binding("s", "label('skip')", "Skip"),
        Binding("u", "label(None)", "Undo"),
        Binding("space", "toggle_group", "Expand"),
        Binding("o", "reveal", "Open"),
        Binding("c", "copy_path", "Copy path"),
        Binding("e", "export", "Export"),
        Binding("q", "quit", "Save & quit"),
    ]

    def __init__(
        self,
        suggestions: list[Suggestion],
        labels: dict[str, str],
        on_label: Callable[[Suggestion, str | None], None],
        on_export: Callable[[], str],
    ) -> None:
        super().__init__()
        self.suggestions = suggestions
        self.labels = dict(labels)
        self.on_label = on_label
        self.on_export = on_export
        self.changed = 0

        groups: dict[str, list[Suggestion]] = defaultdict(list)
        for s in suggestions:
            groups[s.group_key].append(s)
        # only worth a group row if there's more than one thing in it
        self.groups = {k: v for k, v in groups.items() if len(v) > 1}

        # top-level rows, biggest expected payoff first
        def payoff(items: list[Suggestion]) -> float:
            return sum(s.size * s.score for s in items)

        self.entries: list[tuple[str, object]] = [("group", k) for k in self.groups]
        self.entries += [("item", s) for s in suggestions if s.group_key not in self.groups]
        self.entries.sort(
            key=lambda e: payoff(self.groups[e[1]] if e[0] == "group" else [e[1]]),
            reverse=True,
        )
        self.expanded: set[str] = set()
        self.rows: list[tuple[str, object]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main"):
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield Static(id="details")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "diskcleaner"
        self.sub_title = "nothing is deleted from here - you'll get a script to review"
        table = self.query_one(DataTable)
        table.add_columns("", "size", "score", "kind", "path")
        self.rebuild()

    def _group_mark(self, items: list[Suggestion]) -> Text:
        marks = {self.labels.get(s.path) for s in items}
        if len(marks) == 1:
            return MARKS[marks.pop()]
        return Text("mixed", style="yellow")

    def rebuild(self) -> None:
        table = self.query_one(DataTable)
        keep_row = table.cursor_row
        table.clear()
        self.rows = []

        for kind, value in self.entries:
            if kind == "group":
                key = value
                items = self.groups[key]
                arrow = "v" if key in self.expanded else ">"
                total = sum(i.size for i in items)
                avg = sum(i.score for i in items) / len(items)
                table.add_row(
                    self._group_mark(items),
                    human_size(total),
                    f"{avg:.2f}",
                    items[0].item.category.replace("_", " "),
                    Text(f"{arrow} {len(items)} x {key}", style="bold"),
                )
                self.rows.append(("group", key))
                if key in self.expanded:
                    for i in items:
                        self._add_item_row(table, i, indent=True)
            else:
                self._add_item_row(table, value)

        if self.rows:
            table.move_cursor(row=min(keep_row, len(self.rows) - 1))
        self.update_status()

    def _add_item_row(self, table: DataTable, s: Suggestion, indent: bool = False) -> None:
        path = ("    " if indent else "") + s.path
        table.add_row(
            MARKS[self.labels.get(s.path)],
            human_size(s.size),
            f"{s.score:.2f}",
            s.item.category.replace("_", " "),
            path,
        )
        self.rows.append(("item", s))

    def current(self) -> list[Suggestion]:
        if not self.rows:
            return []
        kind, value = self.rows[self.query_one(DataTable).cursor_row]
        return self.groups[value] if kind == "group" else [value]

    def on_data_table_row_highlighted(self, _event) -> None:
        self.show_details()

    def show_details(self) -> None:
        panel = self.query_one("#details", Static)
        items = self.current()
        if not items:
            panel.update("nothing to review")
            return
        if len(items) > 1:
            total = sum(i.size for i in items)
            body = Text.assemble(
                (f"{len(items)} items, {human_size(total)}\n\n", "bold"),
                "Labels apply to the whole group.\nPress space to expand it.\n",
            )
            panel.update(body)
            return

        s = items[0]
        c = s.item.candidate
        modified = int((time.time() - c.mtime) / 86400)
        body = Text()
        body.append(f"{c.path}\n\n", style="bold")
        body.append(f"size        {human_size(c.size)}\n")
        if c.is_dir:
            body.append(f"files       {c.file_count:,}\n")
        body.append(f"modified    {modified} days ago\n")
        body.append(f"score       {s.score:.2f}")
        if s.model_p is not None:
            body.append(f"  (model {s.model_p:.2f}, rules {s.item.prior:.2f})")
        body.append("\n\n")
        if s.reasons:
            body.append("why it's here\n", style="bold red")
            for r in s.reasons:
                body.append(f"  + {r}\n")
        if s.keep_reasons:
            body.append("\nwhy you might keep it\n", style="bold green")
            for r in s.keep_reasons:
                body.append(f"  - {r}\n")
        panel.update(body)

    def update_status(self) -> None:
        marked = [s for s in self.suggestions if self.labels.get(s.path) == "delete"]
        total = sum(s.size for s in marked)
        reviewed = sum(1 for s in self.suggestions if s.path in self.labels)
        self.query_one("#status", Static).update(
            f" marked for cleanup: {len(marked)} items, {human_size(total)}"
            f"   |   reviewed {reviewed}/{len(self.suggestions)}"
        )
        self.show_details()

    def action_label(self, label: str | None) -> None:
        items = self.current()
        for s in items:
            if label is None:
                self.labels.pop(s.path, None)
            else:
                self.labels[s.path] = label
            self.on_label(s, label)
            self.changed += 1
        table = self.query_one(DataTable)
        row = table.cursor_row
        self.rebuild()
        if label is not None and row + 1 < len(self.rows):
            table.move_cursor(row=row + 1)

    def action_toggle_group(self) -> None:
        if not self.rows:
            return
        kind, value = self.rows[self.query_one(DataTable).cursor_row]
        if kind != "group":
            return
        self.expanded ^= {value}
        self.rebuild()

    def action_copy_path(self) -> None:
        items = self.current()
        if not items:
            return
        text = "\n".join(s.path for s in items)
        if not copy_to_system_clipboard(text):
            self.copy_to_clipboard(text)  # OSC 52 fallback, works in most modern terminals
        what = "path" if len(items) == 1 else f"{len(items)} paths"
        self.notify(f"Copied {what} to the clipboard")

    def action_reveal(self) -> None:
        items = self.current()
        if len(items) == 1:
            reveal_in_file_manager(items[0].path)

    def action_export(self) -> None:
        msg = self.on_export()
        self.notify(msg, timeout=8)
