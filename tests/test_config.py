"""Tests for app.config — paths, caches, and utility functions."""


class TestFormatSize:

    def test_bytes(self):
        from app.config import _format_size
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        from app.config import _format_size
        result = _format_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        from app.config import _format_size
        result = _format_size(2 * 1024 * 1024)
        assert "MB" in result

    def test_zero(self):
        from app.config import _format_size
        assert _format_size(0) == "0 B"


class TestNameOperations:

    def _patch_names_file(self, monkeypatch, path_fn):
        """Patch _names_file in both session_store (canonical) and config (re-export)."""
        from app import config, session_store
        monkeypatch.setattr(session_store, "_names_file", path_fn)
        monkeypatch.setattr(config, "_names_file", path_fn)

    def test_load_names_missing_file(self, tmp_path, monkeypatch):
        from app import config
        self._patch_names_file(monkeypatch, lambda project="": tmp_path / "nonexistent.json")
        result = config._load_names()
        assert result == {}

    def test_save_and_load_name(self, tmp_path, monkeypatch):
        from app import config
        names_path = tmp_path / "_session_names.json"
        self._patch_names_file(monkeypatch, lambda project="": names_path)
        config._save_name("sess_001", "Test Name")
        names = config._load_names()
        assert names["sess_001"] == "Test Name"

    def test_delete_name(self, tmp_path, monkeypatch):
        from app import config
        names_path = tmp_path / "_session_names.json"
        self._patch_names_file(monkeypatch, lambda project="": names_path)
        config._save_name("sess_001", "Test Name")
        config._delete_name("sess_001")
        names = config._load_names()
        assert "sess_001" not in names

    def test_delete_nonexistent_name_is_safe(self, tmp_path, monkeypatch):
        from app import config
        names_path = tmp_path / "_session_names.json"
        self._patch_names_file(monkeypatch, lambda project="": names_path)
        config._delete_name("does_not_exist")  # should not raise


class TestKanbanConfigDefaults:
    """Regression tests for kanban config defaults — new keys must always appear."""

    def test_wrong_session_detection_default_is_true(self):
        """wrong_session_detection must default to True (added 2026-04-19)."""
        from app.config import _kanban_config_defaults
        defaults = _kanban_config_defaults()
        assert "wrong_session_detection" in defaults
        assert defaults["wrong_session_detection"] is True

    def test_get_kanban_config_includes_wrong_session_detection(self, tmp_path, monkeypatch):
        """get_kanban_config() fills missing keys from defaults, including wrong_session_detection."""
        import json
        from app import config

        cfg_file = tmp_path / "kanban_config.json"
        cfg_file.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
        monkeypatch.setattr(config, "_KANBAN_CONFIG_FILE", cfg_file)
        # Invalidate cache
        config._kanban_config_cache = None

        result = config.get_kanban_config()
        assert result["wrong_session_detection"] is True

    def test_get_kanban_config_respects_explicit_false(self, tmp_path, monkeypatch):
        """User can disable wrong_session_detection by setting it to false."""
        import json
        from app import config

        cfg_file = tmp_path / "kanban_config.json"
        cfg_file.write_text(json.dumps({"wrong_session_detection": False}), encoding="utf-8")
        monkeypatch.setattr(config, "_KANBAN_CONFIG_FILE", cfg_file)
        config._kanban_config_cache = None

        result = config.get_kanban_config()
        assert result["wrong_session_detection"] is False


# ---------------------------------------------------------------------------
# Project ID alias resolution
# ---------------------------------------------------------------------------
#
# resolve_project_alias() lets two users on different machines (different
# absolute paths -> different encoded project_ids) share a Supabase board by
# remapping the local id to a remote one. The mapping lives in
# kanban_config.json under "project_id_aliases". Only kanban routes go
# through this remap; sessions / git / file APIs use the raw local id.

class TestProjectAliasResolution:

    def _set_aliases(self, tmp_path, monkeypatch, aliases):
        """Helper: write a kanban_config.json with the given aliases dict and
        wire up the cache invalidation so the next call sees them."""
        import json
        from app import config
        cfg_file = tmp_path / "kanban_config.json"
        cfg_file.write_text(json.dumps({"project_id_aliases": aliases}), encoding="utf-8")
        monkeypatch.setattr(config, "_KANBAN_CONFIG_FILE", cfg_file)
        config._kanban_config_cache = None
        return config

    def test_returns_input_unchanged_when_no_aliases_configured(self, tmp_path, monkeypatch):
        """No aliases dict at all -> identity. This is the dominant path
        (single-machine users), so it must not regress."""
        config = self._set_aliases(tmp_path, monkeypatch, {})
        assert config.resolve_project_alias("-home-me-VibeNode") == "-home-me-VibeNode"

    def test_returns_aliased_value_when_local_id_matches(self, tmp_path, monkeypatch):
        """Adopted alias: local id is replaced with the remote id everywhere
        the kanban routes go through resolve_project_alias()."""
        local = "-home-me-VibeNode"
        remote = "C--Users-other-Documents-VibeNode"
        config = self._set_aliases(tmp_path, monkeypatch, {local: remote})
        assert config.resolve_project_alias(local) == remote

    def test_unrelated_input_falls_through_unchanged(self, tmp_path, monkeypatch):
        """An alias for project A must not affect lookups for project B."""
        config = self._set_aliases(
            tmp_path, monkeypatch,
            {"-home-me-VibeNode": "C--Users-other-Documents-VibeNode"},
        )
        assert config.resolve_project_alias("-home-me-OtherProject") == "-home-me-OtherProject"

    def test_empty_input_returns_empty(self, tmp_path, monkeypatch):
        """Empty string in -> empty string out. No KeyError, no surprise."""
        config = self._set_aliases(tmp_path, monkeypatch, {"foo": "bar"})
        assert config.resolve_project_alias("") == ""

    def test_none_input_returns_none(self, tmp_path, monkeypatch):
        """None in -> None out (callers may pass get_active_project() before
        a project is set)."""
        config = self._set_aliases(tmp_path, monkeypatch, {"foo": "bar"})
        assert config.resolve_project_alias(None) is None

    def test_aliases_dict_can_be_missing_entirely(self, tmp_path, monkeypatch):
        """Old configs from before this feature won't have the key — must
        still resolve cleanly without throwing."""
        import json
        from app import config
        cfg_file = tmp_path / "kanban_config.json"
        cfg_file.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
        monkeypatch.setattr(config, "_KANBAN_CONFIG_FILE", cfg_file)
        config._kanban_config_cache = None
        assert config.resolve_project_alias("-home-me-X") == "-home-me-X"


# ---------------------------------------------------------------------------
# ai-title-only orphan detector
# ---------------------------------------------------------------------------
#
# _is_aititle_only_orphan() detects stub .jsonl files left behind when the
# CLI auto-titles a session but no conversation lands.  It is called from
# the per-request all_sessions() filter AND from the startup-time
# _cleanup_aititle_orphans() sweep.  Originally it only handled the single-
# line ai-title case (≤500 B); extended 2026-05-18 to also catch the
# multi-line case where the CLI attaches file-history-snapshot records
# during session bootstrap (largest observed in the wild: 73 KB).

class TestAiTitleOnlyOrphan:

    def test_single_ai_title_line_is_orphan(self, tmp_path):
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "stub.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo","sessionId":"x"}\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is True

    def test_ai_title_plus_file_history_snapshot_is_orphan(self, tmp_path):
        """Regression for the 73KB stub case — multi-line file where every
        non-title record is a file-history-snapshot must still be orphan.
        Without this fix the file survived both the all_sessions() filter
        and the startup sweep."""
        import json
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "stub-with-snapshots.jsonl"
        lines = [json.dumps({"type": "ai-title", "aiTitle": "foo"})]
        for i in range(20):
            lines.append(json.dumps({
                "type": "file-history-snapshot",
                "messageId": f"m{i}",
                "snapshot": {"trackedFileBackups": {}},
            }))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert p.stat().st_size > 500, "test setup must exceed the old 500B cap"
        assert _is_aititle_only_orphan(p) is True

    def test_user_turn_is_not_orphan(self, tmp_path):
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "real.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo"}\n'
            '{"type":"user","message":{"content":"hi"}}\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False

    def test_assistant_turn_is_not_orphan(self, tmp_path):
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "real.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo"}\n'
            '{"type":"assistant","message":{"content":"hi"}}\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False

    def test_summary_record_is_not_orphan(self, tmp_path):
        """Compacted sessions start with a summary record — must not be
        wiped by the orphan sweep."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "compacted.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo"}\n'
            '{"type":"summary","summary":"compact summary"}\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False

    def test_large_file_not_scanned(self, tmp_path):
        """Files >100KB return False without scanning — perf guard for the
        per-request all_sessions() filter on real sidebar contents."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "huge.jsonl"
        # Pure ai-title content above the 100KB cap. Would otherwise pass
        # the type check; the size guard must short-circuit first.
        big_line = '{"type":"ai-title","aiTitle":"foo"}'
        p.write_text((big_line + "\n") * 5000, encoding="utf-8")
        assert p.stat().st_size > 100_000
        assert _is_aititle_only_orphan(p) is False

    def test_empty_file_is_not_orphan(self, tmp_path):
        """Empty file is not specifically an ai-title stub — leave alone."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        assert _is_aititle_only_orphan(p) is False

    def test_snapshot_only_without_ai_title_is_not_orphan(self, tmp_path):
        """File-history-snapshot only (no ai-title) → leave alone.  The
        detector specifically targets the title-eager leak path, not any
        file without messages."""
        import json
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "snapshot-only.jsonl"
        p.write_text(
            json.dumps({"type": "file-history-snapshot"}) + "\n",
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False

    def test_malformed_line_is_not_orphan(self, tmp_path):
        """Malformed JSON line → be conservative, don't auto-delete."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "broken.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo"}\n'
            'not valid json{{{\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False

    def test_missing_file_is_not_orphan(self, tmp_path):
        """Nonexistent path → False, no exception leaks to caller."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "ghost.jsonl"
        assert _is_aititle_only_orphan(p) is False

    def test_tool_use_is_not_orphan(self, tmp_path):
        """tool_use records mean a conversation happened — not orphan."""
        from app.config import _is_aititle_only_orphan
        p = tmp_path / "tooluse.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"foo"}\n'
            '{"type":"tool_use","name":"Read"}\n',
            encoding="utf-8",
        )
        assert _is_aititle_only_orphan(p) is False
