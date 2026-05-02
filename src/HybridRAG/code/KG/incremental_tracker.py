"""Incremental ingestion tracker — content-hash based change detection.

Stores SHA-256 content hashes as ``_content_hash`` properties on tracking
nodes in Neo4j:

- ``SRC_SourceFile._content_hash``  — per C source / header file
- ``SFR_File._content_hash``        — per SFR register header
- ``TS_TestSpecDocument._content_hash`` — per TestSpec Excel workbook
- ``MCALModule._jama_hash``         — combined hash of cached Jama JSON

On re-ingestion the tracker compares local file hashes against stored
values to determine which artifacts are new / changed / deleted / unchanged.
Callers use the result to skip unchanged data and cascade-delete stale nodes
before re-ingesting only what changed.
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("aice.ingestion.incremental")

HASH_PROPERTY = "_content_hash"

# Regex to validate Neo4j label / property identifiers used in f-strings.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str) -> str:
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid Cypher identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class IncrementalPlan:
    """Result of comparing local files against stored hashes in Neo4j."""

    changed: Dict[str, str] = field(default_factory=dict)
    """file_id → new SHA-256 hash  (new or content-modified files)."""

    deleted: List[str] = field(default_factory=list)
    """file_ids present in Neo4j but absent on disk."""

    unchanged: List[str] = field(default_factory=list)
    """file_ids whose hash matches — safe to skip."""

    @property
    def has_changes(self) -> bool:
        return bool(self.changed) or bool(self.deleted)

    @property
    def is_first_run(self) -> bool:
        """No stored hashes found (empty DB or first ingestion)."""
        return not self.unchanged and not self.deleted and bool(self.changed)

    def summary(self) -> str:
        return (
            f"Incremental: {len(self.changed)} changed/new, "
            f"{len(self.deleted)} deleted, {len(self.unchanged)} unchanged"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_file(path: Path) -> str:
    """SHA-256 hex digest of *path*'s contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_files(
    root: Path,
    extensions: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
) -> Dict[str, Path]:
    """Walk *root* and return ``{relative_posix_path: absolute_path}``."""
    _exclude = exclude_dirs or {".git", "__pycache__", "node_modules", "build"}
    result: Dict[str, Path] = {}
    for dirpath, dirnames, filenames in root.resolve().walk():
        dirnames[:] = [d for d in dirnames if d not in _exclude]
        for fname in filenames:
            if extensions and Path(fname).suffix.lower() not in extensions:
                continue
            abs_path = dirpath / fname
            rel = str(abs_path.relative_to(root.resolve())).replace("\\", "/")
            result[rel] = abs_path
    return result


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class IncrementalTracker:
    """Compare local file hashes with ``_content_hash`` stored in Neo4j.

    Supports four artifact types (EA is assumed static and excluded):

    - **Source code** (.c / .h) — tracked on ``SRC_SourceFile`` nodes
    - **SFR headers** — tracked on ``SFR_File`` nodes
    - **TestSpec** (.xlsx) — tracked on ``TS_TestSpecDocument`` nodes
    - **Jama** (cached JSON) — tracked on ``MCALModule`` nodes
    """

    def __init__(self, driver: Any, module: str) -> None:
        self._driver = driver
        self._module = module.upper()

    # ── Source Code ────────────────────────────────────────────

    def plan_source(self, file_map: Dict[str, Path]) -> IncrementalPlan:
        """Hash .c/.h files and compare against ``SRC_SourceFile._content_hash``."""
        return self._plan_files(file_map, "SRC_SourceFile", "file_id")

    def cascade_delete_source(self, file_ids: List[str]) -> int:
        """Delete ``SRC_SourceFile`` nodes and everything linked via ``SRC_DEFINED_IN``."""
        if not file_ids:
            return 0
        with self._driver.session() as s:
            # 1 — delete child nodes
            s.run(
                "UNWIND $ids AS fid "
                "MATCH (n)-[:SRC_DEFINED_IN]->(:SRC_SourceFile {file_id: fid, module: $mod}) "
                "DETACH DELETE n",
                ids=file_ids, mod=self._module,
            )
            # 2 — delete the file nodes themselves
            result = s.run(
                "UNWIND $ids AS fid "
                "MATCH (f:SRC_SourceFile {file_id: fid, module: $mod}) "
                "DETACH DELETE f "
                "RETURN count(f) AS cnt",
                ids=file_ids, mod=self._module,
            )
            cnt = result.single()["cnt"]
        logger.info("  Cascade-deleted %d source files (%d ids)", cnt, len(file_ids))
        return cnt

    def stamp_source(self, file_hashes: Dict[str, str]) -> None:
        self._stamp_hashes("SRC_SourceFile", "file_id", file_hashes)

    # ── SFR ───────────────────────────────────────────────────

    def plan_sfr(self, file_map: Dict[str, Path]) -> IncrementalPlan:
        """Hash SFR headers and compare against ``SFR_File._content_hash``."""
        return self._plan_files(file_map, "SFR_File", "file_id")

    def cascade_delete_sfr(self, file_ids: List[str]) -> int:
        """Delete ``SFR_File`` and linked registers / bitfields / base addresses."""
        if not file_ids:
            return 0
        with self._driver.session() as s:
            # 1 — bitfields (children of registers from these files)
            s.run(
                "UNWIND $ids AS fid "
                "MATCH (:SFR_File {file_id: fid, module: $mod})"
                "<-[:SFR_DEFINED_IN]-(reg)-[:SFR_HAS_BITFIELD]->(bf) "
                "DETACH DELETE bf",
                ids=file_ids, mod=self._module,
            )
            # 2 — registers and base addresses
            s.run(
                "UNWIND $ids AS fid "
                "MATCH (n)-[:SFR_DEFINED_IN]->(:SFR_File {file_id: fid, module: $mod}) "
                "DETACH DELETE n",
                ids=file_ids, mod=self._module,
            )
            # 3 — the file nodes
            result = s.run(
                "UNWIND $ids AS fid "
                "MATCH (f:SFR_File {file_id: fid, module: $mod}) "
                "DETACH DELETE f "
                "RETURN count(f) AS cnt",
                ids=file_ids, mod=self._module,
            )
            cnt = result.single()["cnt"]
        logger.info("  Cascade-deleted %d SFR files (%d ids)", cnt, len(file_ids))
        return cnt

    def stamp_sfr(self, file_hashes: Dict[str, str]) -> None:
        self._stamp_hashes("SFR_File", "file_id", file_hashes)

    # ── TestSpec ──────────────────────────────────────────────

    def plan_testspec(self, xlsx_path: Path) -> IncrementalPlan:
        """Check whether the TestSpec Excel has changed."""
        doc_name = xlsx_path.stem
        current_hash = _hash_file(xlsx_path)
        stored = self._get_stored_hashes("TS_TestSpecDocument", "document_name")
        if doc_name in stored and stored[doc_name] == current_hash:
            return IncrementalPlan(unchanged=[doc_name])
        return IncrementalPlan(changed={doc_name: current_hash})

    def delete_testspec(self) -> int:
        """Delete all ``TS_*`` nodes for this module."""
        with self._driver.session() as s:
            result = s.run(
                "MATCH (n) "
                "WHERE any(l IN labels(n) WHERE l STARTS WITH 'TS_') "
                "  AND n.module = $mod "
                "DETACH DELETE n "
                "RETURN count(n) AS cnt",
                mod=self._module,
            )
            cnt = result.single()["cnt"]
        logger.info("  Deleted %d TestSpec nodes for %s", cnt, self._module)
        return cnt

    def stamp_testspec(self, doc_name: str, content_hash: str) -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (d:TS_TestSpecDocument {document_name: $doc, module: $mod}) "
                f"SET d.{HASH_PROPERTY} = $hash",
                doc=doc_name, mod=self._module, hash=content_hash,
            )

    # ── Jama / Requirements ───────────────────────────────────

    def plan_jama(
        self, data_path: Path, rel_path: Optional[Path] = None,
    ) -> IncrementalPlan:
        """Check whether cached Jama JSON has changed."""
        h = hashlib.sha256()
        h.update(_hash_file(data_path).encode())
        if rel_path and rel_path.exists():
            h.update(_hash_file(rel_path).encode())
        combined = h.hexdigest()

        with self._driver.session() as s:
            rec = s.run(
                "MATCH (m:MCALModule {name: $mod}) RETURN m._jama_hash AS hash",
                mod=self._module,
            ).single()
        stored = rec["hash"] if rec else None

        key = f"jama_{self._module}"
        if stored == combined:
            return IncrementalPlan(unchanged=[key])
        return IncrementalPlan(changed={key: combined})

    def delete_jama_nodes(self) -> int:
        """Delete requirement + folder nodes for this module."""
        with self._driver.session() as s:
            result = s.run(
                "MATCH (n) "
                "WHERE (n:StakeholderRequirement OR n:ProductRequirement OR n:Folder) "
                "  AND n.module = $mod "
                "DETACH DELETE n "
                "RETURN count(n) AS cnt",
                mod=self._module,
            )
            cnt = result.single()["cnt"]
        logger.info("  Deleted %d Jama/requirement nodes for %s", cnt, self._module)
        return cnt

    def stamp_jama(self, combined_hash: str) -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (m:MCALModule {name: $mod}) SET m._jama_hash = $hash",
                mod=self._module, hash=combined_hash,
            )

    # ── Internal helpers ──────────────────────────────────────

    def _get_stored_hashes(self, label: str, uid_prop: str) -> Dict[str, str]:
        label, uid_prop = _validate_ident(label), _validate_ident(uid_prop)
        with self._driver.session() as s:
            result = s.run(
                f"MATCH (n:{label} {{module: $mod}}) "
                f"WHERE n.{HASH_PROPERTY} IS NOT NULL "
                f"RETURN n.{uid_prop} AS uid, n.{HASH_PROPERTY} AS hash",
                mod=self._module,
            )
            return {rec["uid"]: rec["hash"] for rec in result}

    def _plan_files(
        self,
        file_map: Dict[str, Path],
        label: str,
        uid_prop: str,
    ) -> IncrementalPlan:
        # 1 — hash current files (parallel)
        current: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_hash_file, path): fid
                for fid, path in file_map.items()
            }
            for fut in as_completed(futures):
                fid = futures[fut]
                try:
                    current[fid] = fut.result()
                except Exception as exc:
                    logger.warning("Hash failed for %s: %s", fid, exc)

        # 2 — stored hashes from Neo4j
        stored = self._get_stored_hashes(label, uid_prop)

        # 3 — compare
        plan = IncrementalPlan()
        for fid, new_hash in current.items():
            if stored.get(fid) == new_hash:
                plan.unchanged.append(fid)
            else:
                plan.changed[fid] = new_hash

        for fid in stored:
            if fid not in current:
                plan.deleted.append(fid)

        return plan

    def _stamp_hashes(
        self, label: str, uid_prop: str, file_hashes: Dict[str, str],
    ) -> None:
        if not file_hashes:
            return
        label, uid_prop = _validate_ident(label), _validate_ident(uid_prop)
        items = [{"uid": uid, "hash": h} for uid, h in file_hashes.items()]
        with self._driver.session() as s:
            s.run(
                f"UNWIND $items AS item "
                f"MATCH (n:{label} {{{uid_prop}: item.uid, module: $mod}}) "
                f"SET n.{HASH_PROPERTY} = item.hash",
                items=items, mod=self._module,
            )
        logger.debug("  Stamped %d %s hashes", len(items), label)
