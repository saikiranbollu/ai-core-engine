"""Tests for the IncrementalIngestionManager module.

Covers file-change detection (add/modify/delete), git-commit tracking,
state persistence, state reset, and delta reporting.  Git subprocess calls
are mocked; file-system operations use tmp dirs.
"""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Incremental.incremental_ingestion import (
    ChangeType,
    DeltaReport,
    FileChangeRecord,
    IncrementalIngestionManager,
    IncrementalState,
    _StatePersistence,
    _file_content_hash,
    _file_mtime_iso,
    _git_changed_files,
    _git_current_commit,
)


# ========================================================================
# Helper – create a small file tree
# ========================================================================

def _create_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Create a fake repo directory with optional files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel_path, content in (files or {}).items():
        p = repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return repo


# ========================================================================
# ChangeType
# ========================================================================

class TestChangeType:
    def test_values(self):
        assert ChangeType.ADDED.value == "added"
        assert ChangeType.MODIFIED.value == "modified"
        assert ChangeType.DELETED.value == "deleted"


# ========================================================================
# FileChangeRecord
# ========================================================================

class TestFileChangeRecord:
    def test_to_dict_serialises_change_type(self):
        rec = FileChangeRecord(file_path="a.c", change_type=ChangeType.ADDED)
        d = rec.to_dict()
        assert d["change_type"] == "added"
        assert d["file_path"] == "a.c"


# ========================================================================
# DeltaReport
# ========================================================================

class TestDeltaReport:
    def test_empty_report_has_no_changes(self):
        r = DeltaReport()
        assert r.total_changed == 0
        assert not r.has_changes

    def test_counts(self):
        r = DeltaReport(added=3, modified=2, deleted=1, unchanged=10)
        assert r.total_changed == 6
        assert r.has_changes

    def test_summary_without_commits(self):
        r = DeltaReport(added=1, modified=0, deleted=0)
        s = r.summary()
        assert "1 change(s)" in s

    def test_summary_with_commits(self):
        r = DeltaReport(
            from_commit="aabbccddee112233",
            to_commit="11223344556677ee",
            added=1,
        )
        s = r.summary()
        assert "aabbccdd" in s
        assert "11223344" in s

    def test_to_dict(self):
        r = DeltaReport(added=1, changes=[
            FileChangeRecord(file_path="x.c", change_type=ChangeType.ADDED),
        ])
        d = r.to_dict()
        assert d["has_changes"] is True
        assert d["total_changed"] == 1
        assert d["changes"][0]["change_type"] == "added"


# ========================================================================
# IncrementalState
# ========================================================================

class TestIncrementalState:
    def test_round_trip(self):
        state = IncrementalState(
            last_commit_hash="abc123",
            last_run_timestamp="2026-01-01T00:00:00+00:00",
            file_snapshots={"a.c": {"content_hash": "h1", "mtime_iso": "t1", "size_bytes": 100}},
        )
        d = state.to_dict()
        state2 = IncrementalState.from_dict(d)
        assert state2.last_commit_hash == "abc123"
        assert "a.c" in state2.file_snapshots

    def test_from_dict_defaults(self):
        state = IncrementalState.from_dict({})
        assert state.last_commit_hash is None
        assert state.file_snapshots == {}


# ========================================================================
# File hashing / mtime
# ========================================================================

class TestFileHelpers:
    def test_file_content_hash(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        h = _file_content_hash(f)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert h == expected

    def test_file_content_hash_missing(self, tmp_path: Path):
        assert _file_content_hash(tmp_path / "nope.txt") == ""

    def test_file_mtime_iso(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("x", encoding="utf-8")
        mtime = _file_mtime_iso(f)
        assert "T" in mtime  # ISO-8601

    def test_file_mtime_iso_missing(self, tmp_path: Path):
        assert _file_mtime_iso(tmp_path / "nope.txt") == ""


# ========================================================================
# Git helpers (mocked)
# ========================================================================

class TestGitHelpers:
    @patch("IngestionPipeline.Incremental.incremental_ingestion.subprocess.run")
    def test_git_current_commit_success(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n")
        result = _git_current_commit(tmp_path)
        assert result == "abc123def456"

    @patch("IngestionPipeline.Incremental.incremental_ingestion.subprocess.run")
    def test_git_current_commit_not_a_repo(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        result = _git_current_commit(tmp_path)
        assert result is None

    @patch("IngestionPipeline.Incremental.incremental_ingestion.subprocess.run")
    def test_git_current_commit_exception(self, mock_run, tmp_path: Path):
        mock_run.side_effect = FileNotFoundError("git not found")
        result = _git_current_commit(tmp_path)
        assert result is None

    @patch("IngestionPipeline.Incremental.incremental_ingestion.subprocess.run")
    def test_git_changed_files_success(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="A\tnew_file.c\nM\tchanged.h\nD\tremoved.c\n",
        )
        results = _git_changed_files(tmp_path, "aaa", "bbb")
        assert len(results) == 3
        assert results[0] == {"status": "A", "path": "new_file.c"}
        assert results[1] == {"status": "M", "path": "changed.h"}
        assert results[2] == {"status": "D", "path": "removed.c"}

    @patch("IngestionPipeline.Incremental.incremental_ingestion.subprocess.run")
    def test_git_changed_files_failure(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=1, stderr="fatal: bad ref")
        results = _git_changed_files(tmp_path, "aaa", "bbb")
        assert results == []


# ========================================================================
# State persistence
# ========================================================================

class TestStatePersistence:
    def test_save_and_load(self, tmp_path: Path):
        p = tmp_path / "state.json"
        persistence = _StatePersistence(p)
        state = IncrementalState(
            last_commit_hash="abc",
            file_snapshots={"f.c": {"content_hash": "h", "mtime_iso": "t", "size_bytes": 1}},
        )
        persistence.save(state)
        loaded = persistence.load()
        assert loaded.last_commit_hash == "abc"
        assert "f.c" in loaded.file_snapshots

    def test_load_missing(self, tmp_path: Path):
        p = tmp_path / "nope.json"
        state = _StatePersistence(p).load()
        assert state.last_commit_hash is None

    def test_load_corrupt(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{{{bad json", encoding="utf-8")
        state = _StatePersistence(p).load()
        assert state.last_commit_hash is None


# ========================================================================
# IncrementalIngestionManager – detect_changes
# ========================================================================

class TestIncrementalManager:
    """Integration-like tests using real temp files."""

    def _make_manager(self, repo: Path, tmp_path: Path, **kwargs) -> IncrementalIngestionManager:
        return IncrementalIngestionManager(
            repo_path=repo,
            state_path=tmp_path / "state.json",
            include_patterns=["*.c", "*.h", "*.md"],
            **kwargs,
        )

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_first_run_all_added(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "int main(){}", "inc/lib.h": "#pragma once"})
        mgr = self._make_manager(repo, tmp_path)
        report = mgr.detect_changes()

        assert report.added == 2
        assert report.modified == 0
        assert report.deleted == 0
        assert report.has_changes
        assert report.total_files_scanned == 2

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_no_changes_after_commit(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "int main(){}"})
        mgr = self._make_manager(repo, tmp_path)

        # First run
        report1 = mgr.detect_changes()
        assert report1.added == 1
        mgr.commit_state()

        # Second run (nothing changed)
        report2 = mgr.detect_changes()
        assert report2.added == 0
        assert report2.modified == 0
        assert report2.deleted == 0
        assert report2.unchanged == 1
        assert not report2.has_changes

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_modified_file_detected(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)

        mgr.detect_changes()
        mgr.commit_state()

        # Modify file
        (repo / "src" / "main.c").write_text("v2", encoding="utf-8")
        report = mgr.detect_changes()
        assert report.modified == 1
        changes = [c for c in report.changes if c.change_type == ChangeType.MODIFIED]
        assert len(changes) == 1

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_deleted_file_detected(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "hello"})
        mgr = self._make_manager(repo, tmp_path)

        mgr.detect_changes()
        mgr.commit_state()

        # Delete file
        (repo / "src" / "main.c").unlink()
        report = mgr.detect_changes()
        assert report.deleted == 1

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_new_file_added_after_commit(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)

        mgr.detect_changes()
        mgr.commit_state()

        # Add new file
        (repo / "src" / "util.c").write_text("util", encoding="utf-8")
        report = mgr.detect_changes()
        assert report.added == 1
        assert report.unchanged == 1

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_exclude_dirs(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {
            "src/main.c": "v1",
            "build/output.c": "v1",
        })
        mgr = self._make_manager(repo, tmp_path, exclude_dirs={"build"})
        report = mgr.detect_changes()
        assert report.total_files_scanned == 1  # build/ excluded

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_include_patterns_filter(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {
            "src/main.c": "c code",
            "src/notes.txt": "text file",
        })
        mgr = self._make_manager(repo, tmp_path)
        report = mgr.detect_changes()
        # .txt not in include patterns
        assert report.total_files_scanned == 1

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_commit_state_without_detect_is_noop(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)
        mgr.commit_state()  # Should not crash
        assert mgr.last_commit_hash is None

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_reset_state(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)
        mgr.detect_changes()
        mgr.commit_state()
        assert mgr.get_state_summary()["tracked_files_count"] == 1

        mgr.reset_state()
        assert mgr.get_state_summary()["tracked_files_count"] == 0
        assert mgr.last_commit_hash is None

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value="abc123")
    def test_git_commit_tracking(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)
        report = mgr.detect_changes()
        mgr.commit_state()
        assert mgr.last_commit_hash == "abc123"
        assert report.to_commit == "abc123"

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_get_state_summary(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        mgr = self._make_manager(repo, tmp_path)
        summary = mgr.get_state_summary()
        assert "tracked_files_count" in summary
        assert "state_file" in summary

    @patch("IngestionPipeline.Incremental.incremental_ingestion._git_current_commit", return_value=None)
    def test_state_persists_across_instances(self, _mock_git, tmp_path: Path):
        repo = _create_repo(tmp_path, {"src/main.c": "v1"})
        state_path = tmp_path / "state.json"

        mgr1 = IncrementalIngestionManager(repo, state_path, include_patterns=["*.c"])
        mgr1.detect_changes()
        mgr1.commit_state()

        mgr2 = IncrementalIngestionManager(repo, state_path, include_patterns=["*.c"])
        report = mgr2.detect_changes()
        assert report.unchanged == 1
        assert report.added == 0
