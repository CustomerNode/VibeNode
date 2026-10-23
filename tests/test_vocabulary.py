"""One vocabulary in every user-facing string (added 2026-10-23).

README's vocabulary paragraph: the task board is the **Workflow**, the view
for spawned sessions is **Subsessions** (a **composition** of **sections**),
the background process is the **Session Engine**, and a session that is not
running is **Sleeping**.  The app used to say "Kanban board", "VibeNode Engine
Stopped", "safe in the session daemon", "Asleep · Wakes when you send a
message" and "Add to Board" for the same things.

This scans every line of static/js/*.js and templates/*.html for the retired
forms.  Code identifiers, CSS classes, data-view values, localStorage keys and
URLs keep their old names (``kanban``, ``daemon``), so the patterns are the
user-facing phrases only.  A comment line (``//``, ``*``, ``/*``, ``<!--``) may
mention them; anything else needs an entry in ``ALLOW`` with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "static" / "js").glob("*.js")) + sorted((ROOT / "templates").glob("*.html"))

BANNED = {
    "Kanban board": re.compile(r"Kanban board"),
    "VibeNode Engine": re.compile(r"VibeNode Engine", re.I),
    "session daemon": re.compile(r"session daemon", re.I),
    "Asleep ·": re.compile(r"Asleep\s*(·|\\u00b7|&middot;)"),
    "'Asleep' label": re.compile(r"""['"`>]Asleep['"`<]"""),
    "Add to Board": re.compile(r"Add to Board", re.I),
    "Kanban label": re.compile(r">\s*Kanban\s*<"),
}

# (file name, substring of the line, reason).  Keep this short.
ALLOW: list[tuple[str, str, str]] = []

_COMMENT = ("//", "*", "/*", "<!--")


def _hits():
    out = []
    for f in FILES:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for label, rx in BANNED.items():
                if not rx.search(line):
                    continue
                if line.strip().startswith(_COMMENT):
                    continue
                if any(f.name == af and sub in line for af, sub, _ in ALLOW):
                    continue
                out.append(f"{f.relative_to(ROOT).as_posix()}:{n}: {label}: {line.strip()[:140]}")
    return out


def test_files_are_scanned():
    names = {f.name for f in FILES}
    assert {"kanban.js", "healthchecks.js", "live-panel.js", "index.html"} <= names


def test_no_retired_user_facing_terms():
    hits = _hits()
    assert not hits, "retired vocabulary in user-facing strings:\n" + "\n".join(hits)


@pytest.mark.parametrize("phrase,where", [
    ("'Session Engine stopped'", "healthchecks.js"),
    ("safe in the Session Engine", "healthchecks.js"),
    ("asleep: 'Sleeping'", "live-panel.js"),
    ("Welcome to your Workflow", "kanban.js"),
    ("<span>New task</span>", "kanban.js"),
    ("'No subsessions yet'", "toolbar.js"),
])
def test_the_new_forms_are_in_place(phrase, where):
    src = (ROOT / "static" / "js" / where).read_text(encoding="utf-8")
    assert phrase in src, f"{phrase} missing from {where}"


def test_the_scan_catches_a_planted_form(tmp_path):
    """The patterns match the strings they replaced (guards against a regex
    that silently matches nothing)."""
    old = [
        "label: 'VibeNode Engine Stopped',",
        "'Welcome to your Kanban board'",
        "likely safe in the session daemon",
        "const _ST_LABELS = {asleep: 'Asleep'};",
        "<span>Add to Board</span>",
        "'<div class=\"kanban-sidebar-label\">Kanban</div>'",
    ]
    for line in old:
        assert any(rx.search(line) for rx in BANNED.values()), line
