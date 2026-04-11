"""
PostgreSQL Schema & Migration — Sprint 8
==========================================
Replaces SQLite for metadata, audit trails, feedback data.
Neo4j and Qdrant remain unchanged — PostgreSQL handles the administrative layer.

Tables:
  audit_logs        — Every MCP tool invocation (ASPICE prompt logging)
  response_archive  — AI-generated outputs (ASPICE reproducibility)
  review_evidence   — Human review decisions (ASPICE work products)
  feedback_records  — Learning data from submit_human_feedback
  failure_patterns  — Learned failure patterns for continuous improvement
  ingestion_jobs    — Async ingestion job tracking
  sessions_meta     — Session metadata for cross-process visibility

Usage:
  # Initialize database
  python -m src.Observability.postgres_schema --init

  # Or use Alembic (production):
  alembic upgrade head
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════
#  SQL Schema (DDL)
# ═════════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
-- ASPICE Observability: Prompt Logging
CREATE TABLE IF NOT EXISTS audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool_name       VARCHAR(100) NOT NULL,
    workspace_id    VARCHAR(50) DEFAULT 'illd',
    session_id      VARCHAR(200),
    caller_api_key  VARCHAR(100),
    parameters      JSONB,
    response_code   VARCHAR(50),
    duration_ms     INTEGER,
    token_count     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_logs(tool_name);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);

-- ASPICE Observability: Response Archive
CREATE TABLE IF NOT EXISTS response_archive (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    response_id     VARCHAR(200) UNIQUE NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      VARCHAR(200),
    tool_name       VARCHAR(100),
    input_context   JSONB,
    output_content  TEXT,
    model_version   VARCHAR(100),
    confidence_score INTEGER,
    review_type     VARCHAR(20),
    token_count     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_response_rid ON response_archive(response_id);

-- ASPICE Observability: Review Evidence
CREATE TABLE IF NOT EXISTS review_evidence (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id       VARCHAR(200) UNIQUE NOT NULL,
    response_id     VARCHAR(200) NOT NULL REFERENCES response_archive(response_id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewer_id     VARCHAR(100),
    decision        VARCHAR(50) NOT NULL,
    issues_found    INTEGER DEFAULT 0,
    rationale       TEXT,
    checklist       JSONB
);
CREATE INDEX IF NOT EXISTS idx_review_resp ON review_evidence(response_id);

-- Feedback & Learning
CREATE TABLE IF NOT EXISTS feedback_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_id     VARCHAR(200) UNIQUE NOT NULL,
    response_id     VARCHAR(200) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision        VARCHAR(50) NOT NULL,
    reviewer_id     VARCHAR(100),
    issues_found    INTEGER DEFAULT 0,
    correction_notes TEXT,
    module          VARCHAR(50),
    task_type       VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_feedback_resp ON feedback_records(response_id);
CREATE INDEX IF NOT EXISTS idx_feedback_decision ON feedback_records(decision);

-- Failure Patterns (continuous learning)
CREATE TABLE IF NOT EXISTS failure_patterns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern_id      VARCHAR(200) UNIQUE NOT NULL,
    category        VARCHAR(100),
    module          VARCHAR(50),
    description     TEXT,
    occurrence_count INTEGER DEFAULT 1,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    suggested_fix   TEXT,
    severity        VARCHAR(20) DEFAULT 'medium'
);

-- Ingestion Job Tracking
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          VARCHAR(200) UNIQUE NOT NULL,
    job_type        VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'queued',
    workspace_id    VARCHAR(50) DEFAULT 'illd',
    params          JSONB,
    progress        INTEGER DEFAULT 0,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_status ON ingestion_jobs(status);

-- Session Metadata (cross-process visibility)
CREATE TABLE IF NOT EXISTS sessions_meta (
    session_id      VARCHAR(200) PRIMARY KEY,
    assistant_name  VARCHAR(100),
    module_context  VARCHAR(50),
    workspace_id    VARCHAR(50) DEFAULT 'illd',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    ttl_seconds     INTEGER DEFAULT 3600,
    store_keys      JSONB DEFAULT '[]',
    context_count   INTEGER DEFAULT 0
);
"""


# ═════════════════════════════════════════════════════════════════════════
#  PostgreSQL Client (optional — gracefully degrades without psycopg2)
# ═════════════════════════════════════════════════════════════════════════

class PostgresClient:
    """
    Lightweight PostgreSQL client for ASPICE observability.

    Degrades gracefully: if PostgreSQL is unavailable, all write
    operations are no-ops and read operations return empty results.
    """

    def __init__(self, dsn: Optional[str] = None):
        self._dsn = dsn or os.environ.get("POSTGRES_DSN",
            os.environ.get("DATABASE_URL", ""))
        self._conn = None
        self._available = False

        if self._dsn:
            try:
                import psycopg2
                self._conn = psycopg2.connect(self._dsn)
                self._conn.autocommit = True
                self._available = True
                logger.info("[PostgreSQL] Connected: %s", self._dsn[:30] + "...")
            except ImportError:
                logger.info("[PostgreSQL] psycopg2 not installed — audit logging disabled")
            except Exception as e:
                logger.warning("[PostgreSQL] Connection failed: %s — audit logging disabled", e)
        else:
            logger.info("[PostgreSQL] No DSN configured — audit logging to memory only")

    @property
    def available(self) -> bool:
        return self._available

    def init_schema(self):
        """Create tables if they don't exist."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            logger.info("[PostgreSQL] Schema initialized")
        except Exception as e:
            logger.error("[PostgreSQL] Schema init failed: %s", e)

    def log_audit(self, tool_name: str, workspace_id: str = "illd",
                  session_id: Optional[str] = None, parameters: Optional[Dict] = None,
                  response_code: str = "ok", duration_ms: int = 0, token_count: int = 0):
        """Log an MCP tool invocation for ASPICE compliance."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_logs (tool_name, workspace_id, session_id, "
                    "parameters, response_code, duration_ms, token_count) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (tool_name, workspace_id, session_id,
                     json.dumps(parameters or {}), response_code, duration_ms, token_count)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] Audit log failed: %s", e)

    def archive_response(self, response_id: str, session_id: Optional[str] = None,
                         tool_name: str = "", output_content: str = "",
                         confidence_score: int = 0, review_type: str = "",
                         model_version: str = "", token_count: int = 0):
        """Archive an AI-generated response for reproducibility."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO response_archive (response_id, session_id, tool_name, "
                    "output_content, confidence_score, review_type, model_version, token_count) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (response_id) DO NOTHING",
                    (response_id, session_id, tool_name, output_content[:50000],
                     confidence_score, review_type, model_version, token_count)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] Response archive failed: %s", e)

    def get_audit_logs(self, limit: int = 50, tool_name: Optional[str] = None) -> List[Dict]:
        """Query audit logs."""
        if not self._available:
            return []
        try:
            with self._conn.cursor() as cur:
                if tool_name:
                    cur.execute("SELECT * FROM audit_logs WHERE tool_name = %s "
                                "ORDER BY timestamp DESC LIMIT %s", (tool_name, limit))
                else:
                    cur.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT %s", (limit,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning("[PostgreSQL] Audit query failed: %s", e)
            return []

    # ── Feedback persistence (FeedbackSink write-through) ──────────────

    def save_feedback(self, feedback_id: str, response_id: str, decision: str,
                      reviewer_id: Optional[str] = None, issues_found: int = 0,
                      correction_notes: Optional[str] = None,
                      module: Optional[str] = None, task_type: Optional[str] = None):
        """Persist a feedback record from FeedbackSink."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feedback_records (feedback_id, response_id, decision, "
                    "reviewer_id, issues_found, correction_notes, module, task_type) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (feedback_id) DO NOTHING",
                    (feedback_id, response_id, decision, reviewer_id,
                     issues_found, correction_notes, module, task_type)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] save_feedback failed: %s", e)

    def save_review_evidence(self, review_id: str, response_id: str, decision: str,
                             reviewer_id: Optional[str] = None, issues_found: int = 0,
                             rationale: Optional[str] = None, checklist: Optional[Dict] = None):
        """Persist review evidence from FeedbackSink.complete_review."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO review_evidence (review_id, response_id, decision, "
                    "reviewer_id, issues_found, rationale, checklist) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (review_id) DO NOTHING",
                    (review_id, response_id, decision, reviewer_id,
                     issues_found, rationale, json.dumps(checklist or {}))
                )
        except Exception as e:
            logger.warning("[PostgreSQL] save_review_evidence failed: %s", e)

    def save_failure_pattern(self, category: Optional[str] = None,
                             module: Optional[str] = None, description: Optional[str] = None,
                             suggested_fix: Optional[str] = None, severity: str = "medium"):
        """Persist a learned failure pattern."""
        if not self._available:
            return
        try:
            pattern_id = f"fp_{uuid.uuid4().hex[:10]}"
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO failure_patterns (pattern_id, category, module, "
                    "description, suggested_fix, severity) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (pattern_id, category, module, description, suggested_fix, severity)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] save_failure_pattern failed: %s", e)

    # ── Ingestion job persistence (IngestionJobTracker write-through) ──

    def save_ingestion_job(self, job_id: str, job_type: str, status: str = "queued",
                           workspace_id: str = "illd", params: Optional[Dict] = None):
        """Persist an ingestion job record."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingestion_jobs (job_id, job_type, status, workspace_id, params) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (job_id) DO NOTHING",
                    (job_id, job_type, status, workspace_id, json.dumps(params or {}))
                )
        except Exception as e:
            logger.warning("[PostgreSQL] save_ingestion_job failed: %s", e)

    def update_ingestion_job(self, job_id: str, status: Optional[str] = None,
                             progress: Optional[int] = None, result: Optional[Dict] = None,
                             error: Optional[str] = None):
        """Update an ingestion job's status/progress/result."""
        if not self._available:
            return
        sets = ["updated_at = NOW()"]
        vals: list = []
        if status is not None:
            sets.append("status = %s")
            vals.append(status)
        if progress is not None:
            sets.append("progress = %s")
            vals.append(progress)
        if result is not None:
            sets.append("result = %s")
            vals.append(json.dumps(result))
        if error is not None:
            sets.append("error = %s")
            vals.append(error)
        vals.append(job_id)
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE job_id = %s", vals)
        except Exception as e:
            logger.warning("[PostgreSQL] update_ingestion_job failed: %s", e)

    # ── Session metadata persistence (SessionManager write-through) ────

    def save_session_meta(self, session_id: str, assistant_name: Optional[str] = None,
                          module_context: Optional[str] = None,
                          workspace_id: str = "illd", ttl_seconds: int = 3600):
        """Persist session metadata for cross-process visibility."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions_meta (session_id, assistant_name, module_context, "
                    "workspace_id, ttl_seconds) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "assistant_name = EXCLUDED.assistant_name, "
                    "module_context = EXCLUDED.module_context",
                    (session_id, assistant_name, module_context, workspace_id, ttl_seconds)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] save_session_meta failed: %s", e)

    def close_session_meta(self, session_id: str, store_keys: Optional[List] = None,
                           context_count: int = 0):
        """Mark a session as closed with final stats."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions_meta SET closed_at = NOW(), store_keys = %s, "
                    "context_count = %s WHERE session_id = %s",
                    (json.dumps(store_keys or []), context_count, session_id)
                )
        except Exception as e:
            logger.warning("[PostgreSQL] close_session_meta failed: %s", e)

    def close(self):
        if self._conn:
            self._conn.close()


# ═════════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--init" in sys.argv:
        client = PostgresClient()
        if client.available:
            client.init_schema()
            print("Schema initialized successfully")
        else:
            print("PostgreSQL not available. Set POSTGRES_DSN or DATABASE_URL.")
            print("\nSchema DDL for manual creation:")
            print(SCHEMA_SQL)
    elif "--ddl" in sys.argv:
        print(SCHEMA_SQL)
    else:
        print("Usage: python -m src.Observability.postgres_schema [--init|--ddl]")
