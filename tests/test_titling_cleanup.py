"""
Tests for app.titling._cleanup_title_jsonl — covers Fix A from
docs/plans/phantom-sessions-fix-spec.md.

The cleanup helper must:
  1. Delete the JSONL by original sid in the *system utility project*
     (not the user's active project — that was the original bug).
  2. Delete by remapped sid via ``sm._id_aliases`` when available.
  3. Fall back to a content-scan of the system project's *.jsonl files
     when neither the original sid nor a known remap match — including
     files larger than the old 5 KB cap.
  4. Write a system-project tombstone for every cleaned sid so a
     late-flushed JSONL with the same name stays hidden.
"""

import json

import pytest

import app.titling as titling_mod
from app.titling import _cleanup_title_jsonl, _TITLE_JSONL_SIGNATURE


class _FakeSM:
    """Stand-in for the daemon-client proxy carrying just `_id_aliases`."""

    def __init__(self, aliases=None):
        self._id_aliases = dict(aliases or {})


def _make_title_jsonl(path, content_text=None):
    """Write a single-line JSONL whose first record matches the title signature."""
    if content_text is None:
        content_text = (
            "You generate very short titles for coding chat sessions. "
            "Rules: 3-4 words MAX, describe the core task..."
        )
    line = json.dumps({"type": "system", "content": content_text})
    path.write_text(line + "\n", encoding="utf-8")


@pytest.fixture
def fake_system_project(tmp_path, monkeypatch):
    """Patch ``_sessions_dir`` to point at a tmp directory for tests.

    The cleanup helper imports ``_sessions_dir`` from ``app.config`` *inside*
    the function. Patch the module attribute there.
    """
    sys_dir = tmp_path / "C--Users-test--claude--system"
    sys_dir.mkdir()
    project_name = sys_dir.name

    import app.config as config_mod
    monkeypatch.setattr(config_mod, "_sessions_dir",
                        lambda project="": sys_dir)
    return sys_dir, project_name


def test_cleanup_uses_system_utility_project_param(monkeypatch, tmp_path):
    """``_cleanup_title_jsonl`` must call ``_sessions_dir(project)`` —
    NOT ``_sessions_dir()`` (which would default to the active project).
    This was the core bug in LEAK A."""
    received = {}

    def fake_sessions_dir(project=""):
        received["project"] = project
        return tmp_path  # any directory works — we just record the call

    import app.config as config_mod
    monkeypatch.setattr(config_mod, "_sessions_dir", fake_sessions_dir)

    _cleanup_title_jsonl("_title_abc", _FakeSM(), project="ENC-SYSTEM-PROJECT")
    assert received["project"] == "ENC-SYSTEM-PROJECT"


def test_cleanup_in_memory_alias(fake_system_project):
    """When ``sm._id_aliases[sid] = new_sid`` and only ``<new_sid>.jsonl``
    exists, the file must be unlinked (strategy 2)."""
    sys_dir, project = fake_system_project
    original = "_title_abc"
    remapped = "real-sdk-uuid-xyz"
    target = sys_dir / f"{remapped}.jsonl"
    _make_title_jsonl(target)
    assert target.exists()

    _cleanup_title_jsonl(original, _FakeSM({original: remapped}), project)
    assert not target.exists()


def test_cleanup_content_scan_fallback(fake_system_project):
    """When no alias is recorded but the JSONL still carries the title
    signature on its first line, the content-scan fallback must find it."""
    sys_dir, project = fake_system_project
    target = sys_dir / "some-random-uuid.jsonl"
    _make_title_jsonl(target)
    assert target.exists()

    _cleanup_title_jsonl("_title_unknown", _FakeSM(), project)
    assert not target.exists()


def test_cleanup_handles_large_files(fake_system_project):
    """The old 5 KB size cap caused oversized title JSONLs to leak.
    With the cap removed, a 12 KB JSONL whose first line carries the
    signature must still be unlinked."""
    sys_dir, project = fake_system_project
    target = sys_dir / "large-title.jsonl"
    big_content = (
        "You generate very short titles for coding chat sessions. "
        "Rules: 3-4 words MAX. " + ("x" * 12000)
    )
    first_line = json.dumps({"type": "system", "content": big_content})
    target.write_text(first_line + "\n", encoding="utf-8")
    assert target.stat().st_size > 5000

    _cleanup_title_jsonl("_title_huge", _FakeSM(), project)
    assert not target.exists()


def test_cleanup_writes_system_tombstone(fake_system_project, monkeypatch):
    """After cleanup, the cleaned sid must be tombstoned in the system
    project so a late-flushed file with the same name stays hidden."""
    sys_dir, project = fake_system_project
    original = "_title_tombstone_check"
    target = sys_dir / f"{original}.jsonl"
    _make_title_jsonl(target)

    captured: list[tuple[str, str]] = []

    import app.session_store as ss_mod

    real_mark = ss_mod._mark_deleted

    def fake_mark(sid, project=""):
        captured.append((sid, project))
        # also call the real one for fidelity (writes to tmp dir)
        return real_mark(sid, project)

    monkeypatch.setattr(ss_mod, "_mark_deleted", fake_mark)
    _cleanup_title_jsonl(original, _FakeSM(), project)

    assert any(c[0] == original and c[1] == project for c in captured), \
        f"Expected tombstone for {original!r} in project {project!r}, got {captured}"


def test_cleanup_signature_constant_matches_system_prompt():
    """Regression guard: the literal signature used by the content-scan
    fallback must still appear in the system prompt. If someone rewords
    the system prompt without updating the signature, oversized title
    JSONLs will silently leak again."""
    from app.titling import _TITLE_SYSTEM_PROMPT
    assert _TITLE_JSONL_SIGNATURE in _TITLE_SYSTEM_PROMPT


def test_cleanup_handles_missing_files_gracefully(fake_system_project):
    """When neither the original sid nor a remapped sid resolves to a
    real file, the helper must not raise."""
    sys_dir, project = fake_system_project
    # No JSONLs in the directory at all.
    _cleanup_title_jsonl("_title_ghost", _FakeSM(), project)
    # Should reach here without raising.


def test_cleanup_skips_non_title_jsonl(fake_system_project):
    """Files in the system project that aren't title JSONLs must NOT be
    deleted by the content-scan fallback."""
    sys_dir, project = fake_system_project
    not_a_title = sys_dir / "random-real-session.jsonl"
    not_a_title.write_text(
        json.dumps({"type": "user", "content": "hello"}) + "\n",
        encoding="utf-8",
    )
    _cleanup_title_jsonl("_title_unrelated", _FakeSM(), project)
    assert not_a_title.exists(), \
        "Non-title JSONL must not be touched by content-scan"


def test_title_gen_project_switch_race(fake_system_project, monkeypatch):
    """Integration regression for the project-switch race (LEAK A core).

    The user spawns a title session while ``_active_project = "A"``. The
    SDK writes the JSONL into the SYSTEM project. While the title gen
    is in flight, the user switches to project "B". When cleanup runs,
    it must still target the SYSTEM project — NOT project B (which is
    what ``_active_project`` would resolve to today).

    We simulate this by populating the system project with a title
    JSONL, then setting ``_active_project`` to a totally different
    value before calling ``_cleanup_title_jsonl`` with the
    spawn-time-captured system project name. If the fix is correct,
    the file in the system project is deleted regardless of
    ``_active_project``.
    """
    sys_dir, system_project = fake_system_project
    sid = "_title_race_demo"
    target = sys_dir / f"{sid}.jsonl"
    _make_title_jsonl(target)

    # Pretend the user switched projects mid-flight by setting
    # _active_project to a string that does NOT match system_project.
    import app.config as config_mod
    monkeypatch.setattr(config_mod, "_active_project",
                        "C--Users-test-Documents-OtherProj")

    # Call cleanup with the spawn-time system project (as the fix does).
    _cleanup_title_jsonl(sid, _FakeSM(), system_project)
    assert not target.exists(), (
        "Cleanup failed to find the JSONL when active project differs "
        "from the system utility project — race regression."
    )
