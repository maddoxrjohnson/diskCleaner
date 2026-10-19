import time

import pytest

from diskcleaner.safety import is_protected, reject_reason

OLD = time.time() - 400 * 86400


@pytest.mark.parametrize(
    "path",
    [
        "/usr/lib/libfoo.so",
        "/etc/passwd",
        "/mnt/c/Windows/System32/big.dll",
        "/mnt/c/Program Files/App/data.bin",
        "/home/me/.ssh/id_rsa",
        "/home/me/code/repo/.git/objects/pack/pack-1.pack",
        "/home/me/OneDrive/backup.zip",
        "/private/etc/hosts",
        "/mnt/c/Users/me/Steam/steamapps/common/Balatro/Balatro.exe",
        "/mnt/c/Program Files/Epic Games/Fortnite/big.pak",
        "/mnt/d/XboxGames/Halo/Content/game.exe",
        "/mnt/c/Windows.old/Users/me/old.iso",
        "/mnt/c/System Repair/image.wim",
        "/mnt/c/Drivers/audio/setup.exe",
        "/mnt/c/dell/updatepackage/log.zip",
        "/mnt/c/Users/Default/AppData/Local/Temp/x.tmp",
        "/private/var/db/stuff",
    ],
)
def test_protected_paths(path):
    assert is_protected(path)
    assert reject_reason(path, OLD) is not None


@pytest.mark.parametrize(
    "path",
    [
        "/home/me/Downloads/installer.exe",
        "/mnt/c/Users/me/Downloads/old.iso",
        "/home/me/code/app/node_modules",
        "/private/var/folders/xy/T/pytest-1/big.bin",
    ],
)
def test_normal_paths_allowed(path):
    assert not is_protected(path)
    assert reject_reason(path, OLD) is None


def test_recent_files_are_rejected():
    recent = time.time() - 3 * 86400
    assert "modified" in reject_reason("/home/me/Downloads/new.zip", recent)


def test_min_age_is_configurable():
    recent = time.time() - 3 * 86400
    assert reject_reason("/home/me/Downloads/new.zip", recent, min_age_days=1) is None


def test_app_data_only_allowed_in_cache_or_temp():
    assert reject_reason("/mnt/c/Users/me/AppData/Roaming/Slack/storage.db", OLD)
    assert reject_reason("/home/me/.config/Code/state.db", OLD)
    assert reject_reason("/mnt/c/Users/me/AppData/Local/Temp/blob.tmp", OLD) is None
    assert reject_reason("/home/me/.local/share/pip/cache/wheel.whl", OLD) is None


def test_personal_folders_need_to_be_duplicates():
    path = "/home/me/Pictures/2019/vacation.mov"
    assert reject_reason(path, OLD) == "personal folder"
    assert reject_reason(path, OLD, is_duplicate=True) is None


def test_drive_root_is_never_suggested():
    assert reject_reason("/", OLD) == "drive root"
    assert reject_reason("/mnt/c", OLD) == "drive root"


def test_files_at_the_top_of_a_drive():
    assert reject_reason("/mnt/c/hiberfil.sys", OLD) == "system file at the top of the drive"
    assert reject_reason("/mnt/c/pagefile.sys", OLD) == "system file at the top of the drive"
    assert reject_reason("/swapfile", OLD) == "system file at the top of the drive"
    # but a folder like C:\Temp is fine
    assert reject_reason("/mnt/c/Temp", OLD, is_dir=True) is None
    assert reject_reason("/mnt/c/Temp/old.log", OLD) is None


def test_app_managed_items_are_never_suggested():
    path = "/mnt/c/Users/me/.android/avd/Pixel_6.avd"
    assert reject_reason(path, OLD, is_dir=True) is None
    assert reject_reason(path, OLD, is_dir=True, managed_by="Android emulator") == (
        "managed by another app"
    )
