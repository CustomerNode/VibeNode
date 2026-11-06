"""Self-healing Windows desktop shortcut.

A shortcut created by hand, or before a file moved, can point at a launcher
that no longer exists, at python.exe (console flash) instead of pythonw.exe,
or at an old icon path. Every launch finds the shortcuts that belong to this
install and rewrites the broken ones in place, so the user never has to.

Shortcuts are found by what they point at, not by name: any .lnk on the
user's desktop whose target, arguments or start folder sit inside the repo.
The desktop folder is asked of Windows, because OneDrive and folder
redirection move it away from ~/Desktop.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_CSIDL_DESKTOPDIRECTORY = 0x10

_PS_READ = (
    "$ws = New-Object -ComObject WScript.Shell;"
    "$out = @();"
    "foreach ($d in ($env:VN_DESKTOPS -split [char]10)) {"
    "  if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }"
    "  foreach ($f in Get-ChildItem -LiteralPath $d -Filter *.lnk -File) {"
    "    $l = $ws.CreateShortcut($f.FullName);"
    "    $out += [pscustomobject]@{path=$f.FullName; target=$l.TargetPath;"
    "      args=$l.Arguments; workdir=$l.WorkingDirectory; icon=$l.IconLocation}"
    "  }"
    "};"
    "ConvertTo-Json -InputObject @($out) -Compress"
)

_PS_WRITE = (
    "$ws = New-Object -ComObject WScript.Shell;"
    "foreach ($p in ($env:VN_LNKS -split [char]10)) {"
    "  if (-not $p) { continue }"
    "  $l = $ws.CreateShortcut($p);"
    "  $l.TargetPath = $env:VN_TARGET;"
    "  $l.Arguments = '\"' + $env:VN_SCRIPT + '\"';"
    "  $l.WorkingDirectory = $env:VN_WORKDIR;"
    "  $l.IconLocation = $env:VN_ICON + ',0';"
    "  $l.WindowStyle = 7;"
    "  $l.Save()"
    "}"
)


def desktop_dirs():
    """The user's desktop folders: ~/Desktop plus wherever Windows put it."""
    dirs = [Path.home() / "Desktop"]
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, _CSIDL_DESKTOPDIRECTORY, None, 0, buf) == 0 and buf.value:
            dirs.append(Path(buf.value))
    except Exception:
        pass
    seen, unique = set(), []
    for d in dirs:
        key = os.path.normcase(str(d))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _inside(path_text, root):
    if not path_text:
        return False
    p = os.path.normcase(os.path.normpath(path_text.strip().strip('"')))
    r = os.path.normcase(os.path.normpath(str(root)))
    return p == r or p.startswith(r + os.sep)


def _first_arg(args):
    """The first argument of a shortcut's argument string, quotes removed."""
    args = (args or "").strip()
    if args.startswith('"'):
        end = args.find('"', 1)
        return args[1:end] if end > 0 else args[1:]
    return args.split(" ", 1)[0]


def belongs_to(info, root):
    """True when the shortcut launches the install rooted at ``root``."""
    return (_inside(info.get("target"), root)
            or _inside(_first_arg(info.get("args")), root)
            or _inside(info.get("workdir"), root))


def needs_repair(info, icon):
    """True unless the shortcut runs pythonw on a script that exists, with our icon."""
    target = info.get("target") or ""
    if Path(target).name.lower() != "pythonw.exe" or not Path(target).exists():
        return True
    script = _first_arg(info.get("args"))
    if not script or not Path(script).exists():
        return True
    return str(icon).lower() not in (info.get("icon") or "").lower()


def _run_ps(script, env_extra, timeout):
    env = dict(os.environ, **env_extra)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def fix_shortcuts(root, script, icon):
    """Rewrite every broken desktop shortcut of this install. Best effort."""
    if sys.platform != "win32":
        return []
    try:
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if not pythonw.exists():
            return []
        r = _run_ps(_PS_READ,
                    {"VN_DESKTOPS": "\n".join(str(d) for d in desktop_dirs())}, 15)
        found = json.loads(r.stdout.strip() or "[]")
        if isinstance(found, dict):
            found = [found]
        broken = [i["path"] for i in found
                  if belongs_to(i, root) and needs_repair(i, icon)]
        if broken:
            _run_ps(_PS_WRITE, {
                "VN_LNKS": "\n".join(broken),
                "VN_TARGET": str(pythonw),
                "VN_SCRIPT": str(script),
                "VN_WORKDIR": str(Path(script).parent),
                "VN_ICON": str(icon),
            }, 15)
        return broken
    except Exception:
        return []  # Best effort: never block startup
