"""
Windows process detection -- find running Claude sessions, detect waiting state,
and send keystrokes to terminal windows.
"""

import json
import re
import subprocess
import time
from pathlib import Path

from .config import _sessions_dir


def _get_running_session_ids():
    """Return {session_id: pid} for any claude sessions currently running.

    Positive PIDs = UUID confirmed via session registry (safe to send to).
    Negative PIDs = unmatched process (display-only, not killable).
    """
    try:
        # Primary source: Claude's own session registry (~/.claude/sessions/{pid}.json)
        # Each file maps a running PID to its session ID and cwd.
        sessions_reg = Path.home() / ".claude" / "sessions"
        registry = {}  # pid -> {sessionId, cwd}
        if sessions_reg.is_dir():
            for f in sessions_reg.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    pid = int(f.stem)
                    registry[pid] = data
                except Exception:
                    continue

        # Get running claude processes (to filter out stale registry entries)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-command",
             "Get-WmiObject Win32_Process | Where-Object { $_.Name -match 'claude|node' } | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8
        )
        proc_data = json.loads(result.stdout or "[]")
        if isinstance(proc_data, dict):
            proc_data = [proc_data]

        live_pids = set()
        proc_lookup = {}  # pid -> proc info, for parent-chain walking
        for proc in proc_data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            pid = proc.get("ProcessId")
            proc_lookup[pid] = proc
            if name not in ("node.exe", "claude.exe", "claude"):
                continue
            if "--output-format" in cmdline:
                continue
            live_pids.add(pid)

        def _has_cmd_parent(start_pid):
            """Return True if any ancestor of start_pid is cmd.exe."""
            cur = start_pid
            visited = set()
            while cur and cur > 4:
                if cur in visited:
                    break
                visited.add(cur)
                p = proc_lookup.get(cur)
                if not p:
                    break
                if (p.get("Name") or "").lower() == "cmd.exe":
                    return True
                cur = int(p.get("ParentProcessId") or 0)
            return False

        running = {}
        cmd_parented = {}   # session_id -> pid, only for cmd.exe-rooted processes
        current_dir = _sessions_dir()

        # Match via registry: authoritative PID -> session ID mapping.
        # When multiple PIDs map to the same session (e.g. external + GUI resume),
        # prefer the cmd.exe-parented one — WriteConsoleInput only works there.
        for pid, info in registry.items():
            if pid not in live_pids:
                continue  # stale registry entry
            sid = info.get("sessionId")
            if not sid:
                continue
            # Only include if session belongs to current project
            if (current_dir / f"{sid}.jsonl").exists():
                if _has_cmd_parent(pid):
                    cmd_parented[sid] = pid
                else:
                    if sid not in running:
                        running[sid] = pid  # fallback, only if no cmd.exe version yet

        # cmd.exe-parented processes win over external ones
        running.update(cmd_parented)

        # Fallback: command-line UUID matching for sessions not in registry
        uuid_re = re.compile(r"(?:--resume|-r)\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
        matched_pids = set(running.values())
        unmatched_pids = []
        for proc in proc_data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            if name not in ("node.exe", "claude.exe", "claude"):
                continue
            if "--output-format" in cmdline:
                continue
            pid = proc.get("ProcessId")
            m = uuid_re.search(cmdline)
            if m and m.group(1) not in running:
                running[m.group(1)] = pid
                matched_pids.add(pid)
            elif pid not in matched_pids:
                unmatched_pids.append(pid)

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

    # Empty/new file = Claude is at the prompt, not working
    if path.stat().st_size == 0:
        return 'idle'

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
        return 'idle'  # empty/new session — Claude is at the prompt

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

        # If file was modified in the last 3 seconds, Claude might still
        # be mid-response (JSONL writes are batched)
        if time.time() - path.stat().st_mtime < 3:
            return 'working'

        return 'idle'

    return 'working'


def send_to_session(pid: int, text: str) -> dict:
    """
    Send text to a running Claude session via WriteConsoleInput.
    Writes directly to the console input buffer — no focus stealing,
    no window activation, works reliably for repeated sends.
    """
    import tempfile
    import os as _os
    import base64 as _b64

    b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
    ps_script = f"""
$inputBytes = [System.Convert]::FromBase64String('{b64}')
$inputText  = [System.Text.Encoding]::UTF8.GetString($inputBytes)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Explicit)]
public struct KEY_EVENT_RECORD {{
    [FieldOffset(0)] public bool bKeyDown;
    [FieldOffset(4)] public short wRepeatCount;
    [FieldOffset(6)] public short wVirtualKeyCode;
    [FieldOffset(8)] public short wVirtualScanCode;
    [FieldOffset(10)] public char UnicodeChar;
    [FieldOffset(12)] public int dwControlKeyState;
}}

[StructLayout(LayoutKind.Explicit)]
public struct INPUT_RECORD {{
    [FieldOffset(0)] public short EventType;
    [FieldOffset(4)] public KEY_EVENT_RECORD KeyEvent;
}}

public class ConsoleIO {{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool FreeConsole();

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AttachConsole(uint dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool WriteConsoleInput(
        IntPtr hConsoleInput,
        INPUT_RECORD[] lpBuffer,
        uint nLength,
        out uint lpNumberOfEventsWritten);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AllocConsole();

    public static void SendString(uint pid, string text) {{
        FreeConsole();
        if (!AttachConsole(pid)) {{
            AllocConsole();
            throw new Exception("AttachConsole failed for pid " + pid + ", error " + Marshal.GetLastWin32Error());
        }}
        try {{
            IntPtr hInput = GetStdHandle(-10); // STD_INPUT_HANDLE
            foreach (char ch in text) {{
                INPUT_RECORD[] recs = new INPUT_RECORD[2];
                recs[0].EventType = 1; // KEY_EVENT
                recs[0].KeyEvent.bKeyDown = true;
                recs[0].KeyEvent.wRepeatCount = 1;
                recs[0].KeyEvent.UnicodeChar = ch;
                recs[1].EventType = 1;
                recs[1].KeyEvent.bKeyDown = false;
                recs[1].KeyEvent.wRepeatCount = 1;
                recs[1].KeyEvent.UnicodeChar = ch;
                uint written;
                WriteConsoleInput(hInput, recs, 2, out written);
            }}
            // Send Enter
            INPUT_RECORD[] enter = new INPUT_RECORD[2];
            enter[0].EventType = 1;
            enter[0].KeyEvent.bKeyDown = true;
            enter[0].KeyEvent.wRepeatCount = 1;
            enter[0].KeyEvent.wVirtualKeyCode = 0x0D;
            enter[0].KeyEvent.UnicodeChar = (char)13;
            enter[1].EventType = 1;
            enter[1].KeyEvent.bKeyDown = false;
            enter[1].KeyEvent.wRepeatCount = 1;
            enter[1].KeyEvent.wVirtualKeyCode = 0x0D;
            enter[1].KeyEvent.UnicodeChar = (char)13;
            uint w2;
            WriteConsoleInput(hInput, enter, 2, out w2);
        }} finally {{
            FreeConsole();
            AllocConsole();
        }}
    }}
}}
'@

# Find the cmd.exe parent (the console host)
$cur = {pid}
$consolePid = $null
while ($cur -gt 4) {{
    try {{
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -EA Stop
        if ($proc.Name -eq 'cmd.exe') {{ $consolePid = $cur; break }}
        $cur = [int]$proc.ParentProcessId
    }} catch {{ break }}
}}
if (-not $consolePid) {{
    throw "Session was not launched from a GUI terminal (no cmd.exe parent found). Cannot inject input directly."
}}

[ConsoleIO]::SendString([uint32]$consolePid, $inputText)
"""
    tmp_ps = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
            f.write(ps_script)
            tmp_ps = f.name
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp_ps],
            capture_output=True, timeout=15,
            startupinfo=si,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
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
