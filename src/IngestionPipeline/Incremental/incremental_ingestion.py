"""
Incremental Ingestion – Only process changed files.

Features
--------
* Git commit hash tracking (uses ``git`` CLI – no Python git library needed)
* File modification timestamp tracking
* Changed-file detection (added / modified / deleted)
* Delta reporting with human-readable summaries
* Persistent state (JSON file-based, no external DB required)

This module is **standalone** and does not depend on any package in the
``packages/`` workspace (graphrag_core, mcp_toolkit, …).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..config import get_max_workers

logger = logging.getLogger("aice.ingestion.incremental")


# ---------------------------------------------------------------------------
# Change types
# ---------------------------------------------------------------------------

class ChangeType(str, Enum):
    """Type of file change detected between two ingestion runs."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FileChangeRecord:
    """Represents a single file change between two ingestion snapshots."""
    file_path: str
    change_type: ChangeType
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    old_mtime: Optional[str] = None
    new_mtime: Optional[str] = None
    size_bytes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["change_type"] = self.change_type.value
        return d


@dataclass
class DeltaReport:
    """Summary report of changes detected in an incremental ingestion run."""
    from_commit: Optional[str] = None
    to_commit: Optional[str] = None
    from_timestamp: Optional[str] = None
    to_timestamp: Optional[str] = None
    total_files_scanned: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    unchanged: int = 0
    changes: List[FileChangeRecord] = field(default_factory=list)

    @property
    def total_changed(self) -> int:
        return self.added + self.modified + self.deleted

    @property
    def has_changes(self) -> bool:
        return self.total_changed > 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["changes"] = [c.to_dict() for c in self.changes]
        d["total_changed"] = self.total_changed
        d["has_changes"] = self.has_changes
        return d

    def summary(self) -> str:
        parts = [
            f"Delta report: {self.total_changed} change(s)",
            f"  added={self.added}, modified={self.modified}, deleted={self.deleted}, unchanged={self.unchanged}",
        ]
        if self.from_commit and self.to_commit:
            parts.append(f"  commits: {self.from_commit[:8]}..{self.to_commit[:8]}")
        return "\n".join(parts)


@dataclass
class _FileSnapshot:
    """Internal per-file state for tracking."""
    file_path: str
    content_hash: str
    mtime_iso: str
    size_bytes: int


@dataclass
class IncrementalState:
    """Persistent state for incremental ingestion.

    Stores the last-known commit hash, per-file content hashes and
    modification timestamps so that subsequent runs only process deltas.
    """
    last_commit_hash: Optional[str] = None
    last_run_timestamp: Optional[str] = None
    file_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # file_snapshots maps relative path → {"content_hash", "mtime_iso", "size_bytes"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_commit_hash": self.last_commit_hash,
            "last_run_timestamp": self.last_run_timestamp,
            "file_snapshots": self.file_snapshots,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IncrementalState":
        return cls(
            last_commit_hash=data.get("last_commit_hash"),
            last_run_timestamp=data.get("last_run_timestamp"),
            file_snapshots=data.get("file_snapshots", {}),
        )


# ---------------------------------------------------------------------------
# Git helpers (subprocess-based – no gitpython dependency)
# ---------------------------------------------------------------------------

def _git_current_commit(repo_path: Path) -> Optional[str]:
    """Return the HEAD commit hash for *repo_path*, or *None* if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git rev-parse failed for %s: %s", repo_path, exc)
    return None


def _git_changed_files(repo_path: Path, from_commit: str, to_commit: str = "HEAD") -> List[Dict[str, str]]:
    """Return files changed between two commits using ``git diff --name-status``.

    Each entry is a dict with keys ``status`` (A/M/D/R/…) and ``path``.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", from_commit, to_commit],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git diff failed: %s", result.stderr.strip())
            return []
        entries: List[Dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", maxsplit=1)
            if len(parts) == 2:
                entries.append({"status": parts[0][0], "path": parts[1]})
        return entries
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git diff failed for %s: %s", repo_path, exc)
        return []


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def _file_content_hash(path: Path, algorithm: str = "sha256") -> str:
    """Compute a hex-digest content hash for a file."""
    h = hashlib.new(algorithm)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _file_mtime_iso(path: Path) -> str:
    """Return the file modification time as an ISO-8601 string."""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class _StatePersistence:
    """JSON file-based persistence for incremental state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> IncrementalState:
        if not self._path.exists():
            return IncrementalState()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return IncrementalState.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Corrupt state file %s – starting fresh: %s", self._path, exc)
            return IncrementalState()

    def save(self, state: IncrementalState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(state.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Incremental ingestion manager
# ---------------------------------------------------------------------------

class IncrementalIngestionManager:
    """Tracks file changes and enables incremental ingestion.

    Parameters
    ----------
    repo_path:
        Root directory of the repository / source tree to monitor.
    state_path:
        Path to the JSON file storing incremental state.
    include_patterns:
        Glob patterns for files to consider (e.g. ``["*.c", "*.h", "*.json"]``).
        If empty, **all** files are considered.
    exclude_dirs:
        Directory names to skip during scanning.

    Usage
    -----
    >>> mgr = IncrementalIngestionManager("/path/to/repo")
    >>> report = mgr.detect_changes()
    >>> if report.has_changes:
    ...     for change in report.changes:
    ...         process(change)
    ...     mgr.commit_state()
    """

    DEFAULT_INCLUDE_PATTERNS: List[str] = [
        "*.c", "*.h", "*.json", "*.rst", "*.md", "*.puml", "*.xlsx", "*.pdf",
    ]
    DEFAULT_EXCLUDE_DIRS: Set[str] = {
        ".git", ".svn", "__pycache__", "node_modules", ".vscode",
        "build", "output", "bin", ".archive",
    }

    def __init__(
        self,
        repo_path: str | Path,
        state_path: str | Path = ".aice_incremental_state.json",
        include_patterns: Optional[List[str]] = None,
        exclude_dirs: Optional[Set[str]] = None,
    ) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._persistence = _StatePersistence(state_path)
        self._state = self._persistence.load()
        self._include_patterns = include_patterns or list(self.DEFAULT_INCLUDE_PATTERNS)
        self._exclude_dirs = exclude_dirs if exclude_dirs is not None else set(self.DEFAULT_EXCLUDE_DIRS)
        self._max_workers = get_max_workers("incremental")

        # Pending state that gets committed after successful processing
        self._pending_state: Optional[IncrementalState] = None

    @property
    def last_commit_hash(self) -> Optional[str]:
        return self._state.last_commit_hash

    @property
    def last_run_timestamp(self) -> Optional[str]:
        return self._state.last_run_timestamp

    # -- Change detection --------------------------------------------------

    def detect_changes(self) -> DeltaReport:
        """Scan the repository and return a :class:`DeltaReport` of changes.

        This method uses a **two-pronged** strategy:

        1. **Git-aware** – if the repo is a git repository and we have a
           previous commit hash, use ``git diff`` to get the list of changed
           files quickly.
        2. **Fallback / supplement** – always walk the file system to verify
           content hashes and timestamps, catching any drift that git alone
           might not reflect (e.g. unstaged changes, generated files).

        A pending state snapshot is prepared internally.  Call
        :meth:`commit_state` after successfully processing the changes to
        persist the new baseline.
        """
        now = datetime.now(timezone.utc)
        current_commit = _git_current_commit(self._repo_path)

        report = DeltaReport(
            from_commit=self._state.last_commit_hash,
            to_commit=current_commit,
            from_timestamp=self._state.last_run_timestamp,
            to_timestamp=now.isoformat(),
        )

        # 1. Quick-path via git diff (if available)
        git_changed_paths: Set[str] = set()
        if self._state.last_commit_hash and current_commit:
            for entry in _git_changed_files(
                self._repo_path, self._state.last_commit_hash, current_commit
            ):
                git_changed_paths.add(entry["path"])

        # 2. Full file-system scan (parallel hashing)
        current_snapshots: Dict[str, Dict[str, Any]] = {}
        file_list = self._walk_files()

        def _hash_file(item: tuple[str, Path]) -> tuple[str, Dict[str, Any]]:
            rel_path, abs_path = item
            content_hash = _file_content_hash(abs_path)
            mtime = _file_mtime_iso(abs_path)
            size = abs_path.stat().st_size if abs_path.exists() else 0
            return rel_path, {
                "content_hash": content_hash,
                "mtime_iso": mtime,
                "size_bytes": size,
            }

        with ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as executor:
            for rel_path, snapshot in executor.map(_hash_file, file_list):
                current_snapshots[rel_path] = snapshot
                report.total_files_scanned += 1

        old_snapshots = self._state.file_snapshots
        all_paths = set(current_snapshots.keys()) | set(old_snapshots.keys())

        for rel_path in sorted(all_paths):
            old = old_snapshots.get(rel_path)
            new = current_snapshots.get(rel_path)

            if old is None and new is not None:
                # New file
                report.added += 1
                report.changes.append(FileChangeRecord(
                    file_path=rel_path,
                    change_type=ChangeType.ADDED,
                    new_hash=new["content_hash"],
                    new_mtime=new["mtime_iso"],
                    size_bytes=new["size_bytes"],
                ))
            elif old is not None and new is None:
                # Deleted file
                report.deleted += 1
                report.changes.append(FileChangeRecord(
                    file_path=rel_path,
                    change_type=ChangeType.DELETED,
                    old_hash=old["content_hash"],
                    old_mtime=old["mtime_iso"],
                ))
            elif old is not None and new is not None:
                # Check for modification (hash or mtime changed, OR git says changed)
                hash_changed = old["content_hash"] != new["content_hash"]
                in_git_diff = rel_path in git_changed_paths

                if hash_changed or in_git_diff:
                    report.modified += 1
                    report.changes.append(FileChangeRecord(
                        file_path=rel_path,
                        change_type=ChangeType.MODIFIED,
                        old_hash=old["content_hash"],
                        new_hash=new["content_hash"],
                        old_mtime=old["mtime_iso"],
                        new_mtime=new["mtime_iso"],
                        size_bytes=new["size_bytes"],
                    ))
                else:
                    report.unchanged += 1

        # Prepare pending state
        self._pending_state = IncrementalState(
            last_commit_hash=current_commit,
            last_run_timestamp=now.isoformat(),
            file_snapshots=current_snapshots,
        )

        logger.info(report.summary())
        return report

    def commit_state(self) -> None:
        """Persist the pending state snapshot (call after successful processing).

        If :meth:`detect_changes` has not been called or there is no pending
        state, this is a no-op.
        """
        if self._pending_state is None:
            logger.debug("No pending state to commit")
            return
        self._state = self._pending_state
        self._pending_state = None
        self._persistence.save(self._state)
        logger.info("Incremental state committed (commit=%s)", self._state.last_commit_hash)

    def reset_state(self) -> None:
        """Clear all tracked state – the next run will process everything."""
        self._state = IncrementalState()
        self._pending_state = None
        self._persistence.save(self._state)
        logger.info("Incremental state reset – next run will be a full ingestion")

    def get_state_summary(self) -> Dict[str, Any]:
        """Return a human-readable summary of current incremental state."""
        return {
            "last_commit_hash": self._state.last_commit_hash,
            "last_run_timestamp": self._state.last_run_timestamp,
            "tracked_files_count": len(self._state.file_snapshots),
            "state_file": str(self._persistence._path),
        }

    # -- File walking ------------------------------------------------------

    def _walk_files(self) -> List[tuple[str, Path]]:
        """Walk the repo and yield ``(relative_path, absolute_path)`` pairs."""
        import fnmatch as _fnmatch

        results: List[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(self._repo_path):
            # Prune excluded directories (in-place modification)
            dirnames[:] = [
                d for d in dirnames
                if d not in self._exclude_dirs
            ]
            for fname in filenames:
                if self._include_patterns:
                    if not any(_fnmatch.fnmatch(fname, pat) for pat in self._include_patterns):
                        continue
                abs_path = Path(dirpath) / fname
                rel_path = str(abs_path.relative_to(self._repo_path)).replace("\\", "/")
                results.append((rel_path, abs_path))
        return results
