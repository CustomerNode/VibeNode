"""Size-capped log files for VibeNode's long-lived processes.

Why this exists.  The Session Engine's log (``logs/daemon_debug.log``) used to
be the stdout/stderr handle its spawner opened and handed down.  Nothing
capped it, so it reached 100+ MB, and trimming it from outside did not work:
on Windows a child does not inherit append mode, so after an outside
truncation the engine kept writing at its old offset and the file came back
at full size with the gap filled by NUL bytes (measured 2026-10-16: 109 MB of
NULs in a 120 MB file).

The rule now: a log is capped by the one process that writes it, through a
handle it opened itself in append mode.

Rotation is copy-then-truncate, never rename.  On Windows a rename fails while
any other process holds the file open (a spawner's leftover handle, an editor,
``Get-Content -Wait``), and a failed rename would leave the log uncapped.
Copy-then-truncate works with other handles open.  Because the owner writes in
append mode, its next write lands at the new end, so no gap is created.

Stdlib only: the reviver imports this while the rest of VibeNode may be broken.
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

# 10 MB live file plus two backups: at most ~30 MB per log.  The engine writes
# roughly 1-5 MB a day, so this holds several days of history.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 2

# After a failed rotation, wait this long before trying again, so a log that
# cannot rotate does not retry on every line.
_RETRY_AFTER_S = 60.0
# Check the real size after this many bytes written, not on every line.
_CHECK_EVERY_BYTES = 256 * 1024
_COPY_CHUNK = 1024 * 1024


def _backup(path: Path, n: int) -> Path:
    return path.with_name("%s.%d" % (path.name, n))


def rotate(path, max_bytes: int = DEFAULT_MAX_BYTES,
           backups: int = DEFAULT_BACKUPS) -> bool:
    """Move the tail of ``path`` into ``path.1`` and empty ``path`` in place.

    Older backups shift up (``.1`` → ``.2``) and the oldest is dropped.  The
    backup keeps at most the last ``max_bytes`` of the log, and NUL bytes are
    left out, so a file that grew a gap heals here.  Returns True when the
    log was emptied.  Raises OSError when it could not be.
    """
    path = Path(path)
    if backups > 0:
        for n in range(backups - 1, 0, -1):
            src = _backup(path, n)
            if src.exists():
                try:
                    os.replace(src, _backup(path, n + 1))
                except OSError:
                    pass  # an old backup held open: overwrite it below instead
        size = path.stat().st_size
        with open(path, "rb") as src_fh, open(_backup(path, 1), "wb") as dst:
            start = max(0, size - max_bytes)
            src_fh.seek(start)
            if start:
                src_fh.readline()  # start the backup on a whole line
            while True:
                chunk = src_fh.read(_COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk.replace(b"\x00", b""))
    with open(path, "r+b") as fh:
        fh.truncate(0)
    return True


def rotate_if_over(path, max_bytes: int = DEFAULT_MAX_BYTES,
                   backups: int = DEFAULT_BACKUPS) -> bool:
    """Rotate ``path`` when it is over ``max_bytes``.  Never raises.

    For logs written open-append-close (one short-lived handle per line), where
    the caller can simply check before each write.
    """
    try:
        if Path(path).stat().st_size <= max_bytes:
            return False
        return rotate(path, max_bytes, backups)
    except OSError:
        return False


def _has_gap(path: Path) -> bool:
    """True when the file starts with a NUL byte, the mark of an outside trim."""
    try:
        with open(path, "rb") as fh:
            return fh.read(1) == b"\x00"
    except OSError:
        return False


class CappedLogFile(io.TextIOBase):
    """A text stream to a log file that keeps the file under ``max_bytes``.

    Thread-safe.  Every write is flushed, so a crash never loses the last
    lines.  Usable as ``sys.stdout`` / ``sys.stderr`` and as the stream of a
    ``logging.StreamHandler``.
    """

    def __init__(self, path, max_bytes: int = DEFAULT_MAX_BYTES,
                 backups: int = DEFAULT_BACKUPS):
        super().__init__()
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        # Re-entrant: IOBase.close() calls flush() while close() holds it.
        self._lock = threading.RLock()
        self._since_check = 0
        self._retry_at = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Heal on open: an oversized file, or one with a NUL gap left by an
        # older spawner's handle, starts this run empty.
        try:
            if self.path.exists() and (
                    self.path.stat().st_size > max_bytes or _has_gap(self.path)):
                rotate(self.path, max_bytes, backups)
        except OSError:
            pass
        self._fh = open(self.path, "a", encoding="utf-8", errors="replace")

    # -- stream protocol ---------------------------------------------------
    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._fh.fileno()

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    @property
    def buffer(self):
        # For code that writes bytes to sys.stdout.buffer.  Those bytes skip
        # the size check until the next text write; the cap still holds.
        return self._fh.buffer

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._fh.write(s)
            self._fh.flush()
            self._since_check += len(s)
            if self._since_check >= _CHECK_EVERY_BYTES:
                self._since_check = 0
                self._maybe_rotate_locked()
        return len(s)

    def flush(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
            except (ValueError, OSError):
                pass  # already closed

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            finally:
                super().close()

    # -- rotation ----------------------------------------------------------
    def _maybe_rotate_locked(self) -> None:
        now = time.monotonic()
        if now < self._retry_at:
            return
        try:
            # fstat, not tell(): a buffered tell() can be stale after a truncate.
            if os.fstat(self._fh.fileno()).st_size <= self.max_bytes:
                return
            rotate(self.path, self.max_bytes, self.backups)
        except OSError:
            self._retry_at = now + _RETRY_AFTER_S


def open_capped(path, max_bytes: int = DEFAULT_MAX_BYTES,
                backups: int = DEFAULT_BACKUPS) -> CappedLogFile:
    return CappedLogFile(path, max_bytes, backups)
