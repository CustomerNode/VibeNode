"""
Session loading -- fast summary parser and full session parser.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    _sessions_dir,
    _load_names,
    _load_names_cached,
    _format_size,
    _summary_cache,
)


def load_session_summary(path: Path) -> dict:
    """Fast cached summary -- seeks head + tail only, never reads entire file."""
    _err = {"id": path.stem, "error": "", "custom_title": None,
            "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
            "last_activity_ts": 0, "sort_ts": 0, "size": "0 B", "file_bytes": 0, "message_count": 0}
    try:
        st = path.stat()
    except Exception:
        return _err

    cache_key = (str(path), st.st_mtime, st.st_size)
    cached = _summary_cache.get(cache_key)
    if cached is not None:
        return cached

    custom_title = None
    first_user_content = ""
    first_ts = None
    last_ts = None
    message_count = 0
    HEAD_SIZE = 16384
    TAIL_SIZE = 8192

    try:
        file_size = st.st_size
        with open(path, "rb") as f:
            head = f.read(HEAD_SIZE)
            # Estimate message count from file size for large files
            if file_size > HEAD_SIZE + TAIL_SIZE:
                # Read tail by seeking
                f.seek(max(0, file_size - TAIL_SIZE))
                tail = f.read()
                # Estimate message count: count in head+tail, scale by file proportion
                sampled = head + tail
                sample_count = (sampled.count(b'"type":"user"') + sampled.count(b'"type":"assistant"')
                              + sampled.count(b'"type": "user"') + sampled.count(b'"type": "assistant"'))
                sample_bytes = len(sampled)
                message_count = max(sample_count, int(sample_count * file_size / sample_bytes)) if sample_bytes else 0
            else:
                tail = head[HEAD_SIZE:]  # empty if file < HEAD_SIZE
                if file_size > HEAD_SIZE:
                    f.seek(max(0, file_size - TAIL_SIZE))
                    tail = f.read()
                message_count = (head.count(b'"type":"user"') + head.count(b'"type":"assistant"')
                               + head.count(b'"type": "user"') + head.count(b'"type": "assistant"')
                               + tail.count(b'"type":"user"') + tail.count(b'"type":"assistant"')
                               + tail.count(b'"type": "user"') + tail.count(b'"type": "assistant"'))

        head_str = head.decode("utf-8", errors="replace")
        for line in head_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type", "")
            if t == "custom-title":
                custom_title = obj.get("customTitle", "")
            elif t in ("user", "assistant"):
                ts_str = obj.get("timestamp", "")
                if ts_str and first_ts is None:
                    try:
                        first_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                if t == "user" and not first_user_content:
                    msg = obj.get("message", {})
                    raw_c = msg.get("content", "")
                    if isinstance(raw_c, str):
                        first_user_content = raw_c.strip()
                    elif isinstance(raw_c, list):
                        parts = [b.get("text", "") for b in raw_c
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        first_user_content = " ".join(parts).strip()
                if first_ts and first_user_content:
                    break

        # Scan tail for last timestamp and any late custom-title
        if file_size > HEAD_SIZE:
            tail_str = tail.decode("utf-8", errors="replace")
            for line in reversed(tail_str.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type", "")
                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")
                if t in ("user", "assistant") and last_ts is None:
                    ts_str = obj.get("timestamp", "")
                    if ts_str:
                        try:
                            last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            pass
                if last_ts and custom_title is not None:
                    break

    except Exception:
        return _err

    if first_ts:
        date_str = first_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        date_str = datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

    if last_ts is None:
        last_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    last_activity_str = last_ts.strftime("%b %d, %Y  %I:%M %p")

    preview = first_user_content[:120] + ("\u2026" if len(first_user_content) > 120 else "")

    names = _load_names_cached()
    user_set_name = names.get(path.stem)
    effective_title = user_set_name or custom_title

    result = {
        "id": path.stem,
        "custom_title": effective_title,
        "display_title": effective_title if effective_title else (first_user_content[:60] + ("\u2026" if len(first_user_content) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_ts.timestamp() if last_ts else 0,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": st.st_size,
        "size": _format_size(st.st_size),
        "preview": preview,
        "message_count": message_count,
    }
    _summary_cache[cache_key] = result
    return result


def load_session(path: Path) -> dict:
    """Parse a .jsonl session file and return a summary dict."""
    messages = []
    custom_title = None
    first_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue   # skip partial/corrupt lines (e.g. mid-write)
                t = obj.get("type", "")

                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")

                elif t in ("user", "assistant"):
                    role = t
                    content = ""
                    block_type = ""
                    msg = obj.get("message", {})
                    raw = msg.get("content", "")
                    if isinstance(raw, str):
                        content = raw
                    elif isinstance(raw, list):
                        text_parts = []
                        tool_names = []
                        for block in raw:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type", "")
                            if bt == "text":
                                text_parts.append(block.get("text", ""))
                            elif bt == "tool_use":
                                tool_names.append(block.get("name", "tool"))
                            elif bt == "tool_result":
                                block_type = "tool_result"
                                tr_content = block.get("content", "")
                                if isinstance(tr_content, str) and tr_content.strip():
                                    text_parts.append(tr_content)
                                elif isinstance(tr_content, list):
                                    for sub in tr_content:
                                        if isinstance(sub, dict) and sub.get("type") == "text":
                                            text_parts.append(sub.get("text", ""))
                        content = " ".join(text_parts)
                        if not content and tool_names:
                            content = "[" + ", ".join(tool_names) + "]"
                            block_type = "tool"
                        elif tool_names and not block_type:
                            block_type = "tool"
                        # User messages with only tool_results are system output
                        if role == "user" and block_type == "tool_result":
                            block_type = "tool_result"

                    ts_str = obj.get("timestamp", "")
                    ts = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            pass

                    if ts and first_ts is None:
                        first_ts = ts

                    # Skip empty thinking-only messages
                    if content.strip():
                        msg_data = {"role": role, "content": content.strip(), "ts": ts_str}
                        if block_type:
                            msg_data["type"] = block_type
                        messages.append(msg_data)

    except Exception as e:
        return {"id": path.stem, "error": str(e), "messages": [], "custom_title": None,
                "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
                "last_activity_ts": 0, "sort_ts": 0, "size": "0 B", "file_bytes": 0, "message_count": 0}

    # Date: prefer first message timestamp, fall back to file mtime
    if first_ts:
        date_str = first_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        mtime = path.stat().st_mtime
        date_str = datetime.fromtimestamp(mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)

    first_user = next((m["content"] for m in messages if m["role"] == "user" and m["content"]), "")
    preview = first_user[:120] + ("\u2026" if len(first_user) > 120 else "")

    # Last activity: latest message timestamp or file mtime
    last_ts = None
    for m in reversed(messages):
        ts_str = m.get("ts", "")
        if ts_str:
            try:
                last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                break
            except Exception:
                pass
    if last_ts is None:
        last_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    last_activity_str = last_ts.strftime("%b %d, %Y  %I:%M %p")

    # File size (bytes of the .jsonl file only)
    file_bytes = path.stat().st_size
    if file_bytes < 1024:
        size_str = f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        size_str = f"{file_bytes / 1024:.0f} KB"
    else:
        size_str = f"{file_bytes / (1024*1024):.0f} MB"

    # User-set names in _session_names.json always win over anything in the .jsonl
    user_set_name = _load_names().get(path.stem)
    effective_title = user_set_name or custom_title

    return {
        "id": path.stem,
        "custom_title": effective_title,
        "display_title": effective_title if effective_title else (first_user[:60] + ("\u2026" if len(first_user) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_ts.timestamp() if last_ts else 0,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": file_bytes,
        "size": size_str,
        "preview": preview,
        "message_count": len(messages),
        "messages": messages,
    }


def all_sessions(summary_only: bool = False) -> list:
    files = list(_sessions_dir().glob("*.jsonl"))
    loader = load_session_summary if summary_only else load_session
    if summary_only and len(files) > 10:
        with ThreadPoolExecutor(max_workers=min(16, len(files))) as pool:
            sessions = list(pool.map(loader, files))
    else:
        sessions = [loader(f) for f in files]
    sessions.sort(key=lambda x: x["sort_ts"], reverse=True)
    return sessions
