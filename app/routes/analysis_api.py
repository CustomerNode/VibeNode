"""
Analysis routes — summary, code extraction, export, compare.
"""

import io
import zipfile

from flask import Blueprint, jsonify, send_file

from ..config import _sessions_dir
from ..sessions import load_session
from ..code_extraction import _extract_code_blocks, _block_similarity
from ..titling import smart_title, _clean_message, _is_trivial, _score, _STRIP_PREFIXES

bp = Blueprint('analysis_api', __name__)


@bp.route("/api/summary/<session_id>")
def api_summary(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(path)
    messages = session.get("messages", [])
    user_msgs  = [m for m in messages if m.get("role") == "user"      and m.get("content","").strip()]
    asst_msgs  = [m for m in messages if m.get("role") == "assistant"  and m.get("content","").strip()]

    if not user_msgs:
        return jsonify({"html": "<p>No content in this session.</p>"})

    topic = smart_title(messages)

    stop_words = {"i","the","a","an","it","is","are","was","were","to","do","can","could",
                  "would","should","please","me","my","we","our","you","this","that","have",
                  "has","had","be","will","just","so","and","or","but","if","at","in","on",
                  "of","for","with","about","from","make","let","now","there","here","then",
                  "also","get","use","into","by","up","out","its","not","no","yes","ok","add",
                  "when","how","what","why","where","which","who","still","same","each","some"}

    def _make_label(text):
        """2-3 word topic label from most meaningful words in the message."""
        text = _STRIP_PREFIXES.sub("", text).strip()
        words = [w.strip(".,!?\"'()[]{}") for w in text.split()]
        # Prefer words >3 chars that aren't stop words (more likely to be nouns/topics)
        content = [w for w in words if w.lower() not in stop_words and len(w) > 3]
        label_words = content[:3] or words[:3]
        label = " ".join(label_words)
        return label[:1].upper() + label[1:] if label else ""

    def _make_desc(user_text):
        """Use the user's own words -- naturally the right level of abstraction."""
        clean = _clean_message(user_text).strip()
        if not clean:
            return ""
        clean = clean[:1].upper() + clean[1:]
        if len(clean) <= 130:
            return clean
        return clean[:127].rsplit(" ", 1)[0] + "\u2026"

    # Collect all meaningful user messages (preserving conversation order)
    meaningful = []
    for m in messages:
        if m.get("role") != "user":
            continue
        text = _clean_message(m.get("content", ""))
        if text and not _is_trivial(text):
            meaningful.append(text)

    bullets = []
    seen_labels = set()

    if meaningful:
        # Divide into 5 sections; pick highest-scoring message from each
        num_sections = min(5, len(meaningful))
        sz = len(meaningful) / num_sections
        for sec in range(num_sections):
            section = meaningful[int(sec * sz):int((sec + 1) * sz)]
            if not section:
                continue
            best = max(section, key=_score)
            label = _make_label(best)
            desc  = _make_desc(best)
            if not label or not desc:
                continue
            key = label.lower()[:12]
            if key in seen_labels:
                continue
            seen_labels.add(key)
            bullets.append(f"<li><strong>{label}:</strong> {desc}</li>")

    # Overview = topic title + first substantive user message as 1-2 line paragraph
    overview_parts = []
    for t in meaningful[:2]:
        if len(t) > 20:
            clean = t[:1].upper() + t[1:]
            if len(clean) > 180:
                clean = clean[:177].rsplit(" ", 1)[0] + "\u2026"
            overview_parts.append(clean)
            if len(" ".join(overview_parts)) > 200:
                break
    overview_text = " \u2014 ".join(overview_parts) if overview_parts else ""

    # Recent focus = last 3 meaningful user requests
    recent_items = "".join(
        f"<li>{t[:1].upper()}{t[1:130]}{'\u2026' if len(t)>130 else ''}</li>"
        for t in meaningful[-3:]
    )
    recent_html = (f'<div class="sum-section"><div class="sum-label">Recent focus</div>'
                   f'<ul>{recent_items}</ul></div>') if recent_items else ""

    stats = (f"{len(user_msgs)} messages &nbsp;\u00b7&nbsp; {session['size']} &nbsp;\u00b7&nbsp; "
             f"Last active: {session['last_activity']}")

    bullets_html = "".join(bullets) if bullets else "<li>\u2014</li>"

    html = f"""
<div class="sum-topic">{topic}</div>
<div class="sum-stats">{stats}</div>
{"<div class='sum-section'><div class='sum-label'>Overview</div><p style='font-size:13px;color:#ccc;line-height:1.6'>" + overview_text + "</p></div>" if overview_text else ""}
<div class="sum-section">
  <div class="sum-label">Key topics covered</div>
  <ul>{bullets_html}</ul>
</div>
{recent_html}
"""
    return jsonify({"html": html})


@bp.route("/api/extract-code/<session_id>")
def api_extract_code(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    blocks = _extract_code_blocks(path)
    languages = sorted(set(b["language"] for b in blocks if b["language"]))
    return jsonify({"blocks": blocks, "count": len(blocks), "languages": languages})


@bp.route("/api/export-project/<session_id>")
def api_export_project(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(path)
    blocks = _extract_code_blocks(path)
    # Skip duplicates
    non_dup = [b for b in blocks if b["duplicate_of"] is None]
    if not non_dup:
        return jsonify({"error": "No code blocks found"})

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names: dict[str, int] = {}
        file_entries = []
        for b in non_dup:
            base = b["inferred_filename"] or f"block_{len(file_entries)+1}.txt"
            if base not in used_names:
                used_names[base] = 1
                fname = base
            else:
                used_names[base] += 1
                parts = base.rsplit(".", 1)
                if len(parts) == 2:
                    fname = f"{parts[0]}_{used_names[base]}.{parts[1]}"
                else:
                    fname = f"{base}_{used_names[base]}"
            zf.writestr(fname, b["content"])
            file_entries.append((fname, b["language"], b["msg_index"]))

        # Build README
        title = session.get("display_title", session_id)
        last_activity = session.get("last_activity", "")
        file_list = "\n".join(
            f"- {fname} \u2014 {lang or 'code'} (message {mi + 1})"
            for fname, lang, mi in file_entries
        )
        readme = (
            f"# Claude Session Export\n\n"
            f"Exported from session: {title}\n"
            f"Date: {last_activity}\n\n"
            f"## Files\n{file_list}\n"
        )
        zf.writestr("README.md", readme)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="session_export.zip"
    )


@bp.route("/api/compare/<id1>/<id2>")
def api_compare(id1, id2):
    path1 = _sessions_dir() / f"{id1}.jsonl"
    path2 = _sessions_dir() / f"{id2}.jsonl"
    if not path1.exists():
        return jsonify({"error": f"Session {id1} not found"}), 404
    if not path2.exists():
        return jsonify({"error": f"Session {id2} not found"}), 404

    s1 = load_session(path1)
    s2 = load_session(path2)
    blocks1 = _extract_code_blocks(path1)
    blocks2 = _extract_code_blocks(path2)

    def _session_meta(s):
        return {
            "title": s.get("display_title", ""),
            "date": s.get("last_activity", ""),
            "size": s.get("size", ""),
            "message_count": s.get("message_count", 0),
        }

    # Build lookup by inferred_filename for each session
    def _build_lookup(blocks):
        d = {}
        for b in blocks:
            key = b.get("inferred_filename") or b.get("language") or "unknown"
            if key not in d:
                d[key] = b
        return d

    lookup1 = _build_lookup(blocks1)
    lookup2 = _build_lookup(blocks2)

    all_keys = sorted(set(list(lookup1.keys()) + list(lookup2.keys())))
    code_diff = []
    added = removed = changed = same_count = 0

    for key in all_keys:
        b1 = lookup1.get(key)
        b2 = lookup2.get(key)
        c1 = b1["content"] if b1 else ""
        c2 = b2["content"] if b2 else ""
        lang = (b1 or b2).get("language", "")

        if b1 and not b2:
            status = "removed"
            removed += 1
        elif b2 and not b1:
            status = "added"
            added += 1
        else:
            ratio = _block_similarity(c1, c2)
            if ratio > 0.98:
                status = "same"
                same_count += 1
            else:
                status = "changed"
                changed += 1

        code_diff.append({
            "filename": key,
            "language": lang,
            "status": status,
            "content1": c1,
            "content2": c2,
        })

    return jsonify({
        "session1": _session_meta(s1),
        "session2": _session_meta(s2),
        "code_diff": code_diff,
        "stats": {
            "s1_blocks": len(blocks1),
            "s2_blocks": len(blocks2),
            "added": added,
            "removed": removed,
            "changed": changed,
        },
    })
