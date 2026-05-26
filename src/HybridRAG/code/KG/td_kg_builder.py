"""
Test Data Knowledge Graph Builder
==================================

Ingests parsed Test Data (TD) into Neo4j.

Node Types Created:
    - TD_TestParameter      — IO parameter entry per test case row
    - TD_Configuration      — Test configuration metadata
    - TD_HWConnection       — Hardware pin/signal connection per device
    - TD_InterfaceMode      — Interface mode lookup (speed, phy config)

Relationships Created:
    - TD_FOR_TESTCASE       (TD_TestParameter → TS_FunctionalTestCase)
    - TD_USES_CONFIG        (TD_TestParameter → TD_Configuration)

Usage::

    from td_kg_builder import TDKnowledgeGraphBuilder

    builder = TDKnowledgeGraphBuilder(
        neo4j_cfg={"uri": "...", "username": "...", "password": "...", "database": "neo4j"},
        xlsx_path="path/to/TD.xlsx",
        module="ETH_17_LETH",
    )
    builder.build()
"""

from __future__ import annotations

import json
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
    from .td_parser import parse_td_workbook
except ImportError:
    from td_parser import parse_td_workbook

logger = logging.getLogger("td_kg_builder")

# Config ID pattern: AS460_TC49X_C021_P01
_CONFIG_ID_RE = re.compile(r"AS460_\w+?_C(\d+)_P\d+")


class TDKnowledgeGraphBuilder:
    """Builds Neo4j knowledge graph from Test Data Excel."""

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

    def _read_tx(self, cypher: str, parameters: Optional[dict] = None) -> list[dict]:
        """Execute a read query."""
        db = self.neo4j_cfg["database"]
        with self._driver.session(database=db) as session:
            result = session.run(cypher, parameters or {})
            return [rec.data() for rec in result]

    # -- Schema Constraints -------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for TD node types."""
        constraints = [
            ("td_param_uid", "TD_TestParameter", "uid"),
            ("td_config_uid", "TD_Configuration", "uid"),
            ("td_hwconn_uid", "TD_HWConnection", "uid"),
            ("td_iface_uid", "TD_InterfaceMode", "uid"),
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
        """Delete all existing TD nodes for this module."""
        logger.info("Clearing existing TD nodes for module=%s …", self.module)
        for label in ("TD_TestParameter", "TD_Configuration", "TD_HWConnection", "TD_InterfaceMode"):
            cypher = f"""
            MATCH (n:{label})
            WHERE n.module = $module
            DETACH DELETE n
            """
            self._write_tx(cypher, {"module": self.module})
        self.stats["cleared"] += 1

    # -- Ingest TD_Configuration -------------------------------------------

    def _ingest_configurations(self, configs: List[dict]):
        """Batch-MERGE TD_Configuration nodes."""
        if not configs:
            return

        logger.info("Ingesting %d TD_Configuration nodes …", len(configs))

        cypher = """
        UNWIND $batch AS entry
        MERGE (n:TD_Configuration {uid: entry.uid})
        SET n.module = entry.module,
            n.project = $project,
            n.config_index = entry.config_index,
            n.config_file_name = entry.config_file_name,
            n.devices = entry.devices,
            n.regression_options = entry.regression_options,
            n.comments = entry.comments
        """

        nodes = []
        for cfg in configs:
            uid = f"{self.module}_CFG_{cfg['config_index']}"
            nodes.append({
                "uid": uid,
                "module": self.module,
                "config_index": cfg["config_index"],
                "config_file_name": cfg["config_file_name"],
                "devices": json.dumps(cfg["devices"]),
                "regression_options": json.dumps(cfg["regression_options"]),
                "comments": cfg.get("comments"),
            })

        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "project": self.project})
            self.stats["TD_Configuration_nodes"] += len(batch)

    # -- Ingest TD_TestParameter -------------------------------------------

    def _ingest_test_parameters(self, params: List[dict]):
        """Batch-MERGE TD_TestParameter nodes."""
        if not params:
            return

        logger.info("Ingesting %d TD_TestParameter nodes …", len(params))

        cypher = """
        UNWIND $batch AS entry
        MERGE (n:TD_TestParameter {uid: entry.uid})
        SET n.module = entry.module,
            n.project = $project,
            n.test_case_id = entry.test_case_id,
            n.direction = entry.direction,
            n.data_type = entry.data_type,
            n.parameter_name = entry.parameter_name,
            n.condition = entry.condition,
            n.config_values = entry.config_values,
            n.config_count = entry.config_count
        """

        nodes = []
        seen_uids = set()
        for idx, param in enumerate(params):
            # UID: module + test_case + param_name + direction + index
            # Use index as disambiguator since same param name can appear
            # multiple times in same TC (e.g. repeated output checks)
            base_uid = f"{self.module}_{param['test_case_id']}_{param['parameter_name']}_{param['direction']}"
            uid = base_uid
            counter = 0
            while uid in seen_uids:
                counter += 1
                uid = f"{base_uid}_{counter}"
            seen_uids.add(uid)

            nodes.append({
                "uid": uid,
                "module": self.module,
                "test_case_id": param["test_case_id"],
                "direction": param["direction"],
                "data_type": param["data_type"],
                "parameter_name": param["parameter_name"],
                "condition": param["condition"],
                "config_values": json.dumps(param["config_values"]),
                "config_count": len(param["config_values"]),
            })

        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "project": self.project})
            self.stats["TD_TestParameter_nodes"] += len(batch)

        return nodes  # Return for relationship creation

    # -- Ingest TD_HWConnection --------------------------------------------

    def _ingest_hw_connections(self, connections: List[dict]):
        """Batch-MERGE TD_HWConnection nodes."""
        if not connections:
            return

        logger.info("Ingesting %d TD_HWConnection nodes …", len(connections))

        cypher = """
        UNWIND $batch AS entry
        MERGE (n:TD_HWConnection {uid: entry.uid})
        SET n.module = entry.module,
            n.project = $project,
            n.signal_name = entry.signal_name,
            n.pin_number = entry.pin_number,
            n.device = entry.device,
            n.section_header = entry.section_header,
            n.characteristics = entry.characteristics
        """

        nodes = []
        for conn in connections:
            pin = conn['pin_number'] or 'nopin'
            uid = f"{self.module}_{conn['device']}_{conn['signal_name']}_{pin}"
            nodes.append({
                "uid": uid,
                "module": self.module,
                "signal_name": conn["signal_name"],
                "pin_number": conn["pin_number"],
                "device": conn["device"],
                "section_header": conn.get("section_header"),
                "characteristics": json.dumps(conn["characteristics"]),
            })

        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "project": self.project})
            self.stats["TD_HWConnection_nodes"] += len(batch)

    # -- Ingest TD_InterfaceMode -------------------------------------------

    def _ingest_interface_modes(self, modes: List[dict]):
        """Batch-MERGE TD_InterfaceMode nodes."""
        if not modes:
            return

        logger.info("Ingesting %d TD_InterfaceMode nodes …", len(modes))

        cypher = """
        UNWIND $batch AS entry
        MERGE (n:TD_InterfaceMode {uid: entry.uid})
        SET n.module = entry.module,
            n.project = $project,
            n.interface = entry.interface,
            n.speed_mbps = entry.speed_mbps,
            n.phy_config_decimal = entry.phy_config_decimal,
            n.properties = entry.properties
        """

        nodes = []
        for mode in modes:
            speed = mode.get("speedmbps", mode.get("speed_mbps", ""))
            phy_cfg = mode.get("ethtest_phyconfigdecimal", "")
            uid = f"{self.module}_IFACE_{mode['interface']}_{speed}_{phy_cfg}"
            nodes.append({
                "uid": uid,
                "module": self.module,
                "interface": mode["interface"],
                "speed_mbps": speed,
                "phy_config_decimal": mode.get("ethtest_phyconfigdecimal", ""),
                "properties": json.dumps({k: v for k, v in mode.items()
                                          if k not in ("interface", "module")}),
            })

        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "project": self.project})
            self.stats["TD_InterfaceMode_nodes"] += len(batch)

    # -- Create Relationships -----------------------------------------------

    def _create_td_for_testcase(self):
        """Link TD_TestParameter → TS test case nodes via test_case_id."""
        logger.info("Creating TD_FOR_TESTCASE relationships …")

        cypher = """
        MATCH (td:TD_TestParameter)
        WHERE td.module = $module
        WITH td, td.test_case_id AS tc_id
        MATCH (tc)
        WHERE (tc:TS_FunctionalTestCase OR tc:TS_ConfigTestCase
               OR tc:TS_WCETTestCase OR tc:TS_StaticInterfaceTestCase)
          AND tc.test_case_id = tc_id
          AND tc.module = $module
        MERGE (td)-[:TD_FOR_TESTCASE]->(tc)
        RETURN count(*) AS cnt
        """

        if self.dry_run:
            logger.info("  [DRY-RUN] Would create TD_FOR_TESTCASE relationships")
            return

        result = self._read_tx(cypher, {"module": self.module})
        cnt = result[0]["cnt"] if result else 0
        self.stats["td_for_testcase_rels"] = cnt
        logger.info("  Created %d TD_FOR_TESTCASE relationships", cnt)

    def _create_td_uses_config(self):
        """Link TD_TestParameter → TD_Configuration based on config IDs in values."""
        logger.info("Creating TD_USES_CONFIG relationships …")

        # For each TD_TestParameter, parse config_values JSON to find config indices,
        # then link to the matching TD_Configuration node
        cypher = """
        MATCH (td:TD_TestParameter)
        WHERE td.module = $module AND td.config_values IS NOT NULL
        WITH td, td.config_values AS cv_json
        UNWIND keys(apoc.convert.fromJsonMap(cv_json)) AS config_key
        WITH td, config_key
        WHERE config_key =~ 'AS460_\\\\w+_C(\\\\d+)_P\\\\d+'
        WITH td, substring(config_key, size(config_key) - 6, 3) AS cfg_idx_raw
        WITH td, 
             CASE WHEN cfg_idx_raw =~ '\\\\d+' THEN cfg_idx_raw
                  ELSE substring(config_key, size(config_key) - 5, 3)
             END AS cfg_idx
        WITH td, DISTINCT cfg_idx
        MATCH (cfg:TD_Configuration {module: $module, config_index: cfg_idx})
        MERGE (td)-[:TD_USES_CONFIG]->(cfg)
        RETURN count(*) AS cnt
        """

        # The APOC approach is complex. Use a simpler Python-side approach:
        # Extract config indices from each parameter's config_values, batch create rels
        if self.dry_run:
            logger.info("  [DRY-RUN] Would create TD_USES_CONFIG relationships")
            return

        # Strategy: batch by config index
        # Get all TD_TestParameter UIDs + their config_values
        fetch_cypher = """
        MATCH (td:TD_TestParameter)
        WHERE td.module = $module AND td.config_values IS NOT NULL
        RETURN td.uid AS uid, td.config_values AS cv
        """
        results = self._read_tx(fetch_cypher, {"module": self.module})

        # Build pairs
        pairs = []  # (td_uid, config_index)
        for row in results:
            try:
                cv = json.loads(row["cv"])
            except (json.JSONDecodeError, TypeError):
                continue
            indices = set()
            for config_id in cv.keys():
                m = _CONFIG_ID_RE.search(config_id)
                if m:
                    indices.add(m.group(1).zfill(3))
            for idx in indices:
                pairs.append({"td_uid": row["uid"], "cfg_idx": idx})

        if not pairs:
            logger.info("  No config references found")
            return

        logger.info("  Found %d TD → Config pairs to link", len(pairs))

        # Batch create
        link_cypher = """
        UNWIND $batch AS pair
        MATCH (td:TD_TestParameter {uid: pair.td_uid})
        MATCH (cfg:TD_Configuration {module: $module, config_index: pair.cfg_idx})
        MERGE (td)-[:TD_USES_CONFIG]->(cfg)
        """

        for i in range(0, len(pairs), self.BATCH_SIZE):
            batch = pairs[i:i + self.BATCH_SIZE]
            self._write_tx(link_cypher, {"batch": batch, "module": self.module})

        self.stats["td_uses_config_pairs"] = len(pairs)

    # -- Main Build ---------------------------------------------------------

    def build(self):
        """Run the full ingestion pipeline."""
        t0 = time.time()

        print(f"{'='*60}")
        print(f"  TD KG Builder — module: {self.module}")
        print(f"  TD file: {self.xlsx_path.name}")
        print(f"  Project: {self.project}")
        print(f"  Dry run: {self.dry_run}")
        print(f"  Clear existing: {self.clear_module}")
        print(f"{'='*60}")

        # Step 1: Parse
        logger.info("Step 1: Parsing TD workbook …")
        data = parse_td_workbook(self.xlsx_path, self.module)

        print(f"  Parsed: {len(data['test_parameters'])} params, "
              f"{len(data['configurations'])} configs, "
              f"{len(data['hw_connections'])} hw_conns, "
              f"{len(data['interface_modes'])} iface_modes")

        if self.dry_run:
            elapsed = time.time() - t0
            self.stats["elapsed"] = f"{elapsed:.1f}s"
            self._print_summary()
            return

        # Step 2: Connect
        logger.info("Step 2: Connecting to Neo4j …")
        self._connect()

        try:
            # Step 3: Constraints
            logger.info("Step 3: Ensuring constraints …")
            self._ensure_constraints()

            # Step 4: Clear if requested
            if self.clear_module:
                logger.info("Step 4: Clearing existing data …")
                self._clear_existing()
            else:
                logger.info("Step 4: Clear skipped (--clear not set)")

            # Step 5: Ingest nodes
            logger.info("Step 5: Ingesting nodes …")
            self._ingest_configurations(data["configurations"])
            self._ingest_test_parameters(data["test_parameters"])
            self._ingest_hw_connections(data["hw_connections"])
            self._ingest_interface_modes(data["interface_modes"])

            # Step 6: Relationships
            logger.info("Step 6: Creating relationships …")
            self._create_td_for_testcase()
            self._create_td_uses_config()

        finally:
            self._close()

        elapsed = time.time() - t0
        self.stats["elapsed"] = f"{elapsed:.1f}s"
        self._print_summary()

    def _print_summary(self):
        print(f"\n{'='*60}")
        print(f"  TD Ingestion Summary — {self.module}")
        print(f"{'='*60}")
        for k, v in sorted(self.stats.items()):
            print(f"    {k}: {v}")
        print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_neo4j_config() -> dict:
    """Load Neo4j config from env/.env file (standalone CLI use)."""
    from dotenv import load_dotenv
    import os

    env_path = Path(__file__).resolve().parents[4] / "env" / ".env"
    if not env_path.exists():
        # Try alternate path
        env_path = Path(__file__).resolve().parents[3] / "env" / ".env"
    load_dotenv(env_path)

    return {
        "uri": os.getenv("NEO4J_URI_MCAL",
                         "bolt+ssc://bolt-passthrough-neo4j-ai-core-engine-mcal.icp.infineon.com:443"),
        "username": os.getenv("NEO4J_USER_MCAL", "neo4j"),
        "password": os.getenv("NEO4J_PASS_MCAL", "legato"),
        "database": os.getenv("NEO4J_DATABASE_MCAL", "neo4j"),
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    parser = argparse.ArgumentParser(description="Ingest Test Data into Neo4j")
    parser.add_argument("xlsx_path", help="Path to TD Excel file")
    parser.add_argument("--module", default="ETH_17_LETH", help="MCAL module name")
    parser.add_argument("--project", default="A3G", help="Project identifier")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true", help="Clear existing TD nodes first")
    args = parser.parse_args()

    neo4j_cfg = _load_neo4j_config()

    builder = TDKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        xlsx_path=args.xlsx_path,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_module=args.clear,
    )
    builder.build()
