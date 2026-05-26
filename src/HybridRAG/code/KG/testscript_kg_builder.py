"""
Test Script Knowledge Graph Builder
=====================================

Ingests parsed C test script data into Neo4j.

Node Types Created:
    - TSCR_File           — test script file (one per .c file)
    - TSCR_TestCase       — individual test case function
    - TSCR_Helper         — helper/wrapper function

Relationships Created:
    - TSCR_BELONGS_TO_FILE   (TSCR_TestCase/Helper → TSCR_File)
    - TSCR_IMPLEMENTS_TS     (TSCR_TestCase → TS_FunctionalTestCase)
    - TSCR_TESTS_API         (TSCR_TestCase → EA_Function)
    - TSCR_CALLS_HELPER      (TSCR_TestCase → TSCR_Helper)
    - TSCR_BELONGS_TO_MODULE (TSCR_File → MCALModule)

Usage::

    from testscript_kg_builder import TestScriptKGBuilder

    builder = TestScriptKGBuilder(
        neo4j_cfg={"uri": "...", "username": "...", "password": "...", "database": "neo4j"},
        script_paths=["path/to/Test_Eth_17_Leth.c"],
        module="ETH_17_LETH",
    )
    builder.build()
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

try:
    from .testscript_parser import parse_test_script, ParseResult
except ImportError:
    from testscript_parser import parse_test_script, ParseResult

logger = logging.getLogger("testscript_kg_builder")


class TestScriptKGBuilder:
    """Builds Neo4j knowledge graph from C test script files."""

    BATCH_SIZE = 100

    def __init__(
        self,
        neo4j_cfg: dict,
        script_paths: List[str | Path],
        module: str,
        *,
        project: str = "A3G",
        dry_run: bool = False,
        clear_module: bool = False,
    ):
        """
        Args:
            neo4j_cfg: Dict with uri, username, password, database keys.
            script_paths: Paths to .c/.h test script files.
            module: MCAL module name (e.g. "ETH_17_LETH").
            project: Project identifier (default "A3G").
            dry_run: If True, parse but don't write to Neo4j.
            clear_module: If True, delete existing TSCR nodes for this module.
        """
        self.neo4j_cfg = neo4j_cfg
        self.script_paths = [Path(p) for p in script_paths]
        self.module = module.upper()
        self.project = project
        self.dry_run = dry_run
        self.clear_module = clear_module
        self.stats: Counter = Counter()
        self._driver = None

    # -- Neo4j Connection ---------------------------------------------------

    def _connect(self):
        cfg = self.neo4j_cfg
        uri = cfg["uri"]
        logger.info("Connecting to Neo4j at %s …", uri)
        try:
            drv_kw = dict(
                auth=(cfg["username"], cfg["password"]),
                max_connection_lifetime=cfg.get("max_connection_lifetime", 3600),
                max_connection_pool_size=cfg.get("max_connection_pool_size", 50),
            )
            if "+s" not in uri.split("://")[0]:
                drv_kw["encrypted"] = cfg.get("encrypted", False)
            self._driver = GraphDatabase.driver(uri, **drv_kw)
            self._driver.verify_connectivity()
        except (ServiceUnavailable, AuthError, OSError) as exc:
            logger.error("Could not connect to Neo4j at %s: %s", uri, exc)
            print(f"\n  ERROR: Neo4j is not reachable at {uri}.\n")
            sys.exit(1)
        logger.info("Connected to Neo4j (database: %s)", cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry logic."""
        if self.dry_run:
            return
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("Write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient error (attempt %d/%d), retrying in %ds…",
                               attempt, max_attempts, wait)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None) -> list[dict]:
        """Execute a read query."""
        db = self.neo4j_cfg["database"]
        with self._driver.session(database=db) as session:
            result = session.run(cypher, parameters or {})
            return [rec.data() for rec in result]

    # -- Schema Constraints -------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for TSCR node types."""
        constraints = [
            ("tscr_file_uid", "TSCR_File", "uid"),
            ("tscr_testcase_uid", "TSCR_TestCase", "uid"),
            ("tscr_helper_uid", "TSCR_Helper", "uid"),
        ]
        for name, label, prop in constraints:
            cypher = (
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            try:
                self._write_tx(cypher)
            except Exception as exc:
                logger.debug("Constraint %s: %s", name, exc)

    # -- Clear Module -------------------------------------------------------

    def _clear_existing(self):
        """Delete existing TSCR nodes for this module."""
        for label in ("TSCR_TestCase", "TSCR_Helper", "TSCR_File"):
            cypher = f"MATCH (n:{label} {{module: $module}}) DETACH DELETE n"
            self._write_tx(cypher, {"module": self.module})
            logger.info("Cleared %s nodes for module %s", label, self.module)

    # -- Ingest TSCR_File ---------------------------------------------------

    def _ingest_file_node(self, result: ParseResult) -> str:
        """Create the TSCR_File node and return its uid."""
        meta = result.file_metadata
        uid = f"{self.module}_{meta.filename}"

        cypher = """
        MERGE (n:TSCR_File {uid: $uid})
        SET n.module = $module,
            n.project = $project,
            n.filename = $filename,
            n.filepath = $filepath,
            n.version = $version,
            n.date = $date,
            n.author = $author,
            n.description = $description,
            n.test_case_count = $tc_count,
            n.helper_count = $helper_count
        """
        self._write_tx(cypher, {
            "uid": uid,
            "module": self.module,
            "project": self.project,
            "filename": meta.filename,
            "filepath": meta.filepath,
            "version": meta.version,
            "date": meta.date,
            "author": meta.author,
            "description": meta.description,
            "tc_count": len(result.test_cases),
            "helper_count": len(result.helpers),
        })
        self.stats["TSCR_File_nodes"] += 1
        return uid

    # -- Ingest TSCR_TestCase -----------------------------------------------

    def _ingest_test_cases(self, result: ParseResult, file_uid: str):
        """Batch-ingest test case nodes."""
        if not result.test_cases:
            return

        logger.info("Ingesting %d TSCR_TestCase nodes …", len(result.test_cases))

        cypher = """
        UNWIND $batch AS tc
        MERGE (n:TSCR_TestCase {uid: tc.uid})
        SET n.module = $module,
            n.project = $project,
            n.test_case_id = tc.test_case_id,
            n.function_name = tc.function_name,
            n.category = tc.category,
            n.description = tc.description,
            n.body = tc.body,
            n.line_start = tc.line_start,
            n.line_end = tc.line_end,
            n.apis_called = tc.apis_called,
            n.cfg_guards = tc.cfg_guards,
            n.input_param_count = tc.input_param_count,
            n.result_sends = tc.result_sends,
            n.file_uid = $file_uid
        """

        batch = []
        for tc in result.test_cases:
            batch.append({
                "uid": f"{self.module}_{tc.test_case_id}",
                "test_case_id": tc.test_case_id,
                "function_name": tc.function_name,
                "category": tc.category,
                "description": tc.description,
                "body": tc.body,
                "line_start": tc.line_start,
                "line_end": tc.line_end,
                "apis_called": tc.apis_called,
                "cfg_guards": tc.cfg_guards,
                "input_param_count": tc.input_param_count,
                "result_sends": tc.result_sends,
            })

            if len(batch) >= self.BATCH_SIZE:
                self._write_tx(cypher, {"batch": batch, "module": self.module,
                                        "project": self.project, "file_uid": file_uid})
                self.stats["TSCR_TestCase_nodes"] += len(batch)
                batch = []

        if batch:
            self._write_tx(cypher, {"batch": batch, "module": self.module,
                                    "project": self.project, "file_uid": file_uid})
            self.stats["TSCR_TestCase_nodes"] += len(batch)

    # -- Ingest TSCR_Helper -------------------------------------------------

    def _ingest_helpers(self, result: ParseResult, file_uid: str):
        """Batch-ingest helper function nodes."""
        if not result.helpers:
            return

        logger.info("Ingesting %d TSCR_Helper nodes …", len(result.helpers))

        cypher = """
        UNWIND $batch AS h
        MERGE (n:TSCR_Helper {uid: h.uid})
        SET n.module = $module,
            n.project = $project,
            n.function_name = h.function_name,
            n.category = h.category,
            n.description = h.description,
            n.body = h.body,
            n.line_start = h.line_start,
            n.line_end = h.line_end,
            n.apis_called = h.apis_called,
            n.file_uid = $file_uid
        """

        batch = []
        for h in result.helpers:
            batch.append({
                "uid": f"{self.module}_{h.function_name}",
                "function_name": h.function_name,
                "category": h.category,
                "description": h.description,
                "body": h.body,
                "line_start": h.line_start,
                "line_end": h.line_end,
                "apis_called": h.apis_called,
            })

            if len(batch) >= self.BATCH_SIZE:
                self._write_tx(cypher, {"batch": batch, "module": self.module,
                                        "project": self.project, "file_uid": file_uid})
                self.stats["TSCR_Helper_nodes"] += len(batch)
                batch = []

        if batch:
            self._write_tx(cypher, {"batch": batch, "module": self.module,
                                    "project": self.project, "file_uid": file_uid})
            self.stats["TSCR_Helper_nodes"] += len(batch)

    # -- Relationships ------------------------------------------------------

    def _create_belongs_to_file(self, file_uid: str):
        """Create TSCR_BELONGS_TO_FILE relationships."""
        for label in ("TSCR_TestCase", "TSCR_Helper"):
            cypher = f"""
            MATCH (n:{label} {{file_uid: $file_uid}})
            MATCH (f:TSCR_File {{uid: $file_uid}})
            MERGE (n)-[:TSCR_BELONGS_TO_FILE]->(f)
            """
            self._write_tx(cypher, {"file_uid": file_uid})
        self.stats["TSCR_BELONGS_TO_FILE_rels"] += 1
        logger.info("Created TSCR_BELONGS_TO_FILE relationships")

    def _create_implements_ts(self):
        """
        Create TSCR_IMPLEMENTS_TS relationships linking test script functions
        to their TS_FunctionalTestCase nodes via test_case_id.
        """
        cypher = """
        MATCH (tscr:TSCR_TestCase {module: $module})
        MATCH (ts:TS_FunctionalTestCase {test_case_id: tscr.test_case_id, module: $module})
        MERGE (tscr)-[:TSCR_IMPLEMENTS_TS]->(ts)
        """
        self._write_tx(cypher, {"module": self.module})

        # Count created relationships
        count_cypher = """
        MATCH (tscr:TSCR_TestCase {module: $module})-[r:TSCR_IMPLEMENTS_TS]->(ts)
        RETURN count(r) as cnt
        """
        result = self._run(count_cypher, {"module": self.module})
        cnt = result[0]["cnt"] if result else 0
        self.stats["TSCR_IMPLEMENTS_TS_rels"] = cnt
        logger.info("Created %d TSCR_IMPLEMENTS_TS relationships", cnt)

    def _create_tests_api(self):
        """
        Create TSCR_TESTS_API relationships linking test script functions
        to EA_Function nodes via the apis_called list.
        """
        cypher = """
        MATCH (tscr:TSCR_TestCase {module: $module})
        UNWIND tscr.apis_called AS api_name
        MATCH (ea:EA_Function {name: api_name})
        MERGE (tscr)-[:TSCR_TESTS_API]->(ea)
        """
        self._write_tx(cypher, {"module": self.module})

        count_cypher = """
        MATCH (tscr:TSCR_TestCase {module: $module})-[r:TSCR_TESTS_API]->(ea)
        RETURN count(r) as cnt
        """
        result = self._run(count_cypher, {"module": self.module})
        cnt = result[0]["cnt"] if result else 0
        self.stats["TSCR_TESTS_API_rels"] = cnt
        logger.info("Created %d TSCR_TESTS_API relationships", cnt)

    def _create_calls_helper(self):
        """
        Create TSCR_CALLS_HELPER relationships between test cases and helpers
        by checking if the helper function_name appears in the test case body.
        """
        # Get all helper function names for this module
        helpers_q = """
        MATCH (h:TSCR_Helper {module: $module})
        RETURN h.function_name as name, h.uid as uid
        """
        helpers = self._run(helpers_q, {"module": self.module})
        if not helpers:
            return

        logger.info("Linking test cases → %d helpers …", len(helpers))

        # For each helper, find test cases that call it
        cypher = """
        MATCH (tc:TSCR_TestCase {module: $module})
        WHERE tc.body CONTAINS $helper_name
        MATCH (h:TSCR_Helper {uid: $helper_uid})
        MERGE (tc)-[:TSCR_CALLS_HELPER]->(h)
        """

        for h in helpers:
            self._write_tx(cypher, {
                "module": self.module,
                "helper_name": h["name"],
                "helper_uid": h["uid"],
            })

        count_cypher = """
        MATCH (tc:TSCR_TestCase {module: $module})-[r:TSCR_CALLS_HELPER]->(h)
        RETURN count(r) as cnt
        """
        result = self._run(count_cypher, {"module": self.module})
        cnt = result[0]["cnt"] if result else 0
        self.stats["TSCR_CALLS_HELPER_rels"] = cnt
        logger.info("Created %d TSCR_CALLS_HELPER relationships", cnt)

    def _create_belongs_to_module(self, file_uid: str):
        """Link TSCR_File to MCALModule."""
        cypher = """
        MATCH (f:TSCR_File {uid: $file_uid})
        MATCH (m:MCALModule {name: $module})
        MERGE (f)-[:TSCR_BELONGS_TO_MODULE]->(m)
        """
        self._write_tx(cypher, {"file_uid": file_uid, "module": self.module})
        self.stats["TSCR_BELONGS_TO_MODULE_rels"] += 1

    # -- Build Pipeline -----------------------------------------------------

    def build(self):
        """Run the full test script → Neo4j ingestion pipeline."""
        t0 = time.time()

        print("=" * 60)
        print(f"  Test Script KG Builder — module: {self.module}")
        print(f"  Files: {[p.name for p in self.script_paths]}")
        print(f"  Project: {self.project}")
        print(f"  Dry run: {self.dry_run}")
        print(f"  Clear existing: {self.clear_module}")
        print("=" * 60)

        # 1. Parse all script files
        logger.info("Step 1: Parsing test script files …")
        all_results: List[ParseResult] = []
        for path in self.script_paths:
            if not path.exists():
                logger.warning("File not found: %s — skipping", path)
                continue
            result = parse_test_script(path, module=self.module)
            all_results.append(result)
            print(f"  Parsed {path.name}: "
                  f"{len(result.test_cases)} test cases, "
                  f"{len(result.helpers)} helpers")

        total_tc = sum(len(r.test_cases) for r in all_results)
        total_h = sum(len(r.helpers) for r in all_results)
        print(f"  Total: {total_tc} test cases, {total_h} helpers")

        if total_tc == 0:
            print("  WARNING: No test cases found — nothing to ingest.")
            return

        if self.dry_run:
            print("\n  DRY RUN — no Neo4j writes performed.")
            self._print_summary(t0)
            return

        # 2. Connect to Neo4j
        logger.info("Step 2: Connecting to Neo4j …")
        self._connect()

        try:
            # 3. Ensure constraints
            logger.info("Step 3: Ensuring constraints …")
            self._ensure_constraints()

            # 4. Clear existing (if requested)
            if self.clear_module:
                logger.info("Step 4: Clearing existing TSCR nodes …")
                self._clear_existing()

            # 5. Ingest nodes per file
            logger.info("Step 5: Ingesting nodes …")
            for result in all_results:
                file_uid = self._ingest_file_node(result)
                self._ingest_test_cases(result, file_uid)
                self._ingest_helpers(result, file_uid)
                self._create_belongs_to_file(file_uid)
                self._create_belongs_to_module(file_uid)

            # 6. Create cross-artifact relationships
            logger.info("Step 6: Creating cross-artifact relationships …")
            self._create_implements_ts()
            self._create_tests_api()
            self._create_calls_helper()

        finally:
            self._close()

        self._print_summary(t0)

    def _print_summary(self, t0: float):
        elapsed = time.time() - t0
        print(f"\n{'='*60}")
        print(f"  Test Script KG Ingestion Summary — {self.module}")
        print(f"{'='*60}")
        for key, val in sorted(self.stats.items()):
            print(f"    {key}: {val}")
        print(f"    elapsed: {elapsed:.1f}s")
        print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_neo4j_config(profile: str = "mcal") -> dict:
    """Load Neo4j config from storage_config.yaml, with .env override for credentials."""
    import os
    import yaml
    # Path: KG/ → code/ → HybridRAG/ — config is in HybridRAG/config/
    config_path = Path(__file__).parent.parent.parent / "config" / "storage_config.yaml"

    with open(config_path) as f:
        all_cfg = yaml.safe_load(f)

    neo4j_profiles = all_cfg.get("neo4j", {})
    if profile not in neo4j_profiles:
        print(f"ERROR: profile '{profile}' not in storage_config.yaml (available: {list(neo4j_profiles.keys())})")
        sys.exit(1)

    cfg = neo4j_profiles[profile]

    # Override credentials from .env if available
    from dotenv import load_dotenv
    # Path: KG/ → code/ → HybridRAG/ → src/ → ai-core-engine/ — env is in ai-core-engine/env/
    env_path = Path(__file__).parent.parent.parent.parent.parent / "env" / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    cfg["username"] = os.environ.get("NEO4J_USERNAME", cfg.get("username", "neo4j"))
    cfg["password"] = os.environ.get("NEO4J_PASSWORD", cfg.get("password", ""))

    return cfg


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ingest test scripts into Neo4j KG")
    parser.add_argument("scripts", nargs="+", help="Paths to .c test script files")
    parser.add_argument("--module", required=True, help="MCAL module name (e.g. ETH_17_LETH)")
    parser.add_argument("--profile", default="mcal", help="Neo4j profile (default: mcal)")
    parser.add_argument("--project", default="A3G", help="Project name (default: A3G)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true", help="Clear existing TSCR nodes first")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(name)s: %(message)s")

    neo4j_cfg = _load_neo4j_config(args.profile)

    builder = TestScriptKGBuilder(
        neo4j_cfg=neo4j_cfg,
        script_paths=args.scripts,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_module=args.clear,
    )
    builder.build()


if __name__ == "__main__":
    main()
