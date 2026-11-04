"""A stand-in for VibeNode's web server that writes real transcript files.

AI Compare and Controlled A/B Test follow sessions only through their
transcripts and the remap file, so a fake that writes those exactly as the CLI
does exercises the real following, measuring and reporting code without
launching any Claude session.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def user(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": "2026-10-31T10:00:00Z", **kw}


def assistant(text="", stop="end_turn", mid=None, model="claude-sonnet-5-5", tools=(), usage=None, **kw):
    content = [{"type": "text", "text": text}] if text else []
    for i, t in enumerate(tools):
        content.append({"type": "tool_use", "id": f"tu-{mid}-{i}", "name": t, "input": {}})
    msg = {"role": "assistant", "id": mid or str(uuid.uuid4()), "model": model, "stop_reason": stop,
           "content": content, "usage": usage or {"input_tokens": 10, "output_tokens": 5}}
    return {"type": "assistant", "message": msg, "timestamp": "2026-10-31T10:00:05Z", **kw}


def tool_result(tool_use_id, is_error=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": "x"}]}}


def write_transcript(path: Path, records, age: float = 60.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))


class FakeClient:
    """``behave(name, prompt, model, cwd, launch_id, project_dir)`` writes what
    the session does and returns the id the transcript lives under (it may
    differ from the launch id, as after a CLI id switch)."""

    def __init__(self, project_dir: Path, behave, catalog=None, applied=True, costs=None, close_ok=True):
        self.project_dir = Path(project_dir)
        self.behave = behave
        self.catalog = catalog or [{"id": "claude-opus-5-5", "name": "Opus 5.5"},
                                   {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
                                   {"id": "claude-haiku-5-5", "name": "Haiku 5.5"}]
        self.applied = applied
        self.costs = costs if costs is not None else {}
        self.launches = []
        self.close_ok = close_ok   # False: the stop is refused, as a stuck engine would
        self.closed = []           # session ids VibeNode was asked to stop

    def models(self):
        return self.catalog

    def launch(self, name, prompt, model="", cwd="", permission_mode=""):
        lid = "launch-" + uuid.uuid4().hex[:8]
        pdir = self.project_dir / (Path(cwd).name if cwd else "active")
        pdir.mkdir(parents=True, exist_ok=True)
        self.launches.append({"name": name, "prompt": prompt, "model": model, "cwd": cwd,
                              "permission_mode": permission_mode, "launch_id": lid})
        real = self.behave(name=name, prompt=prompt, model=model, cwd=cwd, launch_id=lid, project_dir=pdir) or lid
        if real != lid:
            rp = pdir / "_remapped_sessions.json"
            remaps = json.loads(rp.read_text()) if rp.exists() else {}
            remaps[lid] = {"new_id": real, "ts": 1}
            rp.write_text(json.dumps(remaps))
        self.costs.setdefault(real, round(0.01 * (len(self.costs) + 1), 4))
        applied = None
        if self.applied:
            applied = {"cwd": cwd or str(pdir), "project": "", "permission_mode": permission_mode, "model": model}
            if self.applied == "ignore_mode":
                applied["permission_mode"] = ""
            if self.applied == "ignore_cwd":
                applied["cwd"] = str(pdir)
        return {"ok": True, "launch_id": lid, "project_dir": pdir, "applied": applied}

    def close(self, sid):
        self.closed.append(sid)
        if not self.close_ok:
            return {"ok": False, "state": "working", "error": "engine did not stop it"}
        return {"ok": True, "state": "stopped", "error": ""}

    def cost(self, sid):
        return self.costs.get(sid)
