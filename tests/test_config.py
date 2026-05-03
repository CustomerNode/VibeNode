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
