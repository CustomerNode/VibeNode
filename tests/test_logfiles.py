"""Size-capped logs (common/logfiles.py).

The engine log reached 100+ MB, and after an outside trim it grew back padded
with NUL bytes, because the engine wrote through a handle it inherited
without append mode.  These tests pin the replacement: each process caps its
own log, rotation works while other handles are open, the owner's next write
lands at the new end (no gap), and a file that already has a gap heals.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from common import logfiles
from common.logfiles import CappedLogFile, rotate, rotate_if_over

REPO = Path(__file__).resolve().parents[1]


def _bak(p, n):
    return p.with_name("%s.%d" % (p.name, n))


def test_rotate_keeps_the_tail_strips_nuls_and_shifts_backups(tmp_path):
    log = tmp_path / "x.log"
    _bak(log, 1).write_text("older\n")
    _bak(log, 2).write_text("oldest\n")
    log.write_bytes(b"\x00" * 500 + b"A" * 99 + b"\n" + b"B" * 99 + b"\n")

    assert rotate(log, max_bytes=150, backups=2) is True

    assert log.read_bytes() == b""
    # the tail, cut back to a whole line
    assert _bak(log, 1).read_bytes() == b"B" * 99 + b"\n"
    assert _bak(log, 2).read_text() == "older\n"  # the oldest was dropped


def test_rotate_if_over_leaves_small_files_alone(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("small\n")
    assert rotate_if_over(log, max_bytes=1000) is False
    assert log.read_text() == "small\n"
    assert rotate_if_over(tmp_path / "missing.log") is False  # never raises


def test_capped_log_stays_bounded_and_never_grows_a_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(logfiles, "_CHECK_EVERY_BYTES", 1)
    log = tmp_path / "engine.log"
    stream = CappedLogFile(log, max_bytes=2000, backups=2)
    line = "x" * 99 + "\n"
    for _ in range(205):  # ~20 KB through a 2 KB cap, ending mid-cycle
        stream.write(line)
    stream.close()

    assert log.stat().st_size <= 2000 + len(line)
    for p in (log, _bak(log, 1), _bak(log, 2)):
        data = p.read_bytes()
        assert b"\x00" not in data, "%s has a NUL gap" % p.name
        assert data and set(data.splitlines()) == {line.strip().encode()}
    assert not _bak(log, 3).exists()


def test_rotation_works_while_another_process_holds_the_file(tmp_path, monkeypatch):
    """The Windows case: a rename would fail here, copy-then-truncate does not."""
    monkeypatch.setattr(logfiles, "_CHECK_EVERY_BYTES", 1)
    log = tmp_path / "engine.log"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import sys, time
            fh = open(sys.argv[1], "a")
            print("held", flush=True)
            sys.stdin.read()
        """), str(log)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        stream = CappedLogFile(log, max_bytes=1000, backups=1)
        for i in range(50):
            stream.write("line %04d %s\n" % (i, "y" * 40))
        stream.close()
    finally:
        holder.communicate("", timeout=10)

    assert log.stat().st_size <= 1100
    assert _bak(log, 1).exists()
    assert b"\x00" not in log.read_bytes()
    assert log.read_text().splitlines()[-1].startswith("line 0049")


def test_opening_heals_a_file_with_a_nul_gap(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"\x00" * 4096 + b"real line\n")

    stream = CappedLogFile(log, max_bytes=10_000)
    stream.write("after heal\n")
    stream.close()

    assert log.read_text() == "after heal\n"
    assert _bak(log, 1).read_bytes() == b"real line\n"


def test_a_failed_rotation_backs_off_and_keeps_logging(tmp_path, monkeypatch):
    monkeypatch.setattr(logfiles, "_CHECK_EVERY_BYTES", 1)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise PermissionError("locked")

    log = tmp_path / "engine.log"
    stream = CappedLogFile(log, max_bytes=100)
    monkeypatch.setattr(logfiles, "rotate", boom)
    for _ in range(50):
        stream.write("z" * 50 + "\n")
    stream.close()

    assert len(calls) == 1, "retried on every line instead of backing off"
    assert log.read_text().count("\n") == 50


def test_usable_as_a_logging_stream(tmp_path):
    import logging
    log = tmp_path / "engine.log"
    stream = CappedLogFile(log)
    lg = logging.getLogger("test_logfiles.stream")
    h = logging.StreamHandler(stream)
    lg.addHandler(h)
    try:
        lg.warning("hello %s", "world")
    finally:
        lg.removeHandler(h)
        stream.close()
    assert log.read_text() == "hello world\n"
    assert stream.encoding == "utf-8" and not stream.isatty()


@pytest.mark.parametrize("rel", ["run.py", "app/daemon_client.py"])
def test_no_spawner_hands_the_engine_its_own_log(rel):
    """Two writers on daemon_debug.log is what left the NUL gap."""
    src = (REPO / rel).read_text(encoding="utf-8")
    code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    assert not any('"daemon_debug.log"' in l for l in code), (
        "%s passes daemon_debug.log to the engine as stdout again" % rel)
    assert any('"daemon_console.log"' in l for l in code)


def test_the_engine_opens_its_own_capped_log():
    src = (REPO / "daemon" / "daemon_server.py").read_text(encoding="utf-8")
    assert "open_capped(" in src and '"daemon_debug.log"' in src
    assert "stream=_engine_log" in src
