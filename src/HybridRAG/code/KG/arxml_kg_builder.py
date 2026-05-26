#!/usr/bin/env python3
"""
ARXML ECUC Configuration Knowledge Graph Builder
=================================================

Ingests AUTOSAR ECUC configuration .arxml files into Neo4j.

Node Types Created:
    - ARXML_Module     — Top-level ECUC module configuration
    - ARXML_Container  — ECUC container at any nesting depth
    - ARXML_Parameter  — ECUC parameter value (numerical or textual)
    - ARXML_Reference  — ECUC reference value (cross-link to other container)

Relationships Created:
    - ARXML_HAS_CONTAINER      (ARXML_Module → ARXML_Container)
    - ARXML_HAS_SUB_CONTAINER  (ARXML_Container → ARXML_Container)
    - ARXML_HAS_PARAMETER      (ARXML_Container → ARXML_Parameter)
    - ARXML_HAS_REFERENCE      (ARXML_Container → ARXML_Reference)
    - ARXML_INSTANCE_OF        (ARXML_Parameter → EA_ConfigParameter)
    - ARXML_TESTED_BY_BVEC     (ARXML_Parameter → BVEC_ConfigParameter)
    - ARXML_USED_IN_CONFIG     (TD_Configuration → ARXML_Module)

Usage::

    # Ingest all ARXML files from a device directory
    python arxml_kg_builder.py temp/arxml/repos/.../TC489_COM \\
        --device TC489_COM --module ETH_17_LETH --profile mcal

    # Dry-run (parse only, no Neo4j writes)
    python arxml_kg_builder.py temp/arxml/repos/.../TC489_COM \\
        --device TC489_COM --module ETH_17_LETH --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

# Add parent dirs to path for env_config import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_config import load_yaml_with_env

logger = logging.getLogger("arxml_kg_builder")

# AUTOSAR R4.0 namespace
NS = "{http://autosar.org/schema/r4.0}"

# Regex to extract config index from filename (e.g., Eth_Leth_021.arxml → 021)
CONFIG_INDEX_RE = re.compile(r"_(\d{3})\.arxml$")


# ── ARXML Parser ──────────────────────────────────────────────────────────────

def parse_arxml_file(filepath: Path) -> Optional[dict]:
    """Parse a single ARXML file and extract ECUC hierarchy.

    Returns dict with keys: module_name, containers, parameters, references
    or None if no ECUC-MODULE-CONFIGURATION-VALUES found.
    """
    try:
        tree = ET.parse(filepath)
    except ET.ParseError as exc:
        logger.warning("XML parse error in %s: %s", filepath.name, exc)
        return None

    root = tree.getroot()

    # Find ECUC-MODULE-CONFIGURATION-VALUES
    module_elem = root.find(f".//{NS}ECUC-MODULE-CONFIGURATION-VALUES")
    if module_elem is None:
        logger.debug("No ECUC-MODULE-CONFIGURATION-VALUES in %s", filepath.name)
        return None

    module_name = _get_short_name(module_elem)
    if not module_name:
        return None

    containers = []
    parameters = []
    references = []

    # Process top-level CONTAINERS
    containers_elem = module_elem.find(f"{NS}CONTAINERS")
    if containers_elem is not None:
        for container_elem in containers_elem.findall(f"{NS}ECUC-CONTAINER-VALUE"):
            _walk_container(
                container_elem,
                parent_path=module_name,
                depth=1,
                containers=containers,
                parameters=parameters,
                references=references,
            )

    return {
        "module_name": module_name,
        "containers": containers,
        "parameters": parameters,
        "references": references,
    }


def _get_short_name(elem) -> Optional[str]:
    """Extract SHORT-NAME text from an element."""
    sn = elem.find(f"{NS}SHORT-NAME")
    if sn is not None and sn.text:
        return sn.text.strip()
    return None


def _get_definition_ref(elem) -> Optional[str]:
    """Extract DEFINITION-REF text from an element."""
    dr = elem.find(f"{NS}DEFINITION-REF")
    if dr is not None and dr.text:
        return dr.text.strip()
    return None


def _walk_container(
    elem,
    parent_path: str,
    depth: int,
    containers: list,
    parameters: list,
    references: list,
):
    """Recursively walk an ECUC-CONTAINER-VALUE and collect all data."""
    name = _get_short_name(elem)
    if not name:
        return

    path = f"{parent_path}/{name}"
    definition_ref = _get_definition_ref(elem)

    containers.append({
        "name": name,
        "path": path,
        "depth": depth,
        "definition_ref": definition_ref,
    })

    # Extract PARAMETER-VALUES
    param_values_elem = elem.find(f"{NS}PARAMETER-VALUES")
    if param_values_elem is not None:
        # Numerical parameters
        for pv in param_values_elem.findall(f"{NS}ECUC-NUMERICAL-PARAM-VALUE"):
            _extract_parameter(pv, path, "numerical", parameters)
        # Textual parameters
        for pv in param_values_elem.findall(f"{NS}ECUC-TEXTUAL-PARAM-VALUE"):
            _extract_parameter(pv, path, "textual", parameters)

    # Extract REFERENCE-VALUES
    ref_values_elem = elem.find(f"{NS}REFERENCE-VALUES")
    if ref_values_elem is not None:
        for rv in ref_values_elem.findall(f"{NS}ECUC-REFERENCE-VALUE"):
            _extract_reference(rv, path, references)

    # Recurse into SUB-CONTAINERS
    sub_containers_elem = elem.find(f"{NS}SUB-CONTAINERS")
    if sub_containers_elem is not None:
        for sub_elem in sub_containers_elem.findall(f"{NS}ECUC-CONTAINER-VALUE"):
            _walk_container(
                sub_elem,
                parent_path=path,
                depth=depth + 1,
                containers=containers,
                parameters=parameters,
                references=references,
            )


def _extract_parameter(elem, container_path: str, value_type: str, parameters: list):
    """Extract a parameter value from ECUC-NUMERICAL/TEXTUAL-PARAM-VALUE."""
    definition_ref = _get_definition_ref(elem)
    if not definition_ref:
        return

    # Parameter name = last segment of DEFINITION-REF
    name = definition_ref.rsplit("/", 1)[-1]

    value_elem = elem.find(f"{NS}VALUE")
    value = value_elem.text.strip() if (value_elem is not None and value_elem.text) else ""

    parameters.append({
        "name": name,
        "value": value,
        "value_type": value_type,
        "container_path": container_path,
        "definition_ref": definition_ref,
    })


def _extract_reference(elem, container_path: str, references: list):
    """Extract a reference value from ECUC-REFERENCE-VALUE."""
    definition_ref = _get_definition_ref(elem)
    if not definition_ref:
        return

    # Reference name = last segment of DEFINITION-REF
    name = definition_ref.rsplit("/", 1)[-1]

    value_ref_elem = elem.find(f"{NS}VALUE-REF")
    value_ref = value_ref_elem.text.strip() if (value_ref_elem is not None and value_ref_elem.text) else ""

    references.append({
        "name": name,
        "value_ref": value_ref,
        "container_path": container_path,
        "definition_ref": definition_ref,
    })


def extract_config_index(filename: str) -> str:
    """Extract the numeric config index from an ARXML filename.

    e.g. 'Eth_Leth_021.arxml' → '021', 'Dem_001.arxml' → '001'
    """
    m = CONFIG_INDEX_RE.search(filename)
    return m.group(1) if m else "000"


# ── KG Builder ────────────────────────────────────────────────────────────────

class ArxmlKGBuilder:
    """Builds Neo4j knowledge graph from parsed ARXML ECUC configuration files."""

    BATCH_SIZE = 500

    def __init__(
        self,
        neo4j_cfg: dict,
        arxml_dir: str | Path,
        device: str,
        module: str,
        *,
        project: str = "A3G",
        dry_run: bool = False,
        clear_device: bool = False,
        cross_link: bool = True,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.arxml_dir = Path(arxml_dir)
        self.device = device.upper()
        self.module = module.upper()
        self.project = project
        self.dry_run = dry_run
        self.clear_device = clear_device
        self.cross_link = cross_link
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

    def _read_tx(self, cypher: str, parameters: Optional[dict] = None) -> list:
        """Execute a read transaction and return list of records."""
        if self.dry_run:
            return []
        db = self.neo4j_cfg["database"]
        with self._driver.session(database=db) as session:
            result = session.execute_read(
                lambda tx: tx.run(cypher, parameters or {}).data()
            )
            return result

    # -- Build Pipeline -----------------------------------------------------

    def build(self):
        """Main entry point — parse all ARXML files and ingest into Neo4j."""
        logger.info("=" * 60)
        logger.info("ARXML ECUC KG Builder")
        logger.info("  Device: %s | Module: %s | Project: %s",
                    self.device, self.module, self.project)
        logger.info("  Source dir: %s", self.arxml_dir)
        logger.info("  Dry run: %s | Cross-link: %s",
                    self.dry_run, self.cross_link)
        logger.info("=" * 60)

        start = time.perf_counter()

        # Discover ARXML files
        arxml_files = sorted(self.arxml_dir.glob("*.arxml"))
        if not arxml_files:
            logger.warning("No .arxml files found in %s", self.arxml_dir)
            return

        logger.info("Found %d .arxml files", len(arxml_files))

        # Parse all files
        all_data = []
        for fpath in arxml_files:
            parsed = parse_arxml_file(fpath)
            if parsed:
                config_index = extract_config_index(fpath.name)
                parsed["source_file"] = fpath.name
                parsed["config_index"] = config_index
                all_data.append(parsed)

        logger.info("Parsed %d files with ECUC module data", len(all_data))
        total_containers = sum(len(d["containers"]) for d in all_data)
        total_params = sum(len(d["parameters"]) for d in all_data)
        total_refs = sum(len(d["references"]) for d in all_data)
        logger.info("  Total containers: %d, parameters: %d, references: %d",
                    total_containers, total_params, total_refs)

        if not all_data:
            logger.warning("No ECUC data found — nothing to ingest.")
            return

        # Connect
        if not self.dry_run:
            self._connect()

        try:
            if self.clear_device and not self.dry_run:
                self._clear_device_data()

            self._ensure_constraints()

            # Ingest each file
            for file_data in all_data:
                self._ingest_file(file_data)

            # Cross-link to existing KG nodes
            if self.cross_link and not self.dry_run:
                self._create_cross_links()

            # Report
            elapsed = time.perf_counter() - start
            self._report_stats(elapsed)

        finally:
            self._close()

    # -- Clear existing data ------------------------------------------------

    def _clear_device_data(self):
        """Remove existing ARXML nodes for this device."""
        logger.info("Clearing existing ARXML data for device=%s …", self.device)

        for label in ["ARXML_Reference", "ARXML_Parameter", "ARXML_Container", "ARXML_Module"]:
            self._write_tx(f"""
                MATCH (n:{label} {{device: $device}})
                DETACH DELETE n
            """, {"device": self.device})

        logger.info("Cleared existing ARXML data for %s.", self.device)

    # -- Constraints --------------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for ARXML nodes."""
        constraints = [
            ("arxml_module_uid", "ARXML_Module", "uid"),
            ("arxml_container_uid", "ARXML_Container", "uid"),
            ("arxml_parameter_uid", "ARXML_Parameter", "uid"),
            ("arxml_reference_uid", "ARXML_Reference", "uid"),
        ]
        for name, label, prop in constraints:
            cypher = (
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            self._write_tx(cypher)

    # -- Ingest a single file -----------------------------------------------

    def _ingest_file(self, file_data: dict):
        """Ingest all data from one parsed ARXML file."""
        module_name = file_data["module_name"]
        config_index = file_data["config_index"]
        source_file = file_data["source_file"]

        logger.info("  Ingesting %s (module=%s, index=%s) …",
                    source_file, module_name, config_index)

        # 1. ARXML_Module node
        module_uid = f"ARXML_{self.device}_{module_name}_{config_index}"
        self._write_tx("""
            MERGE (m:ARXML_Module {uid: $uid})
            SET m.name = $name,
                m.device = $device,
                m.config_index = $config_index,
                m.source_file = $source_file,
                m.module = $module,
                m.project = $project
        """, {
            "uid": module_uid,
            "name": module_name,
            "device": self.device,
            "config_index": config_index,
            "source_file": source_file,
            "module": self.module,
            "project": self.project,
        })
        self.stats["ARXML_Module"] += 1

        # 2. ARXML_Container nodes (batched)
        containers = file_data["containers"]
        if containers:
            self._ingest_containers(containers, module_uid, config_index)

        # 3. ARXML_Parameter nodes (batched)
        parameters = file_data["parameters"]
        if parameters:
            self._ingest_parameters(parameters, config_index)

        # 4. ARXML_Reference nodes (batched)
        references = file_data["references"]
        if references:
            self._ingest_references(references, config_index)

    # -- ARXML_Container nodes + relationships ------------------------------

    def _ingest_containers(self, containers: list, module_uid: str, config_index: str):
        """Create ARXML_Container nodes and structure relationships."""
        # Batch create container nodes
        batch = []
        for c in containers:
            uid = f"ARXML_{self.device}_{config_index}:{c['path']}"
            batch.append({
                "uid": uid,
                "name": c["name"],
                "path": c["path"],
                "depth": c["depth"],
                "definition_ref": c.get("definition_ref", ""),
            })

        for i in range(0, len(batch), self.BATCH_SIZE):
            chunk = batch[i:i + self.BATCH_SIZE]
            self._write_tx("""
                UNWIND $batch AS c
                MERGE (n:ARXML_Container {uid: c.uid})
                SET n.name = c.name,
                    n.path = c.path,
                    n.depth = c.depth,
                    n.definition_ref = c.definition_ref,
                    n.device = $device,
                    n.config_index = $config_index,
                    n.module = $module
            """, {
                "batch": chunk,
                "device": self.device,
                "config_index": config_index,
                "module": self.module,
            })

        self.stats["ARXML_Container"] += len(batch)

        # Create ARXML_HAS_CONTAINER (Module → depth=1 containers)
        depth1 = [c for c in containers if c["depth"] == 1]
        if depth1:
            depth1_uids = [f"ARXML_{self.device}_{config_index}:{c['path']}" for c in depth1]
            self._write_tx("""
                MATCH (m:ARXML_Module {uid: $module_uid})
                UNWIND $container_uids AS cuid
                MATCH (c:ARXML_Container {uid: cuid})
                MERGE (m)-[:ARXML_HAS_CONTAINER]->(c)
            """, {
                "module_uid": module_uid,
                "container_uids": depth1_uids,
            })
            self.stats["ARXML_HAS_CONTAINER"] += len(depth1_uids)

        # Create ARXML_HAS_SUB_CONTAINER (parent → child) for depth > 1
        deeper = [c for c in containers if c["depth"] > 1]
        if deeper:
            parent_child_pairs = []
            for c in deeper:
                # parent_path = everything up to last '/'
                parent_path = c["path"].rsplit("/", 1)[0]
                parent_uid = f"ARXML_{self.device}_{config_index}:{parent_path}"
                child_uid = f"ARXML_{self.device}_{config_index}:{c['path']}"
                parent_child_pairs.append({"parent_uid": parent_uid, "child_uid": child_uid})

            for i in range(0, len(parent_child_pairs), self.BATCH_SIZE):
                chunk = parent_child_pairs[i:i + self.BATCH_SIZE]
                self._write_tx("""
                    UNWIND $pairs AS p
                    MATCH (parent:ARXML_Container {uid: p.parent_uid})
                    MATCH (child:ARXML_Container {uid: p.child_uid})
                    MERGE (parent)-[:ARXML_HAS_SUB_CONTAINER]->(child)
                """, {"pairs": chunk})

            self.stats["ARXML_HAS_SUB_CONTAINER"] += len(parent_child_pairs)

    # -- ARXML_Parameter nodes + relationships ------------------------------

    def _ingest_parameters(self, parameters: list, config_index: str):
        """Create ARXML_Parameter nodes and ARXML_HAS_PARAMETER relationships."""
        batch = []
        for p in parameters:
            uid = f"ARXML_{self.device}_{config_index}:{p['container_path']}/{p['name']}"
            container_uid = f"ARXML_{self.device}_{config_index}:{p['container_path']}"
            batch.append({
                "uid": uid,
                "name": p["name"],
                "value": p["value"],
                "value_type": p["value_type"],
                "container_path": p["container_path"],
                "definition_ref": p.get("definition_ref", ""),
                "container_uid": container_uid,
            })

        for i in range(0, len(batch), self.BATCH_SIZE):
            chunk = batch[i:i + self.BATCH_SIZE]
            self._write_tx("""
                UNWIND $batch AS p
                MERGE (n:ARXML_Parameter {uid: p.uid})
                SET n.name = p.name,
                    n.value = p.value,
                    n.value_type = p.value_type,
                    n.container_path = p.container_path,
                    n.definition_ref = p.definition_ref,
                    n.device = $device,
                    n.config_index = $config_index
            """, {
                "batch": chunk,
                "device": self.device,
                "config_index": config_index,
            })

        self.stats["ARXML_Parameter"] += len(batch)

        # Create ARXML_HAS_PARAMETER relationships
        for i in range(0, len(batch), self.BATCH_SIZE):
            chunk = batch[i:i + self.BATCH_SIZE]
            self._write_tx("""
                UNWIND $batch AS p
                MATCH (c:ARXML_Container {uid: p.container_uid})
                MATCH (param:ARXML_Parameter {uid: p.uid})
                MERGE (c)-[:ARXML_HAS_PARAMETER]->(param)
            """, {"batch": chunk})

        self.stats["ARXML_HAS_PARAMETER"] += len(batch)

    # -- ARXML_Reference nodes + relationships ------------------------------

    def _ingest_references(self, references: list, config_index: str):
        """Create ARXML_Reference nodes and ARXML_HAS_REFERENCE relationships."""
        batch = []
        for r in references:
            uid = f"ARXML_{self.device}_{config_index}:{r['container_path']}/{r['name']}"
            container_uid = f"ARXML_{self.device}_{config_index}:{r['container_path']}"
            batch.append({
                "uid": uid,
                "name": r["name"],
                "value_ref": r["value_ref"],
                "container_path": r["container_path"],
                "definition_ref": r.get("definition_ref", ""),
                "container_uid": container_uid,
            })

        for i in range(0, len(batch), self.BATCH_SIZE):
            chunk = batch[i:i + self.BATCH_SIZE]
            self._write_tx("""
                UNWIND $batch AS r
                MERGE (n:ARXML_Reference {uid: r.uid})
                SET n.name = r.name,
                    n.value_ref = r.value_ref,
                    n.container_path = r.container_path,
                    n.definition_ref = r.definition_ref,
                    n.device = $device,
                    n.config_index = $config_index
            """, {
                "batch": chunk,
                "device": self.device,
                "config_index": config_index,
            })

        self.stats["ARXML_Reference"] += len(batch)

        # Create ARXML_HAS_REFERENCE relationships
        for i in range(0, len(batch), self.BATCH_SIZE):
            chunk = batch[i:i + self.BATCH_SIZE]
            self._write_tx("""
                UNWIND $batch AS r
                MATCH (c:ARXML_Container {uid: r.container_uid})
                MATCH (ref:ARXML_Reference {uid: r.uid})
                MERGE (c)-[:ARXML_HAS_REFERENCE]->(ref)
            """, {"batch": chunk})

        self.stats["ARXML_HAS_REFERENCE"] += len(batch)

    # -- Cross-Links --------------------------------------------------------

    def _create_cross_links(self):
        """Create cross-links to existing KG nodes (EA, BVEC, TD)."""
        logger.info("Creating cross-links to existing KG nodes …")

        # ARXML_INSTANCE_OF: ARXML_Parameter → EA_ConfigParameter
        # Match by parameter name
        result = self._read_tx("""
            MATCH (p:ARXML_Parameter {device: $device})
            MATCH (ea:EA_ConfigParameter)
            WHERE p.name = ea.name
            RETURN count(*) AS cnt
        """, {"device": self.device})

        if result and result[0]["cnt"] > 0:
            self._write_tx("""
                MATCH (p:ARXML_Parameter {device: $device})
                MATCH (ea:EA_ConfigParameter)
                WHERE p.name = ea.name
                MERGE (p)-[:ARXML_INSTANCE_OF]->(ea)
            """, {"device": self.device})
            self.stats["ARXML_INSTANCE_OF"] = result[0]["cnt"]
            logger.info("  ARXML_INSTANCE_OF: %d links", result[0]["cnt"])

        # ARXML_TESTED_BY_BVEC: ARXML_Parameter → BVEC_ConfigParameter
        # Match by parameter name
        result = self._read_tx("""
            MATCH (p:ARXML_Parameter {device: $device})
            MATCH (bv:BVEC_ConfigParameter)
            WHERE p.name = bv.parameter_name
            RETURN count(*) AS cnt
        """, {"device": self.device})

        if result and result[0]["cnt"] > 0:
            self._write_tx("""
                MATCH (p:ARXML_Parameter {device: $device})
                MATCH (bv:BVEC_ConfigParameter)
                WHERE p.name = bv.parameter_name
                MERGE (p)-[:ARXML_TESTED_BY_BVEC]->(bv)
            """, {"device": self.device})
            self.stats["ARXML_TESTED_BY_BVEC"] = result[0]["cnt"]
            logger.info("  ARXML_TESTED_BY_BVEC: %d links", result[0]["cnt"])

        # ARXML_USED_IN_CONFIG: TD_Configuration → ARXML_Module
        # Match TD configs whose config_file_name matches our config_index
        result = self._read_tx("""
            MATCH (m:ARXML_Module {device: $device})
            MATCH (td:TD_Configuration)
            WHERE td.config_index = m.config_index
            RETURN count(*) AS cnt
        """, {"device": self.device})

        if result and result[0]["cnt"] > 0:
            self._write_tx("""
                MATCH (m:ARXML_Module {device: $device})
                MATCH (td:TD_Configuration)
                WHERE td.config_index = m.config_index
                MERGE (td)-[:ARXML_USED_IN_CONFIG]->(m)
            """, {"device": self.device})
            self.stats["ARXML_USED_IN_CONFIG"] = result[0]["cnt"]
            logger.info("  ARXML_USED_IN_CONFIG: %d links", result[0]["cnt"])

    # -- Reporting ----------------------------------------------------------

    def _report_stats(self, elapsed: float):
        """Print summary statistics."""
        logger.info("=" * 60)
        logger.info("ARXML KG Build Complete (%.1fs)", elapsed)
        logger.info("-" * 40)
        for key, count in sorted(self.stats.items()):
            logger.info("  %-30s %6d", key, count)
        total_nodes = (
            self.stats["ARXML_Module"]
            + self.stats["ARXML_Container"]
            + self.stats["ARXML_Parameter"]
            + self.stats["ARXML_Reference"]
        )
        total_rels = sum(v for k, v in self.stats.items() if "HAS_" in k or "INSTANCE" in k or "TESTED" in k or "USED" in k)
        logger.info("-" * 40)
        logger.info("  Total nodes: %d | Total relationships: %d", total_nodes, total_rels)
        logger.info("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Ingest ARXML ECUC configuration files into Neo4j KG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python arxml_kg_builder.py temp/arxml/repos/.../TC489_COM "
            "--device TC489_COM --module ETH_17_LETH --profile mcal\n"
            "  python arxml_kg_builder.py temp/arxml/repos/.../TC489_COM "
            "--device TC489_COM --module ETH_17_LETH --dry-run\n"
        ),
    )
    parser.add_argument("arxml_dir", help="Directory containing .arxml files")
    parser.add_argument("--device", required=True,
                        help="Device variant name (e.g., TC489_COM)")
    parser.add_argument("--module", required=True,
                        help="MCAL module identifier (e.g., ETH_17_LETH)")
    parser.add_argument("--project", default="A3G",
                        help="Project tag (default: A3G)")
    parser.add_argument("--profile", default="mcal",
                        help="Storage config profile (default: mcal)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing ARXML data for this device first")
    parser.add_argument("--no-cross-link", action="store_true",
                        help="Skip cross-linking to EA/BVEC/TD nodes")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load Neo4j config from storage_config.yaml
    config_path = Path(__file__).resolve().parents[2] / "config" / "storage_config.yaml"
    if not config_path.exists():
        print(f"ERROR: storage_config.yaml not found at {config_path}")
        sys.exit(1)

    storage_cfg = load_yaml_with_env(config_path)
    neo4j_cfg = storage_cfg.get("neo4j", {}).get(args.profile, {})

    if not neo4j_cfg:
        print(f"ERROR: Profile '{args.profile}' not found in storage_config.yaml")
        sys.exit(1)

    arxml_dir = Path(args.arxml_dir)
    if not arxml_dir.exists():
        print(f"ERROR: Directory not found: {arxml_dir}")
        sys.exit(1)

    builder = ArxmlKGBuilder(
        neo4j_cfg=neo4j_cfg,
        arxml_dir=arxml_dir,
        device=args.device,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_device=args.clear,
        cross_link=not args.no_cross_link,
    )
    builder.build()


if __name__ == "__main__":
    main()
