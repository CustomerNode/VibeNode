"""Folders the account may enter but not list.

Every contestant session runs as the same user, so a folder can never be
locked away from one and kept open to the controller. What CAN be enforced
with the operating system's own permissions is enumeration: a parent folder
whose listing is denied to the account. Windows grants "bypass traverse
checking" by default, and a POSIX folder with mode 311 behaves the same way:
a path you KNOW still opens, a path you do not know cannot be found. So each
trial's copy lives under such a parent with an unguessable name; a contestant
reaches its own copy (its working folder) and the controller reaches all of
them, but nobody can browse for siblings, the key or the canary.

Limits, stated wherever this is used: the account owns the folder and can
change the permission back (the break-out watch flags that), and anything
that tells a contestant a sibling's name (VibeNode's own session list, the
transcript folder) is a side door the watch flags too.
"""

from __future__ import annotations

import getpass
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


def _user() -> str:
    return getpass.getuser()


def _icacls(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def hide(folder: Path) -> dict:
    """Deny listing of ``folder`` to this account. Returns ``{ok, method, detail}``."""
    folder = Path(folder)
    if sys.platform == "win32":
        r = _icacls(str(folder), "/deny", f"{_user()}:(RD)")
        ok = r.returncode == 0 and is_hidden(folder) is True
        return {"ok": ok, "method": "windows acl: deny list-folder to the account",
                "detail": (r.stdout + r.stderr).strip()[:300]}
    try:
        os.chmod(folder, stat.S_IWUSR | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)  # 0311
        ok = is_hidden(folder) is True
        return {"ok": ok, "method": "posix mode 311 (enter, not list)", "detail": ""}
    except OSError as e:
        return {"ok": False, "method": "posix mode 311 (enter, not list)", "detail": str(e)}


def unhide(folder: Path) -> bool:
    folder = Path(folder)
    if sys.platform == "win32":
        r = _icacls(str(folder), "/remove:d", _user())
        return r.returncode == 0 and is_hidden(folder) is False
    try:
        os.chmod(folder, 0o755)
        return True
    except OSError:
        return False


def is_hidden(folder: Path):
    """True when listing is denied to the account, False when not, None when
    it cannot be told (the folder is gone, or the tool failed)."""
    folder = Path(folder)
    if not folder.exists():
        return None
    if sys.platform == "win32":
        r = _icacls(str(folder))
        if r.returncode != 0:
            return None
        # The folder's path shares the first line with the first entry, so match
        # the entry itself: "<DOMAIN\>user:(DENY)(RD)".
        pat = re.compile(r"(?:^|\s)(?:[^\s\\]+\\)?" + re.escape(_user()) + r":\(DENY\)\(RD\)", re.I)
        return any(pat.search(line) for line in r.stdout.splitlines())
    try:
        mode = folder.stat().st_mode
    except OSError:
        return None
    return not bool(mode & stat.S_IRUSR)


def listable(folder: Path) -> bool:
    """What a contestant would get: can the account enumerate ``folder``?"""
    try:
        os.listdir(folder)
        return True
    except PermissionError:
        return False
    except OSError:
        return False
