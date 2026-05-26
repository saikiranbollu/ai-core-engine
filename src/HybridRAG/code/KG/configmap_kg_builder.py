"""
ConfigMap Knowledge Graph Builder
==================================

Ingests parsed ConfigMap data into Neo4j.

Node Types Created:
    - CM_DeviceVariant      — Hardware device/board variant (e.g., TC4D9_COM)

Relationships Created:
    - CM_RUNS_ON_DEVICE     (TD_Configuration → CM_DeviceVariant)
    - BVEC_TARGETS_DEVICE   (BVEC_InputParameter → CM_DeviceVariant)

Usage::

    from configmap_kg_builder import ConfigMapKnowledgeGraphBuilder

    builder = ConfigMapKnowledgeGraphBuilder(
        neo4j_cfg={"uri": "...", "username": "...", "password": "...", "database": "neo4j"},
        xlsx_path="path/to/ConfigMap.xlsx",
        module="ETH_17_LETH",
    )
    builder.build()
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

try:
    from .configmap_parser import parse_configmap_workbook, DEVICE_FAMILY_MAP, FAMILY_TO_VARIANTS
except ImportError:
    from configmap_parser import parse_configmap_workbook, DEVICE_FAMILY_MAP, FAMILY_TO_VARIANTS

logger = logging.getLogger("configmap_kg_builder")


class ConfigMapKnowledgeGraphBuilder:
    """Builds Neo4j knowledge graph from ConfigMap Excel."""

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

    # -- Build Pipeline -----------------------------------------------------

    def build(self):
        """Main entry point — parse Excel and ingest into Neo4j."""
        logger.info("=" * 60)
        logger.info("ConfigMap KG Builder — module=%s, project=%s", self.module, self.project)
        logger.info("  Source: %s", self.xlsx_path)
        logger.info("  Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        # Parse
        data = parse_configmap_workbook(self.xlsx_path, module=self.module)
        device_configs = data["device_configs"]
        device_variants = data["device_variants"]

        if not device_configs:
            logger.warning("No config entries found — nothing to ingest.")
            return

        # Connect
        if not self.dry_run:
            self._connect()

        try:
            # Optionally clear existing CM data for this module
            if self.clear_module and not self.dry_run:
                self._clear_module_data()

            # Ensure constraints
            self._ensure_constraints()

            # 1. Create CM_DeviceVariant nodes
            self._ingest_device_variants(device_variants)

            # 2. Create CM_RUNS_ON_DEVICE relationships
            self._create_runs_on_device_rels(device_configs)

            # 3. Create BVEC_TARGETS_DEVICE relationships
            self._create_bvec_targets_device_rels(device_variants)

            # Report
            self._report_stats()

        finally:
            self._close()

    # -- Clear existing data ------------------------------------------------

    def _clear_module_data(self):
        """Remove existing CM nodes and relationships for this module."""
        logger.info("Clearing existing CM data for module=%s …", self.module)

        # Delete relationships first
        self._write_tx("""
            MATCH (c:TD_Configuration {module: $module})-[r:CM_RUNS_ON_DEVICE]->()
            DELETE r
        """, {"module": self.module})

        self._write_tx("""
            MATCH (b:BVEC_InputParameter {module: $module})-[r:BVEC_TARGETS_DEVICE]->()
            DELETE r
        """, {"module": self.module})

        # Delete device variant nodes for this module
        self._write_tx("""
            MATCH (d:CM_DeviceVariant {module: $module})
            DETACH DELETE d
        """, {"module": self.module})

        logger.info("Cleared existing CM data.")

    # -- Constraints --------------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for CM nodes."""
        constraints = [
            ("cm_device_uid", "CM_DeviceVariant", "uid"),
        ]
        for name, label, prop in constraints:
            cypher = (
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            self._write_tx(cypher)

    # -- Ingest CM_DeviceVariant nodes --------------------------------------

    def _ingest_device_variants(self, device_variants: list[str]):
        """Create CM_DeviceVariant nodes for each unique device."""
        logger.info("Ingesting %d CM_DeviceVariant nodes …", len(device_variants))

        cypher = """
        UNWIND $batch AS entry
        MERGE (d:CM_DeviceVariant {uid: entry.uid})
        SET d.name = entry.name,
            d.device_family = entry.device_family,
            d.module = entry.module,
            d.project = $project
        """

        nodes = []
        for dev in device_variants:
            uid = f"{self.module}_DEV_{dev}"
            family = DEVICE_FAMILY_MAP.get(dev, "UNKNOWN")
            nodes.append({
                "uid": uid,
                "name": dev,
                "device_family": family,
                "module": self.module,
            })

        self._write_tx(cypher, {"batch": nodes, "project": self.project})
        self.stats["CM_DeviceVariant_nodes"] += len(nodes)

        if self.dry_run:
            for n in nodes:
                logger.info("  [DRY] CM_DeviceVariant: %s (family=%s)", n["name"], n["device_family"])

    # -- CM_RUNS_ON_DEVICE relationships ------------------------------------

    def _create_runs_on_device_rels(self, device_configs: list[dict]):
        """Create CM_RUNS_ON_DEVICE relationships: TD_Configuration → CM_DeviceVariant.

        Uses the config_name from ConfigMap to match TD_Configuration.config_file_name.
        """
        logger.info("Creating CM_RUNS_ON_DEVICE relationships …")

        # Build pairs: (config_name, device_name)
        pairs = []
        for entry in device_configs:
            config_name = entry["config_name"]
            for dev in entry["devices"]:
                pairs.append({"config_name": config_name, "device_uid": f"{self.module}_DEV_{dev}"})

        if not pairs:
            logger.warning("No config-device pairs to link.")
            return

        cypher = """
        UNWIND $batch AS pair
        MATCH (cfg:TD_Configuration {module: $module, config_file_name: pair.config_name})
        MATCH (dev:CM_DeviceVariant {uid: pair.device_uid})
        MERGE (cfg)-[:CM_RUNS_ON_DEVICE]->(dev)
        """

        for i in range(0, len(pairs), self.BATCH_SIZE):
            batch = pairs[i:i + self.BATCH_SIZE]
            self._write_tx(cypher, {"batch": batch, "module": self.module})
            self.stats["CM_RUNS_ON_DEVICE_rels"] += len(batch)

        logger.info("  Created %d CM_RUNS_ON_DEVICE relationship pairs.", len(pairs))

        if self.dry_run:
            logger.info("  [DRY] Would create %d CM_RUNS_ON_DEVICE relationships", len(pairs))

    # -- BVEC_TARGETS_DEVICE relationships ----------------------------------

    def _create_bvec_targets_device_rels(self, device_variants: list[str]):
        """Create BVEC_TARGETS_DEVICE relationships: BVEC_InputParameter → CM_DeviceVariant.

        Maps BVEC's family-level device (e.g., TC4DX) to specific device variants.
        """
        logger.info("Creating BVEC_TARGETS_DEVICE relationships …")

        # Build family→variant UIDs mapping for this module's devices
        family_to_uids = {}
        for dev in device_variants:
            family = DEVICE_FAMILY_MAP.get(dev, "")
            if family:
                family_to_uids.setdefault(family, []).append(f"{self.module}_DEV_{dev}")

        # For each BVEC family, link BVEC_InputParameter nodes to matching CM_DeviceVariant(s)
        # BVEC uses: TC4DX, TC49X, TC45X, TC48X
        cypher = """
        MATCH (b:BVEC_InputParameter {module: $module, device: $bvec_family})
        MATCH (d:CM_DeviceVariant {uid: $device_uid})
        MERGE (b)-[:BVEC_TARGETS_DEVICE]->(d)
        RETURN count(*) AS cnt
        """

        total_rels = 0
        for family, dev_uids in family_to_uids.items():
            for dev_uid in dev_uids:
                if self.dry_run:
                    logger.info("  [DRY] BVEC family=%s → device=%s", family, dev_uid)
                    continue
                result = self._read_tx("""
                    MATCH (b:BVEC_InputParameter {module: $module, device: $bvec_family})
                    RETURN count(b) AS cnt
                """, {"module": self.module, "bvec_family": family})
                bvec_count = result[0]["cnt"] if result else 0
                if bvec_count > 0:
                    self._write_tx(cypher, {
                        "module": self.module,
                        "bvec_family": family,
                        "device_uid": dev_uid,
                    })
                    total_rels += bvec_count
                    logger.info("  Linked %d BVEC nodes (family=%s) → %s",
                                bvec_count, family, dev_uid)

        self.stats["BVEC_TARGETS_DEVICE_rels"] += total_rels
        logger.info("  Total BVEC_TARGETS_DEVICE relationships: %d", total_rels)

    # -- Stats & Reporting --------------------------------------------------

    def _report_stats(self):
        """Print final statistics."""
        logger.info("=" * 60)
        logger.info("ConfigMap KG Build Complete — Statistics:")
        for key, val in sorted(self.stats.items()):
            logger.info("  %-35s %d", key, val)
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import yaml
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from env_config import load_yaml_with_env

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Ingest ConfigMap into Neo4j KG")
    parser.add_argument("xlsx", help="Path to ConfigMap Excel file")
    parser.add_argument("--module", default="ETH_17_LETH", help="Module name")
    parser.add_argument("--project", default="A3G", help="Project name")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true", help="Clear existing CM data first")
    parser.add_argument("--profile", default="mcal", help="Storage config profile")
    args = parser.parse_args()

    # Load Neo4j config from storage_config.yaml
    config_path = Path(__file__).resolve().parents[2] / "config" / "storage_config.yaml"
    if config_path.exists():
        storage_cfg = load_yaml_with_env(config_path)
        neo4j_cfg = storage_cfg.get("neo4j", {}).get(args.profile, {})
    else:
        print(f"ERROR: storage_config.yaml not found at {config_path}")
        sys.exit(1)

    if not neo4j_cfg:
        print(f"ERROR: Profile '{args.profile}' not found in storage_config.yaml")
        sys.exit(1)

    builder = ConfigMapKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        xlsx_path=args.xlsx,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_module=args.clear,
    )
    builder.build()
