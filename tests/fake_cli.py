"""A stand-in for the Claude CLI's stream-json mode, for Session Host tests.

Never the real CLI: tests launch this through a wrapper script
(``tests/_session_host_helpers.make_fake_cli``) passed as ``cli_path``.

Speaks enough of the protocol for the SDK's ``Query`` and the engine:

* ``control_request initialize`` -> success with a ``models`` list.  Every
  initialize is logged, so a test can prove the host answered later ones
  from its cache.
* a ``user`` line starts a turn: ``system init``, then by prompt text
    ``perm``      announce, ask ``can_use_tool`` for Bash, wait for the answer
    ``slow N``    N assistant chunks 0.2 s apart
    ``exit N``    exit with code N mid-turn
    anything else ``echo: <text>``
  then a ``result``.  User and assistant messages are also written to the
  transcript ``~/.claude/projects/<encoded cwd>/<sid>.jsonl`` with uuids.
* other control requests are acknowledged.
* stdin EOF exits 0, unless ``FAKE_CLI_IGNORE_EOF=1`` (a CLI that will not
  stop by itself, for the tree-kill tests).

Env: ``FAKE_CLI_LOG`` (JSON-lines event log), ``FAKE_CLI_SPAWN_CHILD=1``
(start a sleeping grandchild at launch, logged as ``child``).
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ARGS = sys.argv[1:]
LOG = os.environ.get("FAKE_CLI_LOG", "")
_out = threading.Lock()
_log_lock = threading.Lock()
_pending = {}            # request id -> [Event, response]
_req_n = [0]


def _arg(flag):
    if flag in ARGS:
        i = ARGS.index(flag)
        if i + 1 < len(ARGS):
            return ARGS[i + 1]
    return None


SID = _arg("--resume") or str(uuid.uuid4())
MODEL = _arg("--model") or "fake-model-1"
CWD = os.getcwd()


def log(event, **kw):
    if not LOG:
        return
    kw.update({"event": event, "pid": os.getpid(), "t": time.time()})
    with _log_lock:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw) + "\n")


def emit(obj):
    with _out:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _encode(cwd):
    try:  # the host puts the repo on PYTHONPATH
        from common.paths import encode_cwd
        return encode_cwd(cwd)
    except Exception:
        return "".join(c if c.isalnum() or c == "-" else "-" for c in cwd)


TRANSCRIPT = Path.home() / ".claude" / "projects" / _encode(CWD) / f"{SID}.jsonl"


def record(obj):
    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    with open(TRANSCRIPT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def say(text):
    u = str(uuid.uuid4())
    message = {"id": "msg_" + u[:8], "type": "message", "role": "assistant", "model": MODEL,
               "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 3, "output_tokens": 2}}
    record({"type": "assistant", "uuid": u, "timestamp": now_iso(), "sessionId": SID,
            "cwd": CWD, "message": message})
    emit({"type": "assistant", "uuid": u, "message": message,
          "parent_tool_use_id": None, "session_id": SID})
    log("say", text=text)


def ask_permission(tool, tool_input):
    _req_n[0] += 1
    rid = f"cli_req_{_req_n[0]}"
    ev = threading.Event()
    _pending[rid] = [ev, None]
    emit({"type": "control_request", "request_id": rid,
          "request": {"subtype": "can_use_tool", "tool_name": tool, "input": tool_input,
                      "permission_suggestions": []}})
    log("permission_asked", request_id=rid)
    ev.wait()
    resp = _pending.pop(rid)[1] or {}
    log("permission_answered", request_id=rid, response=resp)
    return resp.get("behavior") == "allow" or resp.get("allow") is True


def run_turn(text):
    emit({"type": "system", "subtype": "init", "session_id": SID, "model": MODEL,
          "cwd": CWD, "tools": [], "permissionMode": "default"})
    record({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": now_iso(),
            "sessionId": SID, "cwd": CWD, "message": {"role": "user", "content": text}})
    log("turn", text=text)
    if text.startswith("perm"):
        say("about to run Bash")
        ok = ask_permission("Bash", {"command": "echo hi"})
        say("permission granted" if ok else "permission denied")
    elif text.startswith("slow"):
        n = int((text.split() + ["5"])[1])
        for i in range(n):
            say(f"chunk {i}")
            time.sleep(0.2)
    elif text.startswith("exit"):
        code = int((text.split() + ["3"])[1])
        log("exit", code=code)
        sys.stdout.flush()
        os._exit(code)
    else:
        say(f"echo: {text}")
    emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5,
          "duration_api_ms": 4, "num_turns": 1, "session_id": SID, "total_cost_usd": 0.0,
          "usage": {"input_tokens": 3, "output_tokens": 2}, "result": "done"})
    log("result")


def handle_control(msg):
    rid = msg.get("request_id")
    sub = (msg.get("request") or {}).get("subtype")
    log("control_request", subtype=sub, request_id=rid)
    if sub == "initialize":
        body = {"commands": [], "models": [{"value": "opus", "resolvedModel": MODEL,
                                            "displayName": "Fake"}]}
    elif sub == "get_settings":
        body = {"applied": {"model": MODEL, "effort": None}}
    else:
        body = {}
    emit({"type": "control_response",
          "response": {"subtype": "success", "request_id": rid, "response": body}})


def main():
    log("start", argv=ARGS, sid=SID, cwd=CWD)
    if os.environ.get("FAKE_CLI_SPAWN_CHILD") == "1":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        log("child", child_pid=child.pid)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        t = msg.get("type")
        if t == "control_request":
            handle_control(msg)
        elif t == "control_response":
            resp = msg.get("response") or {}
            slot = _pending.get(resp.get("request_id"))
            if slot is not None:
                slot[1] = resp.get("response") or {}
                slot[0].set()
        elif t == "user":
            content = (msg.get("message") or {}).get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            threading.Thread(target=run_turn, args=(text,), daemon=True).start()
    log("stdin_eof")
    if os.environ.get("FAKE_CLI_IGNORE_EOF") == "1":
        while True:
            time.sleep(1)
    # Let a running turn finish its output before exiting, as the CLI does.
    time.sleep(0.2)
    sys.exit(0)


if __name__ == "__main__":
    main()
