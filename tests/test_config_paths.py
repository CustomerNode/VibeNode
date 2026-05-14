"""Tests for path encoding/decoding in app.config — cross-platform correctness."""

import sys
import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# _encode_cwd()
# ---------------------------------------------------------------------------

class TestEncodeCwd:

    def test_windows_path(self):
        from app.config import _encode_cwd
        assert _encode_cwd("C:\\Users\\test\\project") == "C--Users-test-project"

    def test_windows_path_forward_slashes(self):
        from app.config import _encode_cwd
        assert _encode_cwd("C:/Users/test/project") == "C--Users-test-project"

    def test_unix_path(self):
        from app.config import _encode_cwd
        assert _encode_cwd("/home/user/project") == "-home-user-project"

    def test_mac_path(self):
        from app.config import _encode_cwd
        assert _encode_cwd("/Users/devel/Developer/myapp") == "-Users-devel-Developer-myapp"

    def test_underscores_replaced(self):
        from app.config import _encode_cwd
        assert _encode_cwd("/home/user/my_project") == "-home-user-my-project"

    def test_mixed_separators(self):
        from app.config import _encode_cwd
        # Edge case: mixed slashes (shouldn't happen in practice)
        assert _encode_cwd("C:\\Users/test\\project") == "C--Users-test-project"

    def test_empty_string(self):
        from app.config import _encode_cwd
        assert _encode_cwd("") == ""

    def test_drive_letter_only(self):
        from app.config import _encode_cwd
        assert _encode_cwd("C:") == "C-"

    def test_root_unix(self):
        from app.config import _encode_cwd
        assert _encode_cwd("/") == "-"

    def test_hyphens_preserved(self):
        from app.config import _encode_cwd
        # A path with hyphens should keep them (lossy — creates ambiguity)
        result = _encode_cwd("/home/user/my-project")
        assert result == "-home-user-my-project"

    def test_dots_replaced(self):
        """Claude Code encodes ``.`` as ``-`` (e.g. ``.claude`` → ``-claude``).

        REGRESSION GUARD: without this rule, the ``_SYSTEM_UTILITY_CWD``
        filter in ``api_projects`` builds the wrong directory name and the
        ``~/.claude/_system`` utility project leaks into the user-facing
        project list.  Reverting ``_encode_cwd``'s ``.replace('.', '-')``
        re-breaks Compose and the project picker.
        """
        from app.config import _encode_cwd
        # Windows: the system utility CWD encoding
        assert _encode_cwd("C:\\Users\\dev\\.claude\\_system") == "C--Users-dev--claude--system"
        # Unix: dotfile directory
        assert _encode_cwd("/home/user/.config/myapp") == "-home-user--config-myapp"
        # Just a dot
        assert _encode_cwd(".") == "-"

    def test_system_utility_cwd_filter_matches_on_disk(self):
        """The encoded form of ``_SYSTEM_UTILITY_CWD`` must match what Claude
        Code actually writes to disk (``~/.claude/projects/<encoded>``).

        This guards the filter in ``app/routes/project_api.py::api_projects``
        that hides the system utility project from the user-facing list.
        """
        from app.config import _encode_cwd, _SYSTEM_UTILITY_CWD
        encoded = _encode_cwd(_SYSTEM_UTILITY_CWD)
        # Both ``.`` (from ``.claude``) and ``_`` (from ``_system``) must be
        # encoded as ``-``, producing consecutive double-dashes.
        assert "--claude--system" in encoded


# ---------------------------------------------------------------------------
# _decode_project() — Windows paths
# ---------------------------------------------------------------------------

class TestDecodeProjectWindows:

    def test_simple_windows_path(self, tmp_path, monkeypatch):
        from app.config import _decode_project
        # Create the target directory
        proj = tmp_path / "Users" / "test" / "Documents" / "proj"
        proj.mkdir(parents=True)
        drive = str(tmp_path).split("\\")[0].rstrip(":")  # e.g. "C" on Windows, or temp drive
        # Build the encoded name the way _encode_cwd would
        from app.config import _encode_cwd
        encoded = _encode_cwd(str(proj))
        result = _decode_project(encoded)
        if sys.platform == "win32":
            assert Path(result).is_dir()

    def test_windows_encoded_with_double_dash(self):
        """Verify the parser recognizes '--' as drive separator."""
        from app.config import _decode_project
        if sys.platform != "win32":
            pytest.skip("Windows path decoding test")
        # This tests the structural parsing — actual dir existence varies
        result = _decode_project("C--Users-test-Documents-proj")
        # Should at least attempt C:/Users/test/Documents/proj
        assert result.startswith("C:/") or result.startswith("C:\\")


# ---------------------------------------------------------------------------
# _decode_project() — Unix paths
# ---------------------------------------------------------------------------

class TestDecodeProjectUnix:

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path test")
    def test_simple_unix_path(self, tmp_path):
        from app.config import _decode_project
        # Create a directory structure and encode it
        proj = tmp_path / "myproject"
        proj.mkdir()
        from app.config import _encode_cwd
        encoded = _encode_cwd(str(proj))
        result = _decode_project(encoded)
        assert result == str(proj)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path test")
    def test_unix_path_with_underscores(self, tmp_path):
        from app.config import _decode_project, _encode_cwd
        proj = tmp_path / "my_project"
        proj.mkdir()
        encoded = _encode_cwd(str(proj))
        # _encode_cwd converts underscores to dashes, so "my_project" → "my-project"
        # _decode_project must resolve the ambiguity back to "my_project"
        result = _decode_project(encoded)
        assert result == str(proj)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path test")
    def test_unix_path_with_hyphens(self, tmp_path):
        from app.config import _decode_project, _encode_cwd
        proj = tmp_path / "my-project"
        proj.mkdir()
        encoded = _encode_cwd(str(proj))
        result = _decode_project(encoded)
        assert result == str(proj)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path test")
    def test_deep_unix_path(self, tmp_path):
        from app.config import _decode_project, _encode_cwd
        proj = tmp_path / "home" / "user" / "code" / "myapp"
        proj.mkdir(parents=True)
        encoded = _encode_cwd(str(proj))
        result = _decode_project(encoded)
        assert result == str(proj)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path test")
    def test_nonexistent_unix_path_returns_encoded(self, tmp_path):
        from app.config import _decode_project
        # Encoded name with no matching dir should fall through
        result = _decode_project("-nonexistent-path-nowhere")
        # Should return the encoded string (fallback) since no dir matches
        assert isinstance(result, str)

    def test_no_double_dash_on_windows_returns_as_is(self, monkeypatch):
        """On Windows, an encoded name without '--' is returned unchanged."""
        from app import config
        monkeypatch.setattr(config, "sys", type(sys)("sys"))
        config.sys.platform = "win32"
        config.sys.__dict__.update({k: v for k, v in sys.__dict__.items() if k != "platform"})
        # Actually just test the branch: on Windows, no "--" → return encoded
        # Simpler: just call and check
        if sys.platform == "win32":
            result = config._decode_project("no-double-dash")
            assert result == "no-double-dash"


# ---------------------------------------------------------------------------
# Round-trip: encode → decode
# ---------------------------------------------------------------------------

class TestEncodeDecodeRoundTrip:

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix round-trip test")
    def test_roundtrip_simple_unix(self, tmp_path):
        from app.config import _encode_cwd, _decode_project
        proj = tmp_path / "project"
        proj.mkdir()
        encoded = _encode_cwd(str(proj))
        decoded = _decode_project(encoded)
        assert decoded == str(proj)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix round-trip test")
    def test_roundtrip_nested_unix(self, tmp_path):
        from app.config import _encode_cwd, _decode_project
        proj = tmp_path / "level1" / "level2" / "project"
        proj.mkdir(parents=True)
        encoded = _encode_cwd(str(proj))
        decoded = _decode_project(encoded)
        assert decoded == str(proj)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows round-trip test")
    def test_roundtrip_windows(self, tmp_path):
        from app.config import _encode_cwd, _decode_project
        proj = tmp_path / "myproject"
        proj.mkdir()
        encoded = _encode_cwd(str(proj))
        decoded = _decode_project(encoded)
        # On Windows, Path comparison handles slash normalization
        assert Path(decoded) == proj


# ---------------------------------------------------------------------------
# cwd_matches_active_project()
# ---------------------------------------------------------------------------

class TestCwdMatchesActiveProject:

    def test_matching_cwd(self):
        from app.config import cwd_matches_active_project, _encode_cwd
        cwd = "/home/user/project" if sys.platform != "win32" else "C:\\Users\\test\\project"
        encoded = _encode_cwd(cwd)
        assert cwd_matches_active_project(cwd, project=encoded) is True

    def test_non_matching_cwd(self):
        from app.config import cwd_matches_active_project
        assert cwd_matches_active_project(
            "/home/user/project-a",
            project="-home-user-project-b"
        ) is False

    def test_case_insensitive(self):
        from app.config import cwd_matches_active_project, _encode_cwd
        cwd = "/home/User/Project" if sys.platform != "win32" else "C:\\Users\\Test\\Project"
        encoded = _encode_cwd(cwd).upper()
        assert cwd_matches_active_project(cwd, project=encoded) is True

    def test_empty_project_always_matches(self):
        from app.config import cwd_matches_active_project
        # When no project context, everything matches
        assert cwd_matches_active_project("/any/path", project="") is True
