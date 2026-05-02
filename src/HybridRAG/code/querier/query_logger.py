"""
Query Logger — lightweight JSONL logger for Neo4j query telemetry.
==================================================================
Logs every Neo4j query executed by SearchService to a JSONL file
so the KG health dashboard can display latency distributions,
hot queries, and slow-query analysis.

Log file: {HybridRAG}/logs/query_log.jsonl  (auto-created)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HYBRIDRAG_DIR = Path(__file__).resolve().parents[2]  # …/src/HybridRAG
_LOG_DIR = _HYBRIDRAG_DIR / "logs"
_LOG_FILE = _LOG_DIR / "query_log.jsonl"

# Cap individual log file size (rotate when exceeded)
_MAX_LOG_SIZE = int(os.environ.get("QUERY_LOG_MAX_MB", "50")) * 1024 * 1024

_lock = threading.Lock()


def _ensure_dir():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_query(
    *,
    method: str,
    cypher: str,
    params: Optional[dict] = None,
    elapsed_ms: float,
    row_count: int = 0,
    module: Optional[str] = None,
    profile: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Append one query record to the JSONL log (thread-safe)."""
    record = {
        "ts": time.time(),
        "method": method,
        "cypher": cypher[:500],  # truncate long queries
        "params_keys": sorted(params.keys()) if params else [],
        "elapsed_ms": round(elapsed_ms, 2),
        "row_count": row_count,
        "module": module,
        "profile": profile,
        "error": error,
    }
    line = json.dumps(record, default=str) + "\n"
    with _lock:
        try:
            _ensure_dir()
            # Rotate if too large
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_LOG_SIZE:
                rotated = _LOG_FILE.with_suffix(".1.jsonl")
                if rotated.exists():
                    rotated.unlink()
                _LOG_FILE.rename(rotated)
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.debug("query_logger write failed: %s", e)


def read_log(max_records: int = 5000) -> list[dict]:
    """Read the most recent query log records (newest last)."""
    if not _LOG_FILE.exists():
        return []
    records = []
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.debug("query_logger read failed: %s", e)
    # Return the tail
    return records[-max_records:]


def log_path() -> Path:
    """Return the path to the JSONL log file."""
    return _LOG_FILE
