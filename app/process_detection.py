"""
Process detection -- find running Claude sessions and detect waiting state.
Cross-platform: Windows (WMI/PowerShell), macOS (ps), Linux (ps).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .platform_utils import NO_WINDOW as _NO_WINDOW

from .config import _sessions_dir


def _enumerate_claude_processes():
    """Return list of dicts with ProcessId, Name, CommandLine."""
    if sys.platform == "win32":
        return _enumerate_processes_windows()
    elif sys.platform == "darwin":
        return _enumerate_processes_macos()
    elif sys.platform == "linux":
        return _enumerate_processes_linux()
    return []


def _enumerate_processes_windows():
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-command",
             "Get-WmiObject Win32_Process "
             "| Where-Object { $_.Name -match 'claude|node' } "
             "| Select-Object ProcessId,Name,CommandLine "
             "| ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=12,
            creationflags=_NO_WINDOW)
        d = json.loads(result.stdout or "[]")
        return [d] if isinstance(d, dict) else d
    except Exception:
        return []


def _enumerate_processes_unix():
    """Use ps to enumerate claude/node processes (macOS and Linux)."""
    try:
        r = subprocess.run(["ps", "axo", "pid,comm,args"],
                           capture_output=True, text=True, timeout=10)
        procs = []
        for line in r.stdout.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            comm = os.path.basename(parts[1])
            cmdline = parts[2] if len(parts) > 2 else ""
            if not re.search(r'claude|node', comm, re.I):
                continue
            procs.append({"ProcessId": pid, "Name": comm, "CommandLine": cmdline})
        return procs
    except Exception:
        return []


def _enumerate_processes_macos():
    return _enumerate_processes_unix()


def _enumerate_processes_linux():
    return _enumerate_processes_unix()


def _tail_read_lines(path: Path, tail_bytes: int = 65536) -> list:
    """Read the last `tail_bytes` of a file, return non-empty stripped lines."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, 2)
            size = f.tell()
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # skip partial first line
            else:
                f.seek(0)
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


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

        # Get running claude/node processes -- platform-specific enumeration
        proc_data = _enumerate_claude_processes()

        live_pids = set()
        for proc in proc_data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            pid = proc.get("ProcessId")
            if name not in ("node.exe", "node", "claude.exe", "claude"):
                continue
            if "--output-format" in cmdline:
                continue
            live_pids.add(pid)

        running = {}
        current_dir = _sessions_dir()

        # Match via registry: authoritative PID -> session ID mapping.
        _registry_orphan_pids = []
        for pid, info in registry.items():
            if pid not in live_pids:
                continue
            sid = info.get("sessionId")
            if not sid:
                continue
            if (current_dir / f"{sid}.jsonl").exists():
                if sid not in running:
                    running[sid] = pid
            else:
                _registry_orphan_pids.append(pid)

        # Fallback: command-line UUID matching for sessions not in registry
        uuid_re = re.compile(r"(?:--resume|-r)\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
        matched_pids = set(running.values())
        unmatched_pids = []
        for proc in proc_data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            if name not in ("node.exe", "node", "claude.exe", "claude"):
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

        # Registry orphan matching: PIDs whose registered session ID doesn't
        # exist in the current project directory.
        if _registry_orphan_pids:
            now2 = time.time()
            orphan_candidates = []
            for f in current_dir.glob("*.jsonl"):
                try:
                    mt = f.stat().st_mtime
                    if f.stem not in running and (now2 - mt) < 300:
                        orphan_candidates.append((mt, f.stem))
                except (FileNotFoundError, OSError):
                    continue
            orphan_candidates.sort(reverse=True)
            for pid, (_, sid) in zip(_registry_orphan_pids, orphan_candidates):
                if pid not in matched_pids:
                    running[sid] = pid
                    matched_pids.add(pid)

        # Fallback: match unmatched PIDs to most recently modified .jsonl files
        try:
            if unmatched_pids:
                now = time.time()
                candidates = []
                for f in current_dir.glob("*.jsonl"):
                    try:
                        mt = f.stat().st_mtime
                        if f.stem not in running and (now - mt) < 7200:
                            candidates.append((mt, f.stem))
                    except (FileNotFoundError, OSError):
                        continue
                candidates.sort(reverse=True)
                for pid, (_, sid) in zip(unmatched_pids, candidates):
                    running[sid] = -abs(pid)  # negative = display-only
        except Exception:
            pass

        return running
    except Exception:
        try:
            return running  # type: ignore[possibly-undefined]
        except NameError:
            return {}



def _parse_waiting_state(path: Path, has_live_pid: bool = False) -> dict | None:
    """
    Return a dict describing what Claude is waiting on, or None if not waiting.
    Only returns a state when the LAST meaningful message is from the assistant
    (meaning Claude sent something and is now blocked waiting for the user).
    If the last meaningful message is from the user (tool results, etc.),
    Claude is processing -- not waiting.

    has_live_pid: True when we have a confirmed running process. When True,
    we use a slightly longer idle threshold to avoid false-positive question
    detection while Claude is actively streaming/executing.

    Dict: {question, options: list|None, kind: 'tool'|'text'}
    """
    stat = path.stat()
    now = time.time()
    idle_seconds = now - stat.st_mtime

    # If file was written very recently, Claude is actively running -- not waiting.
    # Tool permission prompts land quickly (file goes quiet almost instantly),
    # so we use a short threshold here and apply a stricter one for text-based
    # questions below (which may still be streaming).
    # When we have a confirmed live PID, use a slightly longer threshold (5s)
    # to avoid flashing — actively running sessions can have brief gaps between
    # tool_use write and progress write.
    min_idle = 5 if has_live_pid else 3
    if idle_seconds < min_idle:
        return None

    lines = _tail_read_lines(path)
    if not lines:
        return None

    last_text = None
    last_tool_name = None
    last_tool_input = None
    last_entry_role = None   # 'user' or 'assistant'
    saw_activity = False     # True if progress or queue-operation entries exist after last content entry

    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("progress", "file-history-snapshot", "custom-title",
                 "queue-operation", "system"):
            if t in ("progress", "queue-operation"):
                saw_activity = True
            continue
        if t in ("user", "assistant"):
            last_entry_role = t
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                # Scan ALL content blocks — tool_use takes priority over text.
                # Claude often sends [text "Let me run this", tool_use {Bash}].
                # We must detect the tool_use, not stop at the text block.
                _found_text = None
                _found_tool = None
                _found_tool_input = None
                for block in content:
                    bt = block.get("type", "")
                    if bt == "tool_result":
                        # Claude just received tool output and is processing it -- not waiting
                        return None
                    if bt == "text" and _found_text is None:
                        text = (block.get("text") or "").strip()
                        if text:
                            if len(text) > 1400:
                                _found_text = "\u2026" + text[-1400:].lstrip()
                            else:
                                _found_text = text
                    elif bt == "tool_use":
                        _found_tool = block.get("name", "unknown")
                        inp = block.get("input") or {}
                        if "command" in inp:
                            _found_tool_input = inp["command"][:500]
                        elif "prompt" in inp:
                            _found_tool_input = inp["prompt"][:500]
                        elif "description" in inp:
                            _found_tool_input = inp["description"][:500]
                        elif inp:
                            first_val = next(iter(inp.values()), "")
                            _found_tool_input = str(first_val)[:500]
                # tool_use wins over text (tool permission is the real question)
                if _found_tool:
                    last_tool_name = _found_tool
                    last_tool_input = _found_tool_input
                elif _found_text:
                    last_text = _found_text
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
        """Return list of option strings if question has explicit choices.

        Detects:
        1. Explicit inline choice markers (y/n, yes/no, slash-separated in brackets/parens)
        2. Numbered list menus at the END of a question — only when the list is
           the final content, items are short (<100 chars), and a "?" precedes them.
           This catches plan confirmation prompts and similar interactive menus
           without false-positiving on numbered summaries followed by questions.
        """
        tl = text.lower()
        if re.search(r'\[y/n/a\]|\(y/n/a\)|yes.?no.?all', tl):
            return ["y", "n", "a"]
        if re.search(r'\[y/n\]|\(y/n\)|yes.?or.?no|\[yes/no\]|\(yes/no\)', tl):
            return ["y", "n"]
        if re.search(r'\[yes\]|\[no\]', tl):
            return ["yes", "no"]
        # Slash-separated options in brackets/parens: [opt1/opt2/opt3] or (opt1/opt2/opt3)
        # e.g. "[yes/no/skip/all]", "(proceed/abort/modify)"
        m = re.search(r'[\[\(]([a-zA-Z][a-zA-Z ]*(?:/[a-zA-Z][a-zA-Z ]*){1,5})[\]\)]', text)
        if m:
            opts = [o.strip() for o in m.group(1).split('/')]
            if len(opts) >= 2:
                # Exclude file-path-like matches that are NOT interactive options.
                # Real options: [yes/no], [y/n/a], [proceed/abort/modify]
                # False positives: [src/utils/helpers], [app/routes/live_api]
                _path_dirs = {'src', 'lib', 'app', 'test', 'tests', 'config', 'utils',
                              'components', 'dist', 'build', 'bin', 'pkg', 'cmd',
                              'internal', 'static', 'templates', 'routes', 'models',
                              'views', 'api', 'js', 'css', 'docs', 'node_modules',
                              'public', 'assets', 'services', 'middleware', 'hooks',
                              'types', 'interfaces', 'schemas', 'migrations', 'fixtures'}
                # Known interactive action words — never reject these
                _action_words = {'yes', 'no', 'y', 'n', 'a', 'skip', 'all', 'none',
                                 'abort', 'proceed', 'cancel', 'continue', 'retry',
                                 'modify', 'overwrite', 'keep', 'replace', 'merge',
                                 'accept', 'reject', 'allow', 'deny', 'always'}
                has_action_word = any(o.lower() in _action_words for o in opts)
                _is_path = not has_action_word and (
                    any(o.lower() in _path_dirs for o in opts) or
                    any('.' in o for o in opts) or  # file extensions
                    any('_' in o for o in opts) or  # snake_case identifiers
                    (len(opts) >= 3 and all(re.match(r'^[a-z][a-z0-9]+$', o) for o in opts))
                )
                if not _is_path:
                    return opts
        # Numbered list menu at the END of the text (plan confirmations, etc.)
        # Requirements: "?" before the list, list is at the very end, items are short
        items = re.findall(r'^\s*(\d+)\.\s+(.+)$', text, re.MULTILINE)
        if items and len(items) >= 2 and len(items) <= 8:
            last_label = items[-1][1].strip()
            # Verify the list is at the end of the text (not mid-paragraph)
            if text.rstrip().endswith(last_label):
                # Verify a "?" appears before the first numbered item
                first_num_match = re.search(r'^\s*1\.', text, re.MULTILINE)
                if first_num_match and '?' in text[:first_num_match.start()]:
                    # Verify items are short (actionable choices, not paragraphs)
                    if all(len(label) < 100 for _, label in items):
                        return [f"{num}. {label.strip()}" for num, label in items]
        return None

    if last_text:
        # Text questions need longer idle time (6s) to avoid false positives during
        # streaming -- Claude may still be generating text. Tool permission prompts
        # (handled below) use the 3s threshold since the file goes quiet immediately.
        if idle_seconds < 6:
            return None
        opts = _detect_options(last_text)
        # Only flag as a question if the text is genuinely interrogative.
        # A plain completion message ("Got it.", "Done!", "I've saved the file.") is
        # Claude finishing a task -- it's idle, not asking anything.
        # We require EITHER: an explicit option list, OR a "?" near the END of the
        # text.  Checking the full text causes false positives on long explanatory
        # responses that happen to contain "?" (quoted questions, ternary operators,
        # URLs, rhetorical references, etc.).  A genuine question directed at the
        # user will have "?" in the last couple of sentences.
        tail = last_text[-300:] if len(last_text) > 300 else last_text
        # Strip code blocks before checking for "?" to avoid false positives
        # from code like: echo "What is this?" or ternary operators
        tail_no_code = re.sub(r'```[\s\S]*?```', '', tail)
        tail_no_code = re.sub(r'`[^`]+`', '', tail_no_code)
        if opts is None and "?" not in tail_no_code:
            return None
        return {"question": last_text, "options": opts, "kind": "text"}

    if last_tool_name:
        # If progress or queue-operation entries were written AFTER the tool_use,
        # the tool has already started executing (or the user already responded
        # to this prompt via the GUI) — NOT a pending permission prompt.
        if saw_activity:
            return None
        # No activity seen after tool_use = tool hasn't started executing.
        # The 3s idle threshold at the top of this function already prevents
        # false positives during streaming, and saw_activity being False means
        # no progress/queue-operation entries followed the tool_use.
        # No additional per-tool threshold needed.
        tool_q = f"Allow tool: {last_tool_name}"
        if last_tool_input:
            tool_q += f"\n\n{last_tool_input}"
        return {"question": tool_q, "options": ["y", "n", "a"], "kind": "tool"}

    return None


def _parse_session_kind(path: Path, has_live_pid: bool = False) -> str:
    """
    For a running session that is NOT waiting for user input, return 'working' or 'idle'.

    AUTHORITATIVE SIGNAL: The `stop_reason` field in the last assistant message.
    - stop_reason: 'tool_use' → Claude fired a tool, waiting for result → WORKING
    - stop_reason: 'end_turn' → Claude finished responding → IDLE
    - No stop_reason / user message last → Claude is processing → WORKING

    has_live_pid: True when we have a confirmed running process for this session.
    When True, we trust the PID over file-age heuristics for user-message-last
    sessions (Claude may be thinking for minutes without writing to the file).

    working = Claude is mid-execution (tool pending, processing results, file recently written)
    idle    = Claude finished responding, ready for next user message
    """
    st = path.stat()
    file_age = time.time() - st.st_mtime

    # Empty/new file = Claude is at the prompt, not working
    if st.st_size == 0:
        return 'idle'

    # Recent file activity always means working
    if file_age < 10:
        return 'working'

    # --- Always parse the file to determine state from the actual entries ---
    # We deliberately do NOT have a blanket "file_age > N → idle" cutoff here.
    # Long-running tools (builds, test suites, Agent subprocesses) can leave the
    # file untouched for minutes while still actively working.  The entry-level
    # signals (stop_reason, last entry type) are far more reliable than mtime.

    lines = _tail_read_lines(path)
    if not lines:
        return 'working'

    skip = {"progress", "file-history-snapshot", "custom-title", "system",
            "debug", "meta", "info", "event", "queue-operation"}

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
        # No meaningful entries found. Distinguish two cases:
        # 1. Lines exist but ALL failed JSON parsing (corrupted) → 'working' (safe default)
        # 2. Lines parsed fine but all are skip-type (metadata only) → 'idle'
        any_parsed = False
        for line in lines[-10:]:  # check last 10 lines
            try:
                json.loads(line)
                any_parsed = True
                break
            except Exception:
                continue
        return 'idle' if any_parsed else 'working'

    last = entries[0]
    t = last.get("type", "")
    msg = last.get("message", {})
    stop_reason = msg.get("stop_reason", "")

    # RULE 1: Last entry is from user → usually means Claude is processing it.
    if t == "user":
        content = msg.get("content", "")
        # Check for interrupt/cancellation signals — Claude stopped and is idle.
        if isinstance(content, str) and "interrupt" in content.lower():
            return 'idle'
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    bt = (block.get("text") or "").lower()
                    if "interrupt" in bt:
                        return 'idle'
        # tool_result → Claude is processing tool output → working if recent.
        # If the file is very old, the session likely died mid-execution.
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            if has_live_pid:
                return 'working'  # PID alive = actively processing
            return 'working' if file_age < 300 else 'idle'
        # Plain user text: Claude should be processing it.
        # When we have a confirmed live PID, trust it — Claude can think
        # for 10+ minutes without writing to the file.
        # Without PID info, use 5-minute cutoff (builds, test suites, etc.).
        if has_live_pid:
            return 'working'
        return 'working' if file_age < 300 else 'idle'

    # RULE 2: Last entry is from assistant → check stop_reason
    if t == "assistant":
        # stop_reason: 'tool_use' → Claude fired a tool, either executing or
        # awaiting approval → WORKING regardless of file age.
        # (Permission prompts are caught earlier by _parse_waiting_state.)
        if stop_reason == "tool_use":
            return 'working'

        # stop_reason: 'end_turn' → Claude finished this response.
        # Need to determine if Claude is truly idle or mid-task.
        if stop_reason == "end_turn":
            # Check if the previous entry was a plain user text (not tool_result)
            # If yes AND file is stale → truly idle
            # Otherwise → could be mid-task (more messages coming)
            if len(entries) > 1:
                prev = entries[1]
                pt = prev.get("type", "")
                pc = prev.get("message", {}).get("content", "")
                is_user_text = (
                    pt == "user" and (
                        isinstance(pc, str) or
                        (isinstance(pc, list) and all(
                            b.get("type") != "tool_result" for b in pc if isinstance(b, dict)
                        ))
                    )
                )
                if is_user_text:
                    # User asked → Claude answered with end_turn → idle
                    return 'idle' if file_age > 5 else 'working'

            # Previous entry was tool_result, another assistant msg, etc.
            # Use a moderate threshold — if Claude hasn't written anything
            # in 15s after an end_turn following tool output, it's likely done.
            return 'idle' if file_age > 15 else 'working'

        # No stop_reason yet (streaming in progress) → WORKING
        if not stop_reason:
            return 'working'

        # Any other stop_reason → use moderate threshold
        return 'idle' if file_age > 15 else 'working'

    # Any other entry type → WORKING
    return 'working'


