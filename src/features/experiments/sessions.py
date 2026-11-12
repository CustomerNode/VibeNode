"""Launch VibeNode sessions, follow them, and measure what they did.

Extracted from the original ``scripts/ab_test.py`` (A/B Test, now AI Compare),
whose launcher, id-following and completion test it keeps, so both AI Compare
and Controlled A/B Test share one implementation.

Every session is a real VibeNode session started through ``POST
/api/new-session``; nothing here talks to the CLI directly. A new session starts
under a temporary id and the CLI announces its real id a second later, writing
the transcript under THAT id; VibeNode records the switch in the project's
``_remapped_sessions.json`` (and the engine's alias file), so every poll
re-resolves the id before reading.

A session has finished when its last main-thread record is an assistant message
with ``stop_reason: end_turn`` and the transcript has been quiet for ``settle``
seconds (a Smart termination send-back lands within a second or two and starts
another turn). It has failed when it ends on the CLI's API-error record.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shared.paths import claude_projects_dir, encode_cwd
from shared.session_names import load_engine_aliases, load_remaps_at, remaps_path

BASE = "http://localhost:5050"
REPO = Path(__file__).resolve().parents[3]

# How a variant or trial ended. Only "completed" is a finished answer.
COMPLETED, FAILED, TIMEOUT, CANCELLED, LAUNCH_FAILED = (
    "completed", "failed", "timeout", "cancelled", "launch_failed")

# Tools that change files or run commands: a read-only run that used one
# successfully was not read-only, whatever was asked.
MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"}


# ── run identity ─────────────────────────────────────────────────────────────

def new_run_id(prefix: str, now: float | None = None) -> str:
    """``<prefix>-YYYYMMDD-HHMMSS-<6 hex>``: unique per run, sortable by time."""
    t = time.localtime(now if now is not None else time.time())
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', t)}-{secrets.token_hex(3)}"


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:limit].strip("-")) or "x"


def make_run_dir(root: Path, run_id: str) -> Path:
    """Create a run's own folder; refuse (never reuse) one that exists."""
    d = Path(root) / run_id
    d.mkdir(parents=True, exist_ok=False)
    return d


def variant_dirs(run_dir: Path, names: list[str]) -> dict:
    """One folder per variant, ``01-<slug>``; distinct even when names slug alike."""
    out, used = {}, set()
    for i, n in enumerate(names, 1):
        d = f"{i:02d}-{slug(n)}"
        assert d not in used
        used.add(d)
        p = Path(run_dir) / d
        p.mkdir(parents=True, exist_ok=False)
        out[n] = p
    return out


def write_json(path: Path, data) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# ── talking to VibeNode ──────────────────────────────────────────────────────

class VibeNodeClient:
    """The web server's routes the runners use. Tests pass a fake with the same
    four methods."""

    def __init__(self, base: str = BASE):
        self.base = base

    def _http(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"error": f"HTTP {e.code}"}

    def models(self) -> list:
        return self._http("GET", "/api/models")

    def active_project_dir(self) -> Path:
        for p in self._http("GET", "/api/projects"):
            if p.get("active"):
                return claude_projects_dir() / (p.get("encoded") or p.get("name"))
        raise RuntimeError("No active project in VibeNode.")

    def launch(self, name: str, prompt: str, model: str = "", cwd: str = "",
               permission_mode: str = "") -> dict:
        """Start a new session. Returns ``{ok, launch_id, project_dir, applied, error}``.

        ``applied`` is what the server says it used. A server that predates the
        ``cwd`` / ``permission_mode`` fields returns no ``applied``; callers that
        need them must refuse rather than assume."""
        body = {"name": name, "prompt": prompt}
        if model:
            body["model"] = model
        if cwd:
            body["cwd"] = cwd
        if permission_mode:
            body["permission_mode"] = permission_mode
        r = self._http("POST", "/api/new-session", body)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "launch failed"}
        applied = r.get("applied")
        if applied and applied.get("project"):
            pdir = claude_projects_dir() / applied["project"]
        elif cwd:
            pdir = claude_projects_dir() / encode_cwd(cwd)
        else:
            pdir = self.active_project_dir()
        return {"ok": True, "launch_id": r["new_id"], "project_dir": pdir, "applied": applied}

    def close(self, session_id: str) -> dict:
        """Stop the session through VibeNode's own stop (``close_session``).
        Returns ``{ok, state, error}``; ``state`` is what the engine holds after."""
        try:
            r = self._http("POST", f"/api/live/close/{session_id}", {})
        except Exception as e:
            return {"ok": False, "state": None, "error": str(e)}
        return r if isinstance(r, dict) else {"ok": False, "state": None, "error": "bad reply"}

    def notify(self, title: str, message: str) -> None:
        """One chime in every open VibeNode tab (no page to answer, so no flash)."""
        try:
            self._http("POST", "/api/notify/approval",
                       {"title": title, "message": message, "source": "vibenode-experiments"})
        except Exception:
            pass

    def cost(self, session_id: str):
        """The CLI's reported API-equivalent cost in USD, or None if not known."""
        try:
            r = self._http("GET", f"/api/live/usage/{session_id}")
        except Exception:
            return None
        c = r.get("cost_usd") if isinstance(r, dict) else None
        return float(c) if isinstance(c, (int, float)) and c > 0 else None


# ── models ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum() or ch in "[]")


def resolve_model(text: str, catalog: list) -> dict:
    """A friendly model name ("opus", "sonnet 5", "Haiku 4.5", or an id) as a
    ``{"id", "name"}`` entry of VibeNode's model list. A bare family name means
    its newest model (the list is newest first) without the 1M marker."""
    want = _norm(text)
    for m in catalog:
        if want in (_norm(m["id"]), _norm(m["name"]), _norm(m["id"]).replace("claude", "", 1)):
            return m
    for m in catalog:
        if "[1m]" not in m["id"] and _norm(m["name"]).rstrip("0123456789") == want:
            return m
    raise ValueError(f"unknown model {text!r}; choose from: " + ", ".join(m["name"] for m in catalog))


def same_model(asked: str, ran: str) -> bool:
    strip = lambda x: str(x or "").split("[", 1)[0]
    return strip(asked) == strip(ran)


# ── following a session ──────────────────────────────────────────────────────

def resolve_id(project_dir: Path, sid: str) -> str:
    """Follow launch-id -> real-id switches (chained, cycle-safe)."""
    seen = set()
    while sid not in seen:
        seen.add(sid)
        nxt = (load_remaps_at(remaps_path(project_dir)).get(sid) or {}).get("new_id") \
            or (load_engine_aliases().get(sid) or {}).get("new_id")
        if not nxt or nxt == sid:
            break
        sid = nxt
    return sid


def _records(path: Path) -> list:
    """The transcript's records. A transcript is read while the CLI appends to
    it, so the last line may be half written (cut mid-character, or mid-JSON):
    that line is skipped and read whole on the next poll. A read that fails
    outright (seen once as a MemoryError mid-write on Windows) counts as "not
    readable yet", never as a crash of the whole run."""
    try:
        data = Path(path).read_bytes()
    except (OSError, MemoryError):
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return "".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")


_NOT_HUMAN = ("Stop hook feedback:", "<", "Caveat:", "[Request interrupted", "⟦VN-")


def _is_human_turn(e: dict) -> bool:
    """A user record that is a person's message, not a tool result, a hook's
    send-back or a system note."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return False
        c = _text_of(e.get("message") or {})
    c = str(c or "").strip()
    return bool(c) and not c.startswith(_NOT_HUMAN)


def received_prompt(path: Path):
    """The first message the session actually received, as its transcript
    recorded it (``None`` when there is none yet). AI Compare compares it with
    the prompt it sent, so a prompt changed on the way is shown, not assumed."""
    for e in _records(path):
        if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                continue
            c = "".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        return c if isinstance(c, str) else None
    return None


def read_outcome(path: Path) -> dict:
    """What the transcript says right now: ``state`` is "running", "completed"
    or "failed", plus the final (or latest) reply text and the model."""
    last, last_text, model = None, "", ""
    for e in _records(path):
        if e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
            continue
        last = e
        if e.get("type") == "assistant":
            msg = e.get("message") or {}
            t = _text_of(msg)
            if t.strip():
                last_text = t
            model = msg.get("model") or model
    if not last or last.get("type") != "assistant":
        return {"state": "running", "text": last_text, "model": model}
    msg = last.get("message") or {}
    if last.get("isApiErrorMessage") or last.get("error"):
        return {"state": FAILED, "text": _text_of(msg) or last_text, "model": model,
                "error": str(last.get("error") or _text_of(msg))[:500]}
    if msg.get("stop_reason") == "end_turn":
        return {"state": COMPLETED, "text": _text_of(msg), "model": msg.get("model", model)}
    return {"state": "running", "text": last_text, "model": model}


def _transcripts(project_dir: Path, sid: str) -> list:
    """The session's own transcript and its sub-agents' (``<sid>/subagents``)."""
    main = Path(project_dir) / f"{sid}.jsonl"
    subs = sorted((Path(project_dir) / sid / "subagents").glob("*.jsonl"))
    return [main] + subs


def measure(project_dir: Path, sid: str) -> dict:
    """Measured, never estimated: model calls, tokens, tools, retries and human
    turns, from the session's transcripts. Sub-agent work is counted in the
    totals and also shown on its own. Time is wall clock, measured by the caller:
    sub-agents running in parallel are never summed into it."""
    seen = set()
    tot = Counter()
    sub = Counter()
    tools = Counter()
    retries = humans = 0
    refused = 0
    mutating_ok = []
    first_ts = last_ts = None
    for i, path in enumerate(_transcripts(project_dir, sid)):
        results = {}
        recs = _records(path)
        for e in recs:
            c = (e.get("message") or {}).get("content")
            if e.get("type") == "user" and isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_result":
                        results[x.get("tool_use_id")] = bool(x.get("is_error"))
        for e in recs:
            if i == 0 and e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                first_ts = first_ts or e.get("timestamp")
                last_ts = e.get("timestamp") or last_ts
                if _is_human_turn(e):
                    humans += 1
            if e.get("type") != "assistant":
                continue
            if e.get("isApiErrorMessage"):
                retries += 1
            msg = e.get("message") or {}
            for x in msg.get("content") or []:
                if isinstance(x, dict) and x.get("type") == "tool_use":
                    tools[x.get("name")] += 1
                    if x.get("name") in MUTATING_TOOLS:
                        if results.get(x.get("id"), False):
                            refused += 1
                        else:
                            mutating_ok.append(x.get("name"))
            mid = msg.get("id") or e.get("uuid")
            u = msg.get("usage") or {}
            if not u or mid in seen:
                continue
            seen.add(mid)
            row = Counter({"calls": 1, "input": u.get("input_tokens", 0) or 0,
                           "output": u.get("output_tokens", 0) or 0,
                           "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                           "cache_read": u.get("cache_read_input_tokens", 0) or 0})
            tot.update(row)
            if i > 0:
                sub.update(row)
    return {
        "model_calls": tot["calls"],
        "tokens": {k: tot[k] for k in ("input", "output", "cache_write", "cache_read")},
        "subagent": {"model_calls": sub["calls"], "tokens": {k: sub[k] for k in ("input", "output", "cache_write", "cache_read")}},
        "tools": dict(tools),
        "tool_calls": sum(tools.values()),
        "api_error_retries": retries,
        # The first message is the assignment; any later one is a person stepping in.
        "human_interventions": max(0, humans - 1),
        "mutating_tool_uses": mutating_ok,
        "refused_tool_uses": refused,
        "first_ts": first_ts, "last_ts": last_ts,
    }


def short_answer(text: str) -> str:
    """The card's answer (or headline) when the reply ends on a handoff card."""
    if "```handoff" in text:
        body = text.split("```handoff", 1)[1].split("```", 1)[0]
        try:
            card = json.loads(body)
            return card.get("answer") or card.get("headline") or text
        except ValueError:
            pass
    return text.strip()


def self_report(text: str) -> dict:
    """What the session SAYS about its own result. Recorded, never trusted as
    evidence: a Done card is a claim, not a verification."""
    if "```handoff" in text:
        body = text.split("```handoff", 1)[1].split("```", 1)[0]
        try:
            card = json.loads(body)
            return {"type": card.get("type"), "headline": card.get("headline", ""),
                    "claims_done": card.get("type") in ("done", "deliverable")}
        except ValueError:
            pass
    low = text.lower()
    return {"type": None, "headline": text.strip()[:200],
            "claims_done": bool(re.search(r"\b(done|completed?|all tests pass|implemented)\b", low))}


@dataclass
class Run:
    """One session being followed: a variant (AI Compare) or a trial (Controlled)."""
    key: str
    name: str
    prompt: str
    model: str = ""
    cwd: str = ""
    permission_mode: str = ""
    launch_id: str = ""
    session_id: str = ""
    id_history: list = field(default_factory=list)
    project_dir: Path | None = None
    applied: dict | None = None
    sent_at: float = 0.0
    ended_at: float = 0.0
    status: str = "pending"
    error: str = ""
    text: str = ""
    ran_model: str = ""
    termination: str = ""     # "", "confirmed" or "unconfirmed: <why>" after a cancel or timeout
    contained: dict | None = None   # set by the break-out watch when it stopped this run

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["project_dir"] = str(self.project_dir) if self.project_dir else ""
        d["seconds"] = round(self.ended_at - self.sent_at, 1) if self.ended_at and self.sent_at else None
        return d


def launch_all(client, runs: list) -> None:
    for r in runs:
        res = client.launch(r.name, r.prompt, model=r.model, cwd=r.cwd, permission_mode=r.permission_mode)
        r.sent_at = time.time()
        if not res.get("ok"):
            r.status, r.error, r.ended_at = LAUNCH_FAILED, res.get("error", ""), r.sent_at
            continue
        r.launch_id = r.session_id = res["launch_id"]
        r.id_history = [r.launch_id]
        r.project_dir = Path(res["project_dir"])
        r.applied = res.get("applied")
        r.status = "running"


# Seconds between attempts when a stop was refused or the engine still reports
# the session running.
STOP_RETRY_WAIT = 1.0


def terminate(client, run, quiet: float = 3.0, attempts: int = 3, clock=time.time, sleep=time.sleep) -> str:
    """Stop an unfinished session and confirm it stopped: VibeNode's stop must
    succeed, the engine must report it not running, and its transcript must
    stay unchanged for ``quiet`` seconds. Anything less is "unconfirmed",
    never reported as stopped."""
    if client is None or not hasattr(client, "close"):
        return "unconfirmed: no way to stop the session"
    why = ""
    for _ in range(attempts):
        # The CLI may have switched to its real id since the last poll: stop
        # and watch THAT session, not the launch id's empty file.
        if run.project_dir and run.launch_id:
            sid = resolve_id(run.project_dir, run.launch_id)
            if sid != run.session_id:
                run.session_id = sid
                run.id_history.append(sid)
        res = client.close(run.session_id or run.launch_id)
        state = res.get("state")
        if not res.get("ok"):
            why = res.get("error") or "the stop was refused"
            sleep(STOP_RETRY_WAIT)
            continue
        if state not in (None, "stopped", "idle"):
            why = f"the engine still reports it {state}"
            sleep(STOP_RETRY_WAIT)
            continue
        path = Path(run.project_dir or ".") / f"{run.session_id}.jsonl"
        try:
            before = path.stat().st_size if path.exists() else -1
        except OSError:
            before = -1
        sleep(quiet)
        try:
            after = path.stat().st_size if path.exists() else -1
        except OSError:
            after = -1
        if after == before:
            return "confirmed"
        why = "its transcript was still growing after the stop"
    return f"unconfirmed: {why}"


def wait_all(runs: list, timeout: float, settle: float = 8.0, poll: float = 1.0,
             cancel_file: Path | None = None, on_done=None, clock=time.time, sleep=time.sleep,
             client=None, stop_quiet: float = 3.0) -> None:
    """Follow every running session until it completes, fails, times out, or
    the run is cancelled (Ctrl-C, or ``cancel_file`` appearing). A session that
    times out or is cancelled is STOPPED through VibeNode (``terminate``) and
    its ``termination`` records whether that was confirmed. An unfinished
    session keeps whatever it had written so far as partial text; it is never
    reported as completed."""
    deadline = clock() + timeout
    pending = [r for r in runs if r.status == "running"]
    try:
        _follow(pending, deadline, timeout, settle, poll, cancel_file, on_done, clock, sleep)
    finally:
        for r in runs:
            if r.status in (TIMEOUT, CANCELLED) and not r.termination:
                r.termination = terminate(client, r, quiet=stop_quiet, clock=clock, sleep=sleep)


def _follow(pending, deadline, timeout, settle, poll, cancel_file, on_done, clock, sleep):
    try:
        while pending:
            if cancel_file is not None and Path(cancel_file).exists():
                raise KeyboardInterrupt
            for r in list(pending):
                if r.status != "running":   # ended from outside (the break-out watch)
                    pending.remove(r)
                    if on_done:
                        on_done(r)
                    continue
                sid = resolve_id(r.project_dir, r.launch_id)
                if sid != r.session_id:
                    r.session_id = sid
                    r.id_history.append(sid)
                path = r.project_dir / f"{sid}.jsonl"
                out = read_outcome(path)
                r.text, r.ran_model = out.get("text", ""), out.get("model", "")
                if out["state"] == "running":
                    continue
                try:
                    quiet = clock() - path.stat().st_mtime
                except OSError:
                    continue
                if quiet < settle:
                    continue  # just ended a turn; a send-back may still follow
                r.status = out["state"]
                r.error = out.get("error", "")
                r.ended_at = path.stat().st_mtime
                pending.remove(r)
                if on_done:
                    on_done(r)
            if not pending:
                break
            if clock() >= deadline:
                for r in pending:
                    r.status, r.ended_at = TIMEOUT, clock()
                    r.error = f"no final reply within {timeout:.0f}s"
                return
            sleep(poll)
    except KeyboardInterrupt:
        for r in pending:
            if r.status == "running":
                r.status, r.ended_at, r.error = CANCELLED, clock(), "cancelled before it finished"


def keep_transcripts(run: Run, dest: Path) -> list:
    """Copy the session's transcripts into the run's own folder, so the record
    survives a later trash or edit of the session."""
    out = []
    if not run.project_dir or not run.session_id:
        return out
    for i, src in enumerate(_transcripts(run.project_dir, run.session_id)):
        if src.exists():
            name = "transcript.jsonl" if i == 0 else f"subagent-{src.name}"
            shutil.copy2(src, Path(dest) / name)
            out.append(name)
    return out


# ── reporting helpers ────────────────────────────────────────────────────────

def fmt_tokens(t: dict) -> str:
    return f"{t.get('input', 0) + t.get('cache_write', 0) + t.get('cache_read', 0):,} in / {t.get('output', 0):,} out"


def fmt_cost(c) -> str:
    return f"${c:.4f}" if isinstance(c, (int, float)) else "not reported"


def md_cell(s, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s.replace("|", "\\|")
