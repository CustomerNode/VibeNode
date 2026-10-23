"""Session Host wire protocol, records and paths (spec: docs/plans/session-host-spec.md).

A Session Host (``daemon/session_host.py``) is one small process per live
session that owns the CLI's stdio, so the Session Engine can restart without
the CLI dying.  This module is what the host and the engine share: the frame
format, the record and launch-spec files, the secret, and two read-only
process probes.  It imports nothing heavy (no SDK, no SessionManager), so the
registry, the host and the tests can all use it.

Frames are newline-delimited JSON over TCP on 127.0.0.1.

  engine -> host: ``hello {secret, proto}``, ``write {line}`` (a raw CLI stdin
                  line), ``ack {seq}``, ``end_input``, ``detach``,
                  ``shutdown``, ``rename {sid}``
  host -> engine: ``hello_ok {...}`` | ``hello_err {reason, ...}``,
                  ``msg {seq, line, r?}`` (one raw CLI stdout line; ``r`` marks
                  a replayed line), ``cli_exit {code}``, ``bye``

The first frame must be a ``hello`` carrying the record's secret, or the host
closes the connection.  One engine is attached at a time; the newest wins.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from common.paths import DATA_DIR, REPO_ROOT

PROTO = 1

# Module globals so tests (tests/conftest.py) can point them at a sandbox.
# The engine passes both to every host it spawns (``--hosts-dir`` /
# ``--logs-dir``), so a host always writes where its engine looks.
HOSTS_DIR: Path = DATA_DIR / "session_hosts"
LOGS_DIR: Path = REPO_ROOT / "logs" / "session_hosts"

# asyncio's default StreamReader line limit is 64 KB; one CLI line (a large
# tool result) can be many MB.  Matches the SDK's raised JSON ceiling
# (sdk_patches patch 5, 100 MB) with headroom for the frame's own JSON.
FRAME_LIMIT = 160 * 1024 * 1024

# Default of VIBENODE_SESSION_HOST.  ON since the step-6 acceptance run on
# Windows (2026-10-23): test_session_host_process / _transport / _restart /
# _reaper (incl. test_host_survives_parent_kill) all passed with real host
# processes, three runs in a row.  See
# docs/plans/runs/2026-10-23-0900-review-exec/B2-session-host.md.
DEFAULT_ENABLED = True


def enabled() -> bool:
    """``VIBENODE_SESSION_HOST``: launch new sessions inside a Session Host.
    Read at call time so a test can flip it.  Off only changes how NEW
    sessions start; hosts already running are always reattached."""
    raw = os.environ.get("VIBENODE_SESSION_HOST")
    if raw is None or not raw.strip():
        return DEFAULT_ENABLED
    return raw.strip().lower() not in ("0", "false", "no", "off")


def orphan_seconds() -> float:
    """``VIBENODE_HOST_ORPHAN_MINUTES`` (default 30, fractions allowed): how
    long a host with no engine attached and no turn in flight waits before it
    closes its CLI and exits."""
    try:
        minutes = float(os.environ.get("VIBENODE_HOST_ORPHAN_MINUTES", "30") or "30")
    except ValueError:
        minutes = 30.0
    return max(1.0, minutes * 60.0)


# ── Paths ────────────────────────────────────────────────────────────────

def safe_id(session_id: str) -> str:
    """A session id with anything that could escape a folder removed."""
    return "".join(c for c in str(session_id) if c.isalnum() or c in "-_")


def hosts_dir(override=None) -> Path:
    return Path(override) if override else HOSTS_DIR


def record_path(sid: str, base=None) -> Path:
    return hosts_dir(base) / f"{safe_id(sid)}.json"


def launch_path(sid: str, base=None) -> Path:
    return hosts_dir(base) / f"{safe_id(sid)}.launch.json"


def spill_path(sid: str, base=None) -> Path:
    return hosts_dir(base) / f"{safe_id(sid)}.buf"


def log_path(sid: str, base=None) -> Path:
    return (Path(base) if base else LOGS_DIR) / f"{safe_id(sid)}.log"


# ── Atomic JSON IO ───────────────────────────────────────────────────────

def write_json_atomic(path: Path, data: dict) -> None:
    """Temp file + ``os.replace`` (as ``durable_jobs.save_registry``).  Mode
    0600 on POSIX: records carry the host's secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".host.", suffix=".tmp", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        # Windows refuses to replace a file another process has open (the
        # engine reading a record while its host writes it): retry briefly.
        for attempt in range(50):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.02)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[dict]:
    """The file's JSON object, or None if it is missing or not an object.

    Windows refuses to open a file while another process ``os.replace``s it
    (a host saving its record as the engine reads it).  That refusal is
    transient, so it is retried for up to a second: reading it as "no record"
    made ``list_records`` skip a live host (reconcile would not reattach it,
    a reaper would not shut it down)."""
    for attempt in range(50):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == 49:
                return None
            time.sleep(0.02)
        except Exception:
            return None
    return None


def read_record(sid: str, base=None) -> Optional[dict]:
    return read_json(record_path(sid, base))


def list_records(base=None) -> list:
    """Every host record in the folder (launch specs and temp files skipped)."""
    out = []
    d = hosts_dir(base)
    try:
        paths = sorted(d.glob("*.json"))
    except Exception:
        return out
    for p in paths:
        if p.name.endswith(".launch.json") or p.name.startswith("."):
            continue
        rec = read_json(p)
        if rec and rec.get("sid"):
            rec["_path"] = str(p)
            out.append(rec)
    return out


def delete_host_files(sid: str, base=None) -> None:
    for p in (record_path(sid, base), launch_path(sid, base), spill_path(sid, base)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


# ── Secret and frames ────────────────────────────────────────────────────

def new_secret() -> str:
    return secrets.token_hex(32)


def secret_ok(given, expected) -> bool:
    if not isinstance(given, str) or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def encode(frame: dict) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def read_frame(reader: asyncio.StreamReader) -> Optional[dict]:
    """The next frame, or None at EOF.  A malformed line is skipped."""
    while True:
        try:
            raw = await reader.readline()
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        except ValueError as exc:
            # A line over FRAME_LIMIT: the stream cannot be resynchronised.
            import logging
            logging.getLogger(__name__).warning("read_frame: oversized frame: %s", exc)
            return None
        if not raw:
            return None
        try:
            frame = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if isinstance(frame, dict):
            return frame


# ── Read-only process probes (never signal anything) ─────────────────────

def process_alive(pid) -> bool:
    """True iff ``pid`` exists.  Windows-safe: never ``os.kill(pid, 0)``,
    which TERMINATES the process on Windows (see
    ``SessionManager._process_alive``)."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_create_time(pid) -> Optional[float]:
    """When ``pid`` was created (epoch seconds), or None if unknown.  The pair
    (pid, create time) identifies one process: a pid reused by another program
    has a different create time.  psutil when installed, else the OS directly."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        import psutil  # optional
        return float(psutil.Process(pid).create_time())
    except ImportError:
        pass
    except Exception:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                                            ctypes.byref(k), ctypes.byref(u)):
                return None
            ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return (ticks - 116444736000000000) / 1e7
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                stat = fh.read()
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            with open("/proc/stat", encoding="utf-8") as fh:
                btime = next(int(l.split()[1]) for l in fh if l.startswith("btime"))
            return btime + start_ticks / os.sysconf("SC_CLK_TCK")
        except Exception:
            return None
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y")) if out else None
    except Exception:
        return None


def process_name(pid) -> str:
    """The executable name of ``pid`` (lower case, no path), or ''."""
    if type(pid) is not int or pid <= 0:
        return ""
    try:
        import psutil  # optional
        return (psutil.Process(pid).name() or "").lower()
    except ImportError:
        pass
    except Exception:
        return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                return fh.read().strip().lower()
        except Exception:
            return ""
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return os.path.basename(out).lower()
    except Exception:
        return ""


def same_process(pid, create_time, name="") -> bool:
    """True only when ``pid`` is alive AND is provably the process a record
    describes: its create time matches (to the second) and, when a name was
    recorded, so does its name.  Anything unknown is False (fail closed)."""
    if type(pid) is not int or pid <= 1:
        return False
    try:
        recorded = float(create_time)
    except (TypeError, ValueError):
        return False
    if recorded <= 0 or not process_alive(pid):
        return False
    actual = process_create_time(pid)
    if actual is None or abs(actual - recorded) > 1.0:
        return False
    if name:
        return process_name(pid) == str(name).lower()
    return True
