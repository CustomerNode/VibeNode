"""Session Host bookkeeping on the engine side (spec: docs/plans/session-host-spec.md).

* :func:`reconcile` runs inside ``SessionManager.start()``, before recovery
  and before the engine binds its IPC port: every host a previous engine left
  running is reattached; a dead host's record is cleared (and its CLI killed
  only if provably still that CLI).
* :func:`sweep` runs with the 60 s orphan sweep: it shuts down the host of a
  session this engine has put to sleep, retires a host speaking another
  protocol once it is idle, and clears dead records.
* :func:`ensure_host_gone` / :func:`guarded_kill` are the fallback when a host
  does not answer: the engine never signals a pid it read from a file unless
  that pid's create time (and name) still match the record.

The engine never tree-kills a host or a CLI by bare pid.  Hosts exit on their
own (CLI exit, ``shutdown``, or the orphan timer) and kill their own child.
"""

from __future__ import annotations

import logging
import threading
import time

from daemon import session_host_protocol as shp

logger = logging.getLogger(__name__)

# A host this young may still be between spawn and its engine's attach.
SPAWN_GRACE_S = 60.0


def guarded_kill(record: dict, kill_tree) -> list:
    """Kill the CLI, then the host, of ``record``, each ONLY if it is provably
    the process the record describes (pid alive, create time and name match).
    ``kill_tree`` is ``SessionManager._kill_process_tree`` (its own guards
    apply on top).  Returns what was killed."""
    killed = []
    for role in ("cli", "host"):
        pid = record.get(f"{role}_pid")
        if shp.same_process(pid, record.get(f"{role}_create_time"), record.get(f"{role}_name") or ""):
            logger.warning("Session Host %s: killing unreachable %s pid %s (create time matches)",
                           record.get("sid"), role, pid)
            try:
                kill_tree(pid)
                killed.append(role)
            except Exception:
                logger.exception("guarded kill of %s pid %s failed", role, pid)
    return killed


def _kill_tree():
    from daemon.session_manager import SessionManager
    return SessionManager._kill_process_tree


def host_alive(record: dict) -> bool:
    return shp.same_process(record.get("host_pid"), record.get("host_create_time"),
                            record.get("host_name") or "")


def ensure_host_gone(record: dict, wait: float = 5.0) -> None:
    """After a ``shutdown``: wait for the host to exit; if it did not, kill
    its CLI and itself, guarded.  Then clear its files."""
    from daemon import session_host_transport as sht
    if not sht.wait_host_gone(record, wait):
        if not sht.shutdown_host_sync(record, wait=10.0):
            guarded_kill(record, _kill_tree())
    elif shp.same_process(record.get("cli_pid"), record.get("cli_create_time"),
                          record.get("cli_name") or ""):
        guarded_kill({k: v for k, v in record.items() if not k.startswith("host_")}, _kill_tree())
    sid = record.get("sid")
    if sid:
        cur = shp.read_record(sid)
        if cur is None or cur.get("host_pid") == record.get("host_pid"):
            shp.delete_host_files(sid)


def _clear_dead(record: dict) -> None:
    """A record whose host is gone: kill a CLI that provably survived it."""
    logger.info("Session Host %s: host pid %s is gone; clearing its record",
                record.get("sid"), record.get("host_pid"))
    cli_only = {k: v for k, v in record.items() if not k.startswith("host_")}
    guarded_kill(cli_only, _kill_tree())
    shp.delete_host_files(record.get("sid") or "")


def _retire(sm, record: dict, force: bool = False) -> str:
    """Shut down a host speaking another protocol, once its turn is idle
    (``force``: even mid-turn).  Returns 'retired', 'busy' or 'unreachable'."""
    from daemon import session_host_transport as sht
    import asyncio

    async def ask():
        reader, writer, reply = await sht.open_host(record, timeout=5.0)
        try:
            if reply.get("t") == "hello_err" and reply.get("reason") == "proto":
                if reply.get("turn_in_flight") and not force:
                    return "busy"
                writer.write(shp.encode({"t": "shutdown"}))
                await writer.drain()
                while True:
                    f = await asyncio.wait_for(shp.read_frame(reader), 15.0)
                    if f is None or f.get("t") == "bye":
                        return "retired"
            return "unreachable"
        finally:
            writer.close()
    try:
        return asyncio.run(asyncio.wait_for(ask(), 25.0))
    except Exception:
        return "unreachable"


def reconcile(sm) -> set:
    """Reattach every live host in the hosts folder to ``sm``.  Returns the
    session ids now attached (``recover_sessions`` skips them) plus the ids of
    hosts that must not be resumed yet (a mismatched protocol mid-turn; the
    sweep retires them and resumes the session once idle)."""
    records = shp.list_records()
    if not records:
        return set()
    live = []
    for rec in records:
        if host_alive(rec):
            live.append(rec)
        else:
            _clear_dead(rec)
    if not live:
        return set()
    attached = set()
    results = sm.attach_hosted_sessions(live)
    for rec in live:
        sid = rec.get("sid")
        got = results.get(sid)
        if got:
            attached.add(got)
            continue
        if int(rec.get("proto") or 0) != shp.PROTO:
            outcome = _retire(sm, rec)
            if outcome == "busy":
                logger.info("Session Host %s speaks protocol %s; retiring it once its "
                            "turn ends", sid, rec.get("proto"))
                _retiring(sm)[sid] = dict(rec, _retiring_since=time.time())
                attached.add(sm._resolve_id(sid))
                continue
            if outcome == "retired":
                logger.info("Session Host %s (protocol %s) retired; the session resumes",
                            sid, rec.get("proto"))
                continue
        # Alive but unreachable: two CLIs must never run one session, so the
        # host goes (guarded) before recovery resumes it.
        logger.warning("Session Host %s is alive but did not accept the attach; "
                       "shutting it down so the session can resume", sid)
        ensure_host_gone(rec, wait=0.5)
    return attached


def _retiring(sm) -> dict:
    d = getattr(sm, "_host_retiring", None)
    if not isinstance(d, dict):
        d = {}
        try:
            sm._host_retiring = d
        except Exception:
            pass
    return d


def sweep(sm) -> None:
    """The 60 s backstop.  Acts ONLY on records whose session this engine
    knows about, so an engine never touches hosts it has no claim on (a
    leaked test engine, or a second engine, cannot shut down live sessions):

    * the session is STOPPED here (slept/closed) but its host still runs:
      shut it down;
    * the session was closed here and then deleted (``sm._host_closed``), and
      its host still runs: shut it down;
    * a retiring host (other protocol) has gone idle: retire it, then resume
      the session;
    * the record's host is dead: clear it.
    """
    try:
        records = shp.list_records()
    except Exception:
        return
    retiring = _retiring(sm)
    now = time.time()
    from daemon import session_host_transport as sht
    for rec in records:
        sid = rec.get("sid") or ""
        rid = sm._resolve_id(sid)
        with sm._lock:
            info = sm._sessions.get(rid)
        if sid in retiring:
            # No engine can answer a turn stuck on a permission prompt in a
            # host of another protocol, so a busy host is waited on only as
            # long as an orphan would be; then it is retired mid-turn and the
            # session resumes (losing that turn, as --resume always did).
            since = float((retiring.get(sid) or {}).get("_retiring_since") or now)
            force = now - since > shp.orphan_seconds()
            if not host_alive(rec) or _retire(sm, rec, force=force) == "retired":
                retiring.pop(sid, None)
                shp.delete_host_files(sid)
                cwd = rec.get("cwd") or ""
                logger.info("Session Host %s retired; resuming the session", sid)
                threading.Thread(target=sm.start_session, kwargs=dict(
                    session_id=rid, cwd=cwd, resume=True), daemon=True).start()
            continue
        if info is None:
            # Unknown here: not ours, unless this engine closed it (a session
            # slept and then deleted, whose host ignored the shutdown).
            closed = getattr(sm, "_host_closed", None)
            if not isinstance(closed, set) or not ({sid, rid} & closed):
                continue
            if not host_alive(rec):
                _clear_dead(rec)
            else:
                logger.info("Session Host %s outlived its deleted session; shutting it down", sid)
                if not sht.shutdown_host_sync(rec, wait=10.0):
                    ensure_host_gone(rec, wait=0.5)
            continue
        if now - float(rec.get("started_at") or 0) < SPAWN_GRACE_S:
            continue
        if not host_alive(rec):
            _clear_dead(rec)
            continue
        from daemon.session_manager import SessionState
        current = getattr(info, "_host_record", None) or {}
        if info.state == SessionState.STOPPED or not sm._is_hosted(info) \
                or current.get("host_pid") != rec.get("host_pid"):
            logger.info("Session Host %s outlived its session's close; shutting it down", sid)
            if not sht.shutdown_host_sync(rec, wait=10.0):
                ensure_host_gone(rec, wait=0.5)
