"""
BVEC Knowledge Graph Builder
==============================

Ingests parsed BVEC (Boundary Value & Equivalence Class) analysis data
into Neo4j.

Node Types Created:
    - BVEC_InputParameter   — BV entry for API input parameters
    - BVEC_OutputParameter  — BV entry for API output/return parameters
    - BVEC_ConfigParameter  — BV entry for configuration parameters

Relationships Created:
    - BVEC_VERIFIED_BY  (BVEC_* → TS_FunctionalTestCase / TS_ConfigTestCase)
    - BVEC_FOR_API      (BVEC_* → SRC_Function)

Usage::

    from bvec_kg_builder import BVECKnowledgeGraphBuilder

    builder = BVECKnowledgeGraphBuilder(
        neo4j_cfg={"uri": "...", "username": "...", "password": "...", "database": "neo4j"},
        xlsx_path="path/to/BVEC.xlsx",
        module="ETH_17_LETH",
    )
    builder.build()
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

try:
    from .bvec_parser import parse_bvec_workbook
except ImportError:
    from bvec_parser import parse_bvec_workbook

logger = logging.getLogger("bvec_kg_builder")


# ---------------------------------------------------------------------------
# Test case ID parsing
# ---------------------------------------------------------------------------
# Pattern: "Leth_Tc_Fn_004(AS460_TC4DX_C021_P01)"
# We extract the base test case name: "Leth_Tc_Fn_004"
_TC_ID_RE = re.compile(r"([A-Za-z]+_Tc_[A-Za-z]+_\d+)")


def _extract_test_case_names(test_case_id: str) -> List[str]:
    """
    Extract base test case names from the test_case_id field.

    Input:  "Leth_Tc_Fn_004(AS460_TC4DX_C021_P01)"
    Output: ["Leth_Tc_Fn_004"]

    Handles comma-separated and newline-separated entries.
    """
    if not test_case_id or test_case_id == "NA":
        return []
    return list(set(_TC_ID_RE.findall(test_case_id)))


class BVECKnowledgeGraphBuilder:
    """Builds Neo4j knowledge graph from BVEC Analysis Report Excel."""

    BATCH_SIZE = 200

    def __init__(
        self,
        neo4j_cfg: dict,
        xlsx_path: str | Path,
        module: str,
        *,
        project: str = "A3G",
        dry_run: bool = False,
        clear_module: bool = False,
    ):
        """
        Args:
            neo4j_cfg: Dict with uri, username, password, database keys.
            xlsx_path: Path to the BVEC Excel file.
            module: MCAL module name (e.g. "ETH_17_LETH").
            project: Project identifier (default "A3G").
            dry_run: If True, parse but don't write to Neo4j.
            clear_module: If True, delete existing BVEC nodes for this module.
        """
        self.neo4j_cfg = neo4j_cfg
        self.xlsx_path = Path(xlsx_path)
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
        """Create uniqueness constraints for BVEC node types."""
        constraints = [
            ("bvec_input_uid", "BVEC_InputParameter", "uid"),
            ("bvec_output_uid", "BVEC_OutputParameter", "uid"),
            ("bvec_config_uid", "BVEC_ConfigParameter", "uid"),
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
        """Delete all existing BVEC nodes for this module."""
        logger.info("Clearing existing BVEC nodes for module=%s …", self.module)
        for label in ("BVEC_InputParameter", "BVEC_OutputParameter", "BVEC_ConfigParameter"):
            cypher = f"""
            MATCH (n:{label})
            WHERE n.module = $module
            DETACH DELETE n
            """
            self._write_tx(cypher, {"module": self.module})
        self.stats["cleared"] += 1

    # -- Ingest Nodes -------------------------------------------------------

    def _ingest_nodes(self, label: str, nodes: List[dict]):
        """Batch-MERGE nodes into Neo4j."""
        if not nodes:
            return

        logger.info("Ingesting %d %s nodes …", len(nodes), label)

        cypher = f"""
        UNWIND $batch AS entry
        MERGE (n:{label} {{uid: entry.uid}})
        SET n.module = entry.module,
            n.project = $project,
            n.device = entry.device,
            n.api_name = entry.api_name,
            n.parameter_name = entry.parameter_name,
            n.parameter_range = entry.parameter_range,
            n.equivalence_class = entry.equivalence_class,
            n.class_name = entry.class_name,
            n.class_range = entry.class_range,
            n.boundary_value = entry.boundary_value,
            n.actual_value = entry.actual_value,
            n.test_case_id = entry.test_case_id,
            n.remarks = entry.remarks
        """

        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "project": self.project})
            self.stats[f"{label}_nodes"] += len(batch)

    # -- Create Relationships -----------------------------------------------

    def _create_bvec_verified_by(self, all_entries: Dict[str, List[dict]]):
        """
        Create BVEC_VERIFIED_BY relationships from BVEC nodes to TS test cases.

        Matches on test_case_name property of TS_FunctionalTestCase/TS_ConfigTestCase.
        """
        logger.info("Creating BVEC_VERIFIED_BY relationships …")

        # Collect all (uid, test_case_name) pairs
        pairs: List[dict] = []
        for label, nodes in all_entries.items():
            for node in nodes:
                tc_names = _extract_test_case_names(node["test_case_id"])
                for tc_name in tc_names:
                    pairs.append({"bvec_uid": node["uid"], "tc_name": tc_name, "label": label})

        if not pairs:
            logger.info("No test case references found — skipping BVEC_VERIFIED_BY")
            return

        logger.info("Found %d BVEC → test case pairs to link", len(pairs))

        # Process by BVEC label
        for label in ("BVEC_InputParameter", "BVEC_OutputParameter", "BVEC_ConfigParameter"):
            label_pairs = [p for p in pairs if p["label"] == label]
            if not label_pairs:
                continue

            # Try matching against TS_FunctionalTestCase (test_case_id property)
            cypher = f"""
            UNWIND $batch AS pair
            MATCH (bvec:{label} {{uid: pair.bvec_uid}})
            MATCH (tc)
            WHERE (tc:TS_FunctionalTestCase OR tc:TS_ConfigTestCase OR tc:TS_WCETTestCase OR tc:TS_StaticInterfaceTestCase)
              AND tc.test_case_id = pair.tc_name
              AND tc.module = $module
            MERGE (bvec)-[:BVEC_VERIFIED_BY]->(tc)
            """

            for i in range(0, len(label_pairs), self.BATCH_SIZE):
                batch = [{"bvec_uid": p["bvec_uid"], "tc_name": p["tc_name"]}
                         for p in label_pairs[i:i + self.BATCH_SIZE]]
                self._write_tx(cypher, {"batch": batch, "module": self.module})

            self.stats["bvec_verified_by_pairs"] += len(label_pairs)

    def _create_bvec_for_api(self, all_entries: Dict[str, List[dict]]):
        """
        Create BVEC_FOR_API relationships from BVEC nodes to SRC_Function nodes.

        Matches on function_name property of SRC_Function.
        """
        logger.info("Creating BVEC_FOR_API relationships …")

        # Collect unique (uid, api_name) where api_name is a real function
        pairs: List[dict] = []
        seen_apis: set = set()

        for label, nodes in all_entries.items():
            for node in nodes:
                api = node["api_name"]
                # Skip non-function entries (void, NA, comma-separated lists for config)
                if not api or api in ("void", "NA", ""):
                    continue
                # For config params, api_name may be comma-separated — take each one
                if "," in api:
                    api_list = [a.strip() for a in api.split(",") if a.strip()]
                else:
                    api_list = [api]

                for single_api in api_list:
                    key = (node["uid"], single_api)
                    if key not in seen_apis:
                        seen_apis.add(key)
                        pairs.append({"bvec_uid": node["uid"], "api_name": single_api, "label": label})

        if not pairs:
            logger.info("No API references found — skipping BVEC_FOR_API")
            return

        logger.info("Found %d BVEC → API pairs to link", len(pairs))

        for label in ("BVEC_InputParameter", "BVEC_OutputParameter", "BVEC_ConfigParameter"):
            label_pairs = [p for p in pairs if p["label"] == label]
            if not label_pairs:
                continue

            cypher = f"""
            UNWIND $batch AS pair
            MATCH (bvec:{label} {{uid: pair.bvec_uid}})
            MATCH (fn:SRC_Function {{name: pair.api_name, module: $module}})
            MERGE (bvec)-[:BVEC_FOR_API]->(fn)
            """

            for i in range(0, len(label_pairs), self.BATCH_SIZE):
                batch = [{"bvec_uid": p["bvec_uid"], "api_name": p["api_name"]}
                         for p in label_pairs[i:i + self.BATCH_SIZE]]
                self._write_tx(cypher, {"batch": batch, "module": self.module})

            self.stats["bvec_for_api_pairs"] += len(label_pairs)

    # ======================================================================
    # PUBLIC ENTRY POINT
    # ======================================================================

    def build(self):
        """Run the full BVEC → Neo4j ingestion pipeline."""
        t0 = time.time()

        print("=" * 60)
        print(f"  BVEC KG Builder — module: {self.module}")
        print(f"  BVEC file: {self.xlsx_path.name}")
        print(f"  Project: {self.project}")
        print(f"  Dry run: {self.dry_run}")
        print(f"  Clear existing: {self.clear_module}")
        print("=" * 60)

        # 1. Parse BVEC Excel
        logger.info("Step 1: Parsing BVEC workbook …")
        entries = parse_bvec_workbook(self.xlsx_path, self.module)
        total_parsed = sum(len(v) for v in entries.values())
        print(f"  Parsed: {total_parsed} entries "
              f"({len(entries['BVEC_InputParameter'])} input, "
              f"{len(entries['BVEC_OutputParameter'])} output, "
              f"{len(entries['BVEC_ConfigParameter'])} config)")

        if total_parsed == 0:
            print("  WARNING: No entries parsed — nothing to ingest.")
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
                logger.info("Step 4: Clearing existing BVEC nodes …")
                self._clear_existing()

            # 5. Ingest nodes
            logger.info("Step 5: Ingesting BVEC nodes …")
            for label, nodes in entries.items():
                self._ingest_nodes(label, nodes)

            # 6. Create relationships
            logger.info("Step 6: Creating relationships …")
            self._create_bvec_verified_by(entries)
            self._create_bvec_for_api(entries)

        finally:
            self._close()

        self._print_summary(t0)

    def _print_summary(self, t0: float):
        elapsed = time.time() - t0
        print(f"\n{'='*60}")
        print(f"  BVEC Ingestion Summary — {self.module}")
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
    config_path = Path(__file__).parent.parent.parent / "config" / "storage_config.yaml"
    if not config_path.exists():
        print(f"  ERROR: Config not found at {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    instance = cfg.get("neo4j", {}).get(profile)
    if not instance:
        print(f"  ERROR: Profile '{profile}' not found in storage_config.yaml neo4j section")
        sys.exit(1)

    # Load .env for credentials
    env_path = Path(__file__).parent.parent.parent.parent.parent / "env" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    def _resolve(val: str) -> str:
        """Resolve ${VAR} placeholders from environment."""
        if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
            env_key = val[2:-1]
            return os.environ.get(env_key, val)
        return val

    return {
        "uri": instance["uri"],
        "username": _resolve(instance.get("username", "neo4j")),
        "password": _resolve(instance.get("password", "")),
        "database": instance.get("database", "neo4j"),
        "max_connection_lifetime": instance.get("max_connection_lifetime", 3600),
        "max_connection_pool_size": instance.get("max_connection_pool_size", 50),
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="BVEC Analysis → Neo4j ingestion")
    parser.add_argument("xlsx_path", help="Path to BVEC Excel file")
    parser.add_argument("module", help="MCAL module name (e.g. ETH_17_LETH)")
    parser.add_argument("--profile", default="mcal", help="Neo4j profile (default: mcal)")
    parser.add_argument("--project", default="A3G", help="Project identifier (default: A3G)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true", help="Clear existing BVEC nodes for module")

    args = parser.parse_args()

    neo4j_cfg = _load_neo4j_config(args.profile)

    builder = BVECKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        xlsx_path=args.xlsx_path,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_module=args.clear,
    )
    builder.build()
