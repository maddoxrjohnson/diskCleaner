"""Writes a cleanup script for the user to read and run themselves.

The tool itself never deletes anything. The script it writes:
  * moves things to the Recycle Bin / Trash instead of deleting them
  * skips anything that's gone, or was modified since the scan
  * refuses to fall back to `rm` if no trash command is available
"""

from __future__ import annotations

import re
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ExportItem:
    path: str
    size: int
    is_dir: bool


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def default_format() -> str:
    return "ps1" if sys.platform == "win32" else "sh"


_wsl_mount = re.compile(r"^/mnt/([a-zA-Z])(/|$)")


def to_windows_path(path: str) -> str:
    """/mnt/c/Users/x -> C:\\Users\\x, so a WSL scan can export a PowerShell script."""
    m = _wsl_mount.match(path)
    if not m:
        return path
    rest = path[m.end() :].replace("/", "\\")
    return f"{m.group(1).upper()}:\\{rest}"


def ps_quote(s: str) -> str:
    # single-quoted PowerShell strings are literal; only ' needs escaping
    return "'" + s.replace("'", "''") + "'"


PS_HEADER = r"""
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName Microsoft.VisualBasic
$ScanTime = [DateTimeOffset]::FromUnixTimeSeconds({scan_ts}).UtcDateTime
$Freed = 0
$Failed = 0

function Move-ToRecycleBin([string]$Path, [long]$Size, [bool]$IsDir) {{
    if (-not (Test-Path -LiteralPath $Path)) {{
        Write-Host "skip (no longer exists): $Path"; return
    }}
    $item = Get-Item -LiteralPath $Path -Force
    if ($IsDir) {{
        $newer = Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object {{ $_.LastWriteTimeUtc -gt $ScanTime }} | Select-Object -First 1
        if ($newer) {{ Write-Host "skip (changed since scan): $Path"; return }}
    }} elseif ($item.Length -ne $Size -or $item.LastWriteTimeUtc -gt $ScanTime) {{
        Write-Host "skip (changed since scan): $Path"; return
    }}

    try {{
        if ($IsDir) {{
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                $Path, 'OnlyErrorDialogs', 'SendToRecycleBin')
        }} else {{
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
                $Path, 'OnlyErrorDialogs', 'SendToRecycleBin')
        }}
    }} catch {{
        # the message Windows gives here is often useless ("not supported on
        # this system" really means access denied), so add the likely cause
        $script:Failed++
        Write-Host "FAILED: $Path" -ForegroundColor Red
        $err = $_.Exception
        if ($err.InnerException) {{ $err = $err.InnerException }}
        Write-Host "  $($err.Message)"
        Write-Host "  usually a file in there is open in another program, or needs admin rights"
        return
    }}
    # don't take the call's word for it
    if (Test-Path -LiteralPath $Path) {{
        $script:Failed++
        Write-Host "FAILED (still there, maybe partly recycled): $Path" -ForegroundColor Red
        return
    }}
    $script:Freed += $Size
    Write-Host "recycled: $Path"
}}
"""

SH_HEADER = r"""
set -u
REF="$(mktemp)"
trap 'rm -f "$REF"' EXIT
touch -t {scan_stamp} "$REF"
FREED=0
FAILED=0

to_trash() {{
    if command -v gio >/dev/null 2>&1; then gio trash -- "$1"
    elif command -v trash-put >/dev/null 2>&1; then trash-put -- "$1"
    elif command -v trash >/dev/null 2>&1; then trash "$1"
    elif [ "$(uname)" = "Darwin" ]; then
        osascript -e 'on run argv' \
            -e 'tell application "Finder" to delete POSIX file (item 1 of argv)' \
            -e 'end run' "$1" >/dev/null
    else
        echo "no trash command found (install trash-cli), not deleting: $1" >&2
        return 1
    fi
}}

file_size() {{ stat -c %s -- "$1" 2>/dev/null || stat -f %z -- "$1"; }}

move_to_trash() {{
    local path="$1" size="$2" is_dir="$3"
    if [ ! -e "$path" ]; then echo "skip (no longer exists): $path"; return; fi
    if [ -n "$(find "$path" -newer "$REF" -print -quit 2>/dev/null)" ]; then
        echo "skip (changed since scan): $path"; return
    fi
    if [ "$is_dir" = 0 ] && [ "$(file_size "$path")" != "$size" ]; then
        echo "skip (changed since scan): $path"; return
    fi
    if to_trash "$path" && [ ! -e "$path" ]; then
        FREED=$((FREED + size))
        echo "trashed: $path"
    else
        FAILED=$((FAILED + 1))
        echo "FAILED: $path" >&2
    fi
}}
"""


def render(items: list[ExportItem], *, scan_time: float, fmt: str) -> str:
    total = sum(i.size for i in items)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    intro = [
        f"diskcleaner cleanup script - generated {stamp}",
        f"{len(items)} items, {human_size(total)} total",
        "",
        "READ THIS BEFORE RUNNING IT. Nothing here is permanently deleted:",
        "everything goes to the Recycle Bin / Trash, and anything that changed",
        "since the scan is skipped. Delete or comment out any line you're unsure about.",
    ]

    if fmt == "ps1":
        lines = [f"# {x}".rstrip() for x in intro]
        lines.append(PS_HEADER.format(scan_ts=int(scan_time)))
        for i in sorted(items, key=lambda i: -i.size):
            lines.append(f"# {human_size(i.size)}")
            lines.append(
                f"Move-ToRecycleBin {ps_quote(to_windows_path(i.path))} {i.size} "
                f"${str(i.is_dir).lower()}"
            )
        lines.append("")
        lines.append(
            'Write-Host ("done, about {0:N1} GB moved to the Recycle Bin" -f ($Freed / 1GB))'
        )
        lines.append(
            'if ($Failed) { Write-Host "$Failed item(s) failed, see FAILED above" '
            "-ForegroundColor Yellow }"
        )
    elif fmt == "sh":
        lines = ["#!/usr/bin/env bash"] + [f"# {x}".rstrip() for x in intro]
        scan_stamp = time.strftime("%Y%m%d%H%M.%S", time.localtime(scan_time))
        lines.append(SH_HEADER.format(scan_stamp=scan_stamp))
        for i in sorted(items, key=lambda i: -i.size):
            lines.append(f"# {human_size(i.size)}")
            lines.append(f"move_to_trash {shlex.quote(i.path)} {i.size} {int(i.is_dir)}")
        lines.append("")
        lines.append('echo "done, about $((FREED / 1024 / 1024)) MB moved to the trash"')
        lines.append(
            'if [ "$FAILED" -gt 0 ]; then echo "$FAILED item(s) failed, see FAILED above" >&2; fi'
        )
    else:
        raise ValueError(f"unknown format: {fmt}")

    return "\n".join(lines) + "\n"


def write_script(
    items: list[ExportItem],
    *,
    scan_time: float,
    fmt: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    fmt = fmt or default_format()
    out_dir = out_dir or Path.cwd()
    name = f"cleanup-{datetime.now():%Y%m%d-%H%M%S}.{fmt}"
    path = out_dir / name
    text = render(items, scan_time=scan_time, fmt=fmt)
    # Windows PowerShell 5 needs the BOM to read non-ASCII paths correctly
    encoding = "utf-8-sig" if fmt == "ps1" else "utf-8"
    with open(path, "w", encoding=encoding, newline="\r\n" if fmt == "ps1" else "\n") as f:
        f.write(text)
    if fmt == "sh":
        path.chmod(0o755)
    return path
