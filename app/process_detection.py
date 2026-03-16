"""
Windows process detection — find running Claude sessions, detect waiting state,
and send keystrokes to terminal windows.
"""

import json
import re
import subprocess
import time
from pathlib import Path

from .config import _sessions_dir


def _get_running_session_ids():
    """Return {session_id: pid} for any claude sessions currently running."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-command",
             "Get-WmiObject Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8
        )
        data = json.loads(result.stdout or "[]")
        if isinstance(data, dict):
            data = [data]

        running = {}
        resume_pids = []
        uuid_re = re.compile(r"(?:--resume|-r)\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)

        for proc in data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            if name not in ("node.exe", "claude.exe", "claude"):
                continue
            m = uuid_re.search(cmdline)
            if m:
                running[m.group(1)] = proc.get("ProcessId")
            elif re.search(r"\b(--resume|resume)\b", cmdline):
                # claude --resume: UUID not in command line -- resolve by recent file activity
                resume_pids.append(proc.get("ProcessId"))

        # For --resume processes, match to the most recently active .jsonl not already claimed.
        # No time limit -- --resume can resume sessions that have been idle for hours.
        if resume_pids:
            candidates = sorted(
                [(f.stat().st_mtime, f.stem) for f in _sessions_dir().glob("*.jsonl")
                 if f.stem not in running],
                reverse=True
            )
            for pid, (_, sid) in zip(resume_pids, candidates):
                running[sid] = pid

        return running
    except Exception:
        return {}


def _parse_waiting_state(path: Path) -> dict | None:
    """
    Return a dict describing what Claude is waiting on, or None if not waiting.
    Only returns a state when the LAST meaningful message is from the assistant
    (meaning Claude sent something and is now blocked waiting for the user).
    If the last meaningful message is from the user (tool results, etc.),
    Claude is processing -- not waiting.
    Dict: {question, options: list|None, kind: 'tool'|'text'}
    """
    stat = path.stat()
    now = time.time()
    idle_seconds = now - stat.st_mtime

    # If file was written less than 6 seconds ago, Claude is actively running -- not waiting
    # 6s gives Claude time to finish generating text before we flag it as a question
    if idle_seconds < 6:
        return None

    try:
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return None

    last_text = None
    last_tool_name = None
    last_tool_input = None
    last_entry_role = None   # 'user' or 'assistant'

    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("progress", "file-history-snapshot", "custom-title"):
            continue
        if t in ("user", "assistant"):
            last_entry_role = t
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "tool_result":
                        # Claude just received tool output and is processing it -- not waiting
                        return None
                    if bt == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            if len(text) > 1400:
                                last_text = "\u2026" + text[-1400:].lstrip()
                            else:
                                last_text = text
                        break
                    elif bt == "tool_use":
                        last_tool_name = block.get("name", "unknown")
                        inp = block.get("input") or {}
                        if "command" in inp:
                            last_tool_input = inp["command"][:200]
                        elif "prompt" in inp:
                            last_tool_input = inp["prompt"][:200]
                        elif "description" in inp:
                            last_tool_input = inp["description"][:200]
                        elif inp:
                            first_val = next(iter(inp.values()), "")
                            last_tool_input = str(first_val)[:200]
                        break
            elif isinstance(content, str) and content.strip():
                text = content.strip()
                if len(text) > 1400:
                    last_text = "\u2026" + text[-1400:].lstrip()
                else:
                    last_text = text
            break
        else:
            break  # unknown entry type -- stop scanning

    # Only flag as waiting if the LAST message was from the assistant
    # (Claude said/asked something and is now blocked on user input)
    if last_entry_role != "assistant":
        return None

    def _detect_options(text):
        """Return list of option strings if question has explicit choices."""
        tl = text.lower()
        if re.search(r'\[y/n/a\]|\(y/n/a\)|yes.?no.?all', tl):
            return ["y", "n", "a"]
        if re.search(r'\[y/n\]|\(y/n\)|yes.?or.?no|\[yes/no\]|\(yes/no\)', tl):
            return ["y", "n"]
        if re.search(r'\[yes\]|\[no\]', tl):
            return ["yes", "no"]
        items = re.findall(r'(?:^|\n)\s*(\d+)[.)]\s+(.+?)(?=\n\s*\d+[.)]|\n\n|$)', text, re.MULTILINE)
        if len(items) >= 2:
            return [f"{n}. {v.strip()[:60]}" for n, v in items[:6]]
        return None

    if last_text:
        opts = _detect_options(last_text)
        # Only flag as a question if the text is genuinely interrogative.
        # A plain completion message ("Got it.", "Done!", "I've saved the file.") is
        # Claude finishing a task -- it's idle, not asking anything.
        # We require EITHER: an explicit option list, OR a "?" somewhere in the text.
        if opts is None and "?" not in last_text:
            return None
        return {"question": last_text, "options": opts, "kind": "text"}

    if last_tool_name:
        tool_q = f"Allow tool: {last_tool_name}"
        if last_tool_input:
            tool_q += f"\n\n{last_tool_input}"
        return {"question": tool_q, "options": ["y", "n", "a"], "kind": "tool"}

    return None


def _parse_session_kind(path: Path) -> str:
    """
    For a running session that is NOT waiting for user input, return 'working' or 'idle'.
    working = Claude is mid-execution (tool pending, processing results, file recently written)
    idle    = Claude finished responding, ready for next user message
    """
    file_age = time.time() - path.stat().st_mtime

    # Recent file activity always means working
    if file_age < 10:
        return 'working'

    # If the file hasn't been touched in >30s and a process is running,
    # Claude is sitting idle at the prompt waiting for input -- regardless of
    # what the last entry pattern looks like (e.g. resumed sessions, finished tasks)
    if file_age > 30:
        return 'idle'

    try:
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return 'working'

    skip = {"progress", "file-history-snapshot", "custom-title", "system",
            "debug", "meta", "info", "event"}

    # Collect last few meaningful entries
    entries = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type", "") in skip:
            continue
        entries.append(obj)
        if len(entries) >= 5:
            break

    if not entries:
        return 'working'

    last = entries[0]
    t = last.get("type", "")

    if t == "user":
        content = last.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    return 'working'   # got tool result, Claude processing it
        # Last entry is a user text message -- Claude is actively responding
        return 'working'

    if t == "assistant":
        content = last.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    return 'working'   # Claude fired a tool, awaiting result

        # Last entry is assistant text (no tool_use).
        # Idle only when Claude genuinely finished answering the user:
        #   pattern = user+text -> assistant+text (direct Q&A, no tools)
        # Everything else is mid-task:
        #   tool_result before it  -> Claude just got results, writing next step
        #   assistant before it    -> Claude sent multiple messages in a row (announcing work)
        #   thinking before it     -> Claude is still reasoning
        if len(entries) > 1:
            prev = entries[1]
            pt = prev.get("type", "")
            pc = prev.get("message", {}).get("content", "")
            prev_is_user_text = (
                pt == "user" and
                isinstance(pc, list) and
                all(b.get("type") != "tool_result" for b in pc)
            ) or (pt == "user" and isinstance(pc, str))
            if not prev_is_user_text:
                return 'working'

        return 'idle'

    return 'working'


def send_to_session(pid: int, text: str) -> dict:
    """
    Send text to a running Claude session via PowerShell SendKeys.
    Returns a result dict with 'ok', 'method', and optionally 'err'.
    """
    import tempfile
    import os as _os
    import base64 as _b64

    b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
    ps_script = f"""$inputBytes = [System.Convert]::FromBase64String('{b64}')
$inputText  = [System.Text.Encoding]::UTF8.GetString($inputBytes)

function Find-Window([int]$Pid) {{
    $visited = @{{}}
    $cur = $Pid
    while ($cur -gt 4) {{
        if ($visited.ContainsKey($cur)) {{ break }}
        $visited[$cur] = 1
        try {{
            $p = Get-Process -Id $cur -EA Stop
            if ($p.MainWindowHandle -ne [IntPtr]::Zero) {{ return $p }}
        }} catch {{}}
        try {{
            $cur = [int](Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -EA Stop).ParentProcessId
        }} catch {{ break }}
    }}
    return $null
}}

$wp = Find-Window {pid}
if (-not $wp) {{ Write-Error "No window for pid {pid}"; exit 1 }}

Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class WU {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
}}
'@

[WU]::ShowWindow($wp.MainWindowHandle, 9)
[WU]::SetForegroundWindow($wp.MainWindowHandle)
Start-Sleep -Milliseconds 300

Add-Type -AssemblyName System.Windows.Forms
$specialChars = [char[]]@('+','^','%','~','(',')','{{'[0],'}}'[0],'[',']')
foreach ($ch in $inputText.ToCharArray()) {{
    $str = [string]$ch
    if ($specialChars -contains $ch) {{
        [System.Windows.Forms.SendKeys]::SendWait("{{" + $str + "}}")
    }} else {{
        [System.Windows.Forms.SendKeys]::SendWait($str)
    }}
    Start-Sleep -Milliseconds 20
}}
[System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")
"""
    tmp_ps = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
            f.write(ps_script)
            tmp_ps = f.name
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp_ps],
            capture_output=True, timeout=12
        )
        if res.returncode == 0:
            return {"ok": True, "method": "sent"}
        sk_err = res.stderr.decode("utf-8", errors="ignore").strip()[:300]
        return {"ok": False, "method": "failed", "rc": res.returncode, "err": sk_err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "timeout"}
    except Exception as e:
        return {"ok": False, "method": "error", "err": str(e)}
    finally:
        if tmp_ps:
            try:
                _os.unlink(tmp_ps)
            except Exception:
                pass


def send_to_clipboard(text: str) -> dict:
    """Fallback: copy text to clipboard when session is not running."""
    clip = text.replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-command", f"Set-Clipboard '{clip}'"],
                   capture_output=True, timeout=5)
    return {"ok": True, "method": "clipboard",
            "message": "Session not running \u2014 copied to clipboard."}
