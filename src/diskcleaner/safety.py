"""Hard safety rules.

Everything in here is deliberately dumb and strict. The model is allowed to
reorder or hide suggestions, but it can never bring back something these
rules have rejected.
"""

from __future__ import annotations

import time
from pathlib import PurePath

# Top-level folders on a drive / root filesystem we never even walk into.
# Compared case-insensitively against the first path component after the root.
PROTECTED_TOP_LEVEL = {
    # windows
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "boot",
    "system repair",
    "perflogs",
    "inetpub",
    "config.msi",
    "msocache",
    # leftovers from Windows upgrades - use Disk Cleanup for these, the files
    # are owned by TrustedInstaller and removing them by hand goes badly
    "windows.old",
    "$windows.~bt",
    "$windows.~ws",
    "$winreagent",
    "$sysreset",
    "$getcurrent",
    # OEM recovery / driver folders (Dell, HP, Lenovo, ...)
    "apps",
    "drivers",
    "dell",
    "swsetup",
    "oem",
    "intel",
    "amd",
    "nvidia",
    # unix-ish
    "bin",
    "sbin",
    "lib",
    "lib32",
    "lib64",
    "usr",
    "etc",
    "var",
    "opt",
    "proc",
    "sys",
    "dev",
    "run",
    "snap",
    "srv",
    "root",
    # macOS
    "system",
    "library",
    "applications",
    "private",
    "volumes",
    "cores",
}

# Built-in profiles under C:\Users that aren't a real person's stuff
SYSTEM_PROFILES = {"default", "default user", "all users", "defaultapppool"}

# If any component of a path matches one of these, hands off.
NEVER_TOUCH_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    "keychains",
    # cloud-synced folders: deleting here deletes it everywhere
    "onedrive",
    "dropbox",
    "icloud drive",
    "google drive",
    "mobile documents",
    # game libraries belong to their launcher - uninstall through it instead,
    # otherwise e.g. Steam still thinks the game is installed
    "steamapps",
    "epic games",
    "gog galaxy",
    "gog games",
    "riot games",
    "ea games",
    "ubisoft game launcher",
    "xboxgames",
    "battle.net",
}

# App config lives in these. We only allow suggestions inside them when the
# path also goes through something that is obviously a cache/temp/log folder.
CONFIG_HOMES = {"appdata", ".config", ".local"}
MAC_LIBRARY = "library"
DISPOSABLE_PARTS = {"temp", "tmp", "cache", "caches", ".cache", "logs", "crashdumps"}

# "Personal stuff" folders. Files in here are only suggested if they're
# exact duplicates of something else.
PERSONAL_DIRS = {"documents", "pictures", "photos", "music", "videos", "movies"}

DEFAULT_MIN_AGE_DAYS = 30


def _parts(path: str | PurePath) -> list[str]:
    p = PurePath(path)
    parts = [x.lower() for x in p.parts]
    # drop the root/anchor ("/" or "C:\\")
    if parts and (parts[0] == p.anchor.lower() or parts[0] in ("/", "\\")):
        parts = parts[1:]
    # WSL mounts windows drives at /mnt/<letter>, treat that like a drive root
    if len(parts) >= 2 and parts[0] == "mnt" and len(parts[1]) == 1:
        parts = parts[2:]
    # on macOS /etc, /var and /tmp are really /private/etc etc.
    if len(parts) >= 2 and parts[0] == "private":
        parts = parts[1:]
    return parts


def is_protected(path: str | PurePath) -> bool:
    """True if we shouldn't scan into or suggest anything at this path."""
    parts = _parts(path)
    if not parts:
        # the root of a drive itself is fine to scan, just not to suggest
        return False
    # macOS per-user temp folders live under /var, but they're fair game
    if parts[:2] == ["var", "folders"]:
        return any(part in NEVER_TOUCH_PARTS for part in parts)
    if parts[0] in PROTECTED_TOP_LEVEL:
        return True
    if parts[0] == "users" and len(parts) > 1 and parts[1] in SYSTEM_PROFILES:
        return True
    return any(part in NEVER_TOUCH_PARTS for part in parts)


def _in_config_home(parts: list[str]) -> bool:
    for i, part in enumerate(parts):
        if part in CONFIG_HOMES:
            return True
        # ~/Library on macOS (the root-level /Library is already protected)
        if part == MAC_LIBRARY and i > 0:
            return True
    return False


def reject_reason(
    path: str,
    mtime: float,
    *,
    is_dir: bool = False,
    is_duplicate: bool = False,
    managed_by: str | None = None,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    now: float | None = None,
) -> str | None:
    """Return a short reason string if this item must never be suggested."""
    parts = _parts(path)
    if not parts:
        return "drive root"
    if is_protected(path):
        return "protected location"
    if managed_by:
        return "managed by another app"
    # files sitting right in C:\ (or /) are things like pagefile.sys and
    # hiberfil.sys. Folders there, like C:\Temp, are still fair game.
    if len(parts) == 1 and not is_dir:
        return "system file at the top of the drive"

    now = time.time() if now is None else now
    age_days = (now - mtime) / 86400
    if age_days < min_age_days:
        return f"modified in the last {min_age_days} days"

    if _in_config_home(parts) and not any(p in DISPOSABLE_PARTS for p in parts):
        return "application data"

    if not is_duplicate and any(p in PERSONAL_DIRS for p in parts[:-1]):
        return "personal folder"

    return None
