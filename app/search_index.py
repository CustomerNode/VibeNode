"""
Full-text search index over Claude session transcripts.

Role in the system
------------------
Sessions live on disk as JSONL transcripts under
``~/.claude/projects/<encoded-project>/<session-id>.jsonl``.  Once a session
scrolls out of the sidebar, the decisions/fixes/errors inside it become
unfindable.  This module maintains a derived SQLite FTS5 index over those
transcripts so the web UI can answer:

  1. "where did we decide X"          — full-text search with BM25 ranking,
  2. "which session touched file Y"   — a touched-file lookup table,
  3. "which sessions hit this error"  — tool_result output is indexed too.

Design decisions (load-bearing)
-------------------------------
* **DB location** is ``~/.claude/gui_search_index.db`` — the same
  derived-state pattern as ``gui_kanban.db``.  It lives outside the repo, so
  nothing can ever be committed to the public repository, and the path is
  computed *lazily* (``_index_db_path()``) so the test suite's fake
  ``Path.home()`` isolation applies automatically.
* **The index is disposable.**  On any ``sqlite3.DatabaseError`` we delete
  the DB file and rebuild from the JSONLs — they are the source of truth.
* **Indexing is incremental and debounced.**  ``ensure_index()`` compares
  each JSONL's ``(mtime, size)`` against the ``indexed_files`` bookkeeping
  table and re-parses only changed files.  A per-project TTL
  (``_ENSURE_TTL``) short-circuits repeat calls so search-as-you-type never
  re-stats the directory on every keystroke.  Indexing only ever runs inside
  an ``/api/search`` request in the web-server process — never in the daemon
  and never on a per-turn hot path.
* **Deleted/utility sessions are filtered at query time**, not index time:
  tombstones change without touching the JSONL's mtime, so an index-time
  filter would keep serving a session the user just deleted.
* **JSONL parsing mirrors ``app.sessions.load_session``** — same entry
  shapes (string vs block-list content, tool_result string/list forms) and
  the same tolerance for blank or corrupt lines (mid-write files).
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .session_store import _get_deleted_ids, _get_remapped_ids, _get_utility_ids

logger = logging.getLogger("app.search_index")

# Re-check a project's JSONLs at most this often (seconds).  Search results
# may therefore lag disk reality by up to this long — acceptable for a
# history-search feature, and it keeps repeated searches from re-statting
# the whole project directory on every keystroke.
_ENSURE_TTL = 20.0

# Cap indexed tool_result text per block.  Tool output (build logs, test
# runs) can be megabytes; errors overwhelmingly appear early in the output,
# so the head is what matters for "which session hit this error".
_MAX_BLOCK_CHARS = 4000

# "Touched" means *edited* — the same semantics as the daemon's
# tracked-files scan (daemon/backends/claude_store.py::read_tracked_files).
# Read-only access (Read/Grep) is deliberately not "touched".
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Snippet delimiters.  The client converts these to <mark> AFTER
# HTML-escaping the snippet text, so raw transcript content can never
# inject markup.
_SNIP_OPEN = "[[HIT]]"
_SNIP_CLOSE = "[[/HIT]]"

# Serializes all index WRITES (schema create, reindex).  Reads don't take
# the lock — WAL mode lets searches read while another request indexes.
_index_lock = threading.Lock()

# project -> time.monotonic() of the last completed ensure_index() pass.
_last_ensure: dict = {}


def _index_db_path() -> Path:
    """Path of the search-index DB, computed lazily on every call.

    Lazy (rather than a module constant) so tests that monkeypatch
    ``Path.home()`` get an isolated DB for free, and so the module can be
    imported before any home-dir redirection happens.
    """
    return Path.home() / ".claude" / "gui_search_index.db"


def _connect() -> sqlite3.Connection:
    """Open the index DB, creating the schema on first use.

    WAL journaling lets concurrent searches read while ensure_index()
    writes.  Schema changes bump PRAGMA user_version; on mismatch we rebuild
    from scratch (the index is disposable derived state).
    """
    db_path = _index_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        version = con.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        # Corrupted file.  Close BEFORE re-raising: on Windows an open
        # handle blocks the unlink that the rebuild path needs to do.
        con.close()
        raise
    if version != 1:
        if version != 0:
            # Future/unknown schema — rebuild rather than guess.
            con.close()
            _delete_db_files()
            con = sqlite3.connect(str(db_path))
            con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS indexed_files(
                project    TEXT NOT NULL,
                session_id TEXT NOT NULL,
                mtime      REAL NOT NULL,
                size       INTEGER NOT NULL,
                PRIMARY KEY(project, session_id)
            );
            CREATE TABLE IF NOT EXISTS session_files(
                project    TEXT NOT NULL,
                session_id TEXT NOT NULL,
                file_path  TEXT NOT NULL,  -- as recorded in the tool_use input
                norm_path  TEXT NOT NULL,  -- lowercased forward-slash form for matching
                PRIMARY KEY(project, session_id, norm_path)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
                content,
                project UNINDEXED,
                session_id UNINDEXED,
                role UNINDEXED,
                ts UNINDEXED,
                msg_idx UNINDEXED,
                tokenize='unicode61'
            );
            PRAGMA user_version = 1;
            """
        )
        con.commit()
    return con


def _delete_db_files() -> None:
    """Remove the index DB and its WAL/SHM sidecars.

    Safe: the index is derived state; the JSONLs remain the source of truth
    and the next ensure_index() rebuilds everything.
    """
    base = _index_db_path()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(base) + suffix).unlink(missing_ok=True)
        except OSError:
            # A concurrent handle may briefly hold the file on Windows;
            # the rebuild will still recreate a coherent schema.
            logger.warning("Could not remove index file %s%s", base, suffix)


def _normalize_path(p: str) -> str:
    """Normalize a file path for matching: forward slashes, lowercase."""
    return p.replace("\\", "/").lower()


def _like_escape(fragment: str) -> str:
    """Escape LIKE wildcards in a user-supplied fragment (ESCAPE '\\')."""
    return (
        fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _extract_from_jsonl(path: Path):
    """Parse one session JSONL into indexable rows.

    Returns ``(messages, files)`` where ``messages`` is a list of
    ``(msg_idx, role, ts, content)`` tuples for every non-empty
    user/assistant message, and ``files`` is a ``{norm_path: original_path}``
    dict of files touched via edit tools.

    Parsing mirrors ``app.sessions.load_session``: blank/corrupt lines are
    skipped silently (the CLI may be mid-write), string and block-list
    content shapes are both handled, and thinking-only messages are dropped.
    """
    messages = []
    files: dict = {}
    msg_idx = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue  # partial/corrupt line — same tolerance as load_session
                if obj.get("type") not in ("user", "assistant"):
                    continue
                role = obj["type"]
                ts = obj.get("timestamp", "")
                raw = obj.get("message", {}).get("content", "")
                text_parts = []
                if isinstance(raw, str):
                    if raw.strip():
                        text_parts.append(raw)
                elif isinstance(raw, list):
                    for block in raw:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text":
                            text_parts.append(block.get("text", ""))
                        elif bt == "tool_use":
                            if block.get("name", "") in _EDIT_TOOLS:
                                inp = block.get("input", {}) or {}
                                fp = inp.get("file_path", "") or inp.get("path", "")
                                if fp:
                                    files[_normalize_path(fp)] = fp
                        elif bt == "tool_result":
                            tr = block.get("content", "")
                            if isinstance(tr, str) and tr.strip():
                                text_parts.append(tr[:_MAX_BLOCK_CHARS])
                            elif isinstance(tr, list):
                                for sub in tr:
                                    if isinstance(sub, dict) and sub.get("type") == "text":
                                        text_parts.append(
                                            sub.get("text", "")[:_MAX_BLOCK_CHARS]
                                        )
                content = " ".join(text_parts).strip()
                if content:
                    messages.append((msg_idx, role, ts, content))
                    msg_idx += 1
    except OSError:
        # File vanished mid-scan (deleted between stat and open) — treat as
        # empty; the next ensure pass removes its rows.
        return [], {}
    return messages, files


def ensure_index(project: str, force: bool = False) -> None:
    """Bring the index up to date for *project*, incrementally.

    Compares each on-disk JSONL's ``(mtime, size)`` against the bookkeeping
    table and re-parses only new/changed files; rows for JSONLs that no
    longer exist are removed.  Debounced by ``_ENSURE_TTL`` per project
    unless *force* is True.

    All work happens under ``_index_lock`` so concurrent search requests
    serialize their (rare) index writes instead of racing.
    """
    if not project:
        return
    with _index_lock:
        now = time.monotonic()
        if not force and now - _last_ensure.get(project, float("-inf")) < _ENSURE_TTL:
            return
        try:
            _ensure_index_locked(project)
        except sqlite3.DatabaseError:
            # Corrupted index DB — nuke and rebuild from the JSONLs.
            logger.warning(
                "Search index DB corrupted; rebuilding from transcripts", exc_info=True
            )
            _delete_db_files()
            _ensure_index_locked(project)
        _last_ensure[project] = time.monotonic()


def _ensure_index_locked(project: str) -> None:
    """Do the incremental index pass.  Caller holds ``_index_lock``."""
    proj_dir = config._CLAUDE_PROJECTS / project
    on_disk: dict = {}
    if proj_dir.is_dir():
        for f in proj_dir.glob("*.jsonl"):
            # Underscore-prefixed stems are metadata files (_session_names
            # etc.), never transcripts — same exclusion as all_sessions().
            if f.stem.startswith("_"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue  # deleted between glob and stat
            on_disk[f.stem] = (st.st_mtime, st.st_size)

    con = _connect()
    try:
        indexed = {
            sid: (mtime, size)
            for sid, mtime, size in con.execute(
                "SELECT session_id, mtime, size FROM indexed_files WHERE project = ?",
                (project,),
            )
        }
        changed = [sid for sid, sig in on_disk.items() if indexed.get(sid) != sig]
        removed = [sid for sid in indexed if sid not in on_disk]
        if not changed and not removed:
            return

        cur = con.cursor()
        cur.execute("BEGIN")
        for sid in removed + changed:
            # Deleting from the FTS table by unindexed column scans the
            # table — fine at the ~15k messages/project scale measured.
            cur.execute(
                "DELETE FROM messages WHERE project = ? AND session_id = ?",
                (project, sid),
            )
            cur.execute(
                "DELETE FROM session_files WHERE project = ? AND session_id = ?",
                (project, sid),
            )
            cur.execute(
                "DELETE FROM indexed_files WHERE project = ? AND session_id = ?",
                (project, sid),
            )
        for sid in changed:
            messages, files = _extract_from_jsonl(proj_dir / f"{sid}.jsonl")
            cur.executemany(
                "INSERT INTO messages(content, project, session_id, role, ts, msg_idx)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(c, project, sid, r, t, i) for (i, r, t, c) in messages],
            )
            cur.executemany(
                "INSERT OR IGNORE INTO session_files"
                "(project, session_id, file_path, norm_path) VALUES (?, ?, ?, ?)",
                [(project, sid, orig, norm) for norm, orig in files.items()],
            )
            mtime, size = on_disk[sid]
            cur.execute(
                "INSERT INTO indexed_files(project, session_id, mtime, size)"
                " VALUES (?, ?, ?, ?)",
                (project, sid, mtime, size),
            )
        con.commit()
        if changed or removed:
            logger.info(
                "Search index updated for %s: %d reindexed, %d removed",
                project, len(changed), len(removed),
            )
    finally:
        con.close()


def _build_match_query(q: str) -> str:
    """Convert raw user input into a safe FTS5 MATCH expression.

    Each whitespace token becomes a quoted phrase (embedded double quotes
    doubled per FTS5 string rules) so user input can never be parsed as
    FTS5 syntax.  Tokens are AND-joined; the last token gets a ``*`` prefix
    suffix so search-as-you-type matches partial words.

    Returns "" when no usable tokens remain (e.g. all-punctuation input).
    """
    tokens = q.split()
    parts = []
    for i, tok in enumerate(tokens):
        core = tok.replace('"', '""').strip("*")
        if not core.strip('"'):
            continue
        phrase = f'"{core}"'
        if i == len(tokens) - 1:
            phrase += "*"
        parts.append(phrase)
    return " ".join(parts)


def search(project: str, q: str = "", file_filter: str = "", limit: int = 20) -> dict:
    """Search *project*'s indexed transcripts, recovering from corruption.

    Thin wrapper around :func:`_search_once` that handles the one corruption
    case ``ensure_index()`` cannot see: damage localized to FTS pages that a
    no-change ensure pass (which only reads ``indexed_files``) never touches.
    Without this, every search would 500 until some JSONL's mtime changed.
    On ``sqlite3.DatabaseError`` the DB is deleted, rebuilt from the JSONLs,
    and the query retried once.
    """
    try:
        return _search_once(project, q, file_filter, limit)
    except ValueError:
        raise  # invalid query — not a corruption problem
    except sqlite3.DatabaseError:
        logger.warning(
            "Search index DB corrupted at query time; rebuilding from transcripts",
            exc_info=True,
        )
        with _index_lock:
            _delete_db_files()
        ensure_index(project, force=True)
        return _search_once(project, q, file_filter, limit)


def _search_once(project: str, q: str, file_filter: str, limit: int) -> dict:
    """Single search attempt against the current index DB.

    Args:
        project: encoded project directory name (e.g. ``C--Users-x-proj``).
        q: free-text query (FTS5 over message content, BM25-ranked).
        file_filter: substring matched against touched-file paths
            (case-insensitive, slash-insensitive).
        limit: max sessions returned (1-100).

    Returns a dict::

        {"project": ..., "query": ..., "file": ...,
         "sessions": [{"session_id", "rank", "snippets": [{role, ts, text}],
                       "files": [...]}, ...],
         "stats": {"sessions_indexed", "messages_indexed", "took_ms"}}

    Raises:
        ValueError: the query produced no valid FTS expression or FTS
            rejected it — callers should map this to HTTP 400.

    Sessions the user deleted/hid are dropped here at query time (NOT at
    index time) because tombstones change without touching JSONL mtimes.
    """
    t0 = time.perf_counter()
    excluded = (
        _get_deleted_ids(project)
        | _get_utility_ids(project)
        | _get_remapped_ids(project)
    )
    con = _connect()
    try:
        # --- touched-file lookup: session_id -> [original paths] ---
        file_sessions = None
        if file_filter:
            file_sessions = {}
            rows = con.execute(
                "SELECT session_id, file_path FROM session_files"
                " WHERE project = ? AND norm_path LIKE ? ESCAPE '\\'"
                " ORDER BY file_path",
                (project, "%" + _like_escape(_normalize_path(file_filter)) + "%"),
            )
            for sid, fp in rows:
                if sid not in excluded:
                    file_sessions.setdefault(sid, []).append(fp)

        sessions: dict = {}
        if q:
            match = _build_match_query(q)
            if not match:
                raise ValueError("invalid search query")
            try:
                # Over-fetch (limit*10 messages) so grouping by session
                # still fills `limit` sessions when one session dominates.
                rows = con.execute(
                    "SELECT session_id, role, ts,"
                    f" snippet(messages, 0, '{_SNIP_OPEN}', '{_SNIP_CLOSE}',"
                    " ' … ', 12), bm25(messages)"
                    " FROM messages WHERE messages MATCH ? AND project = ?"
                    " ORDER BY bm25(messages) LIMIT ?",
                    (match, project, max(200, limit * 10)),
                ).fetchall()
            except sqlite3.OperationalError as e:
                # Residual FTS syntax problem despite sanitization.
                raise ValueError("invalid search query") from e
            for sid, role, ts, snip, rank in rows:
                if sid in excluded:
                    continue
                if file_sessions is not None and sid not in file_sessions:
                    continue  # combined q + file: intersect
                entry = sessions.get(sid)
                if entry is None:
                    if len(sessions) >= limit:
                        continue
                    entry = sessions[sid] = {
                        "session_id": sid,
                        "rank": rank,
                        "snippets": [],
                        "files": (file_sessions or {}).get(sid, []),
                    }
                if len(entry["snippets"]) < 3:
                    entry["snippets"].append(
                        {"role": role, "ts": ts, "text": snip}
                    )
        elif file_sessions is not None:
            # File-only lookup: order by transcript recency (indexed mtime).
            order = {
                sid: mtime
                for sid, mtime in con.execute(
                    "SELECT session_id, mtime FROM indexed_files WHERE project = ?",
                    (project,),
                )
            }
            for sid in sorted(
                file_sessions, key=lambda s: order.get(s, 0), reverse=True
            )[:limit]:
                sessions[sid] = {
                    "session_id": sid,
                    "rank": 0,
                    "snippets": [],
                    "files": file_sessions[sid],
                }

        n_files, n_msgs = con.execute(
            "SELECT (SELECT COUNT(*) FROM indexed_files WHERE project = ?),"
            " (SELECT COUNT(*) FROM messages WHERE project = ?)",
            (project, project),
        ).fetchone()
    finally:
        con.close()

    return {
        "project": project,
        "query": q,
        "file": file_filter,
        "sessions": list(sessions.values()),
        "stats": {
            "sessions_indexed": n_files,
            "messages_indexed": n_msgs,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
