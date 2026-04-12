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
