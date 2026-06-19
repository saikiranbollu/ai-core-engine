"""Unit tests for src/_common/path_safety.py (W-05 / F-CA-I01, F-CE-O01)."""
import os

import pytest

from src._common.path_safety import (
    reject_traversal,
    safe_path_under,
    validate_extension,
    validate_session_id,
)


class TestValidateSessionId:
    def test_valid(self):
        assert validate_session_id("sandbox_abc-123") == "sandbox_abc-123"

    def test_rejects_traversal(self):
        with pytest.raises(ValueError):
            validate_session_id("../etc/passwd")

    def test_rejects_separator(self):
        with pytest.raises(ValueError):
            validate_session_id("a/b")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError):
            validate_session_id("a\\b")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_session_id("")


class TestSafePathUnder:
    def test_path_under_root(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("x")
        assert safe_path_under(str(f), [tmp_path]) == f.resolve()

    def test_path_outside_roots_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside_marker.txt"
        with pytest.raises(ValueError, match="not under allowed"):
            safe_path_under(str(outside), [tmp_path / "sub"])

    def test_requires_at_least_one_root(self, tmp_path):
        with pytest.raises(ValueError):
            safe_path_under(str(tmp_path), [])

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink creation typically needs privilege on Windows"
    )
    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            safe_path_under(str(link), [tmp_path])


class TestRejectTraversal:
    def test_returns_resolved(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert reject_traversal(str(f)) == f.resolve()

    def test_rejects_parent_traversal(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside.txt").write_text("x")
        escaping = root / ".." / "outside.txt"
        with pytest.raises(ValueError, match="parent-directory traversal"):
            reject_traversal(str(escaping))


class TestValidateExtension:
    def test_allowed_case_insensitive(self):
        assert validate_extension("page.PNG", {".png"}).name == "page.PNG"

    def test_rejected(self):
        with pytest.raises(ValueError):
            validate_extension("evil.exe", {".png", ".jpg"})
