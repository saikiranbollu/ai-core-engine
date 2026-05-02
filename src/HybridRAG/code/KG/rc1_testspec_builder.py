#!/usr/bin/env python3
"""
RC1 Test Specification Knowledge Graph Builder
================================================

Builds TS_* (Test Specification) nodes and relationships in the Neo4j
knowledge graph from Polarion-sourced test specification data.

This is the RC1 counterpart of A3G's ``TestSpecKnowledgeGraphBuilder``
(which reads Excel files).  This builder reads JSON produced by
``fetch_polarion_testspec.py`` and creates the same ontology-compatible
node types and relationships.

Node types created:
    - TS_FunctionalTestCase      (from ifxITSTTestcase)
    - TS_ConfigTestCase          (from ifxConfigurationTestcase)
    - TS_StaticInterfaceTestCase (from ifxStaticTestcase)
    - TS_TestSpecDocument        (synthetic document-level node)

Relationships created:
    - TS_VERIFIES                (test case → ProductRequirement via ifxVerify links)
    - TS_VALIDATES_EA            (test case → EA_* nodes via text-mention matching)
    - TS_TESTS_CONFIG_ELEMENT    (config test → EA_ConfigContainer/Parameter)
    - TS_BELONGS_TO_MODULE       (test case → MCALModule)
    - TS_CONTAINS_TESTCASE       (TS_TestSpecDocument → test cases)
    - IMPLEMENTS                 (V-Model bridge: PRQ → EA)
    - TRACES_TO                  (V-Model bridge: EA → TS)

Usage::

    python rc1_testspec_builder.py --module GPT --profile test --dry-run
    python rc1_testspec_builder.py --module GPT --profile test
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

PROJECT = "RC1"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("rc1_testspec_builder")


# ---------------------------------------------------------------------------
# Polarion type → Neo4j label mapping
# ---------------------------------------------------------------------------
POLARION_TC_TYPE_MAP = {
    "ifxITSTTestcase": "TS_FunctionalTestCase",
    "ifxConfigurationTestcase": "TS_ConfigTestCase",
    "ifxStaticTestcase": "TS_StaticInterfaceTestCase",
}

# Unique-key property for each TS node type
UID_MAP = {
    "TS_FunctionalTestCase":      "test_case_id",
    "TS_ConfigTestCase":          "test_case_id",
    "TS_StaticInterfaceTestCase": "test_case_id",
    "TS_TestSpecDocument":        "document_name",
}


# ---------------------------------------------------------------------------
# Polarion item → Neo4j property mapping
# ---------------------------------------------------------------------------
def _strip_html(text: Optional[str]) -> Optional[str]:
    """Strip HTML tags from text."""
    if not text:
        return text
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
        def handle_data(self, data: str):
            self._parts.append(data)
        def get_text(self) -> str:
            return "".join(self._parts).strip()

    s = _Stripper()
    s.feed(str(text))
    return s.get_text()


def map_functional_testcase(item: dict, module: str) -> dict:
    """Map a Polarion ifxITSTTestcase to TS_FunctionalTestCase properties."""
    raw = item.get("raw_fields", {})
    return {
        "test_case_id": item.get("id", ""),
        "name": item.get("title", ""),
        "test_objective": _strip_html(raw.get("ifxTestObjective", "")) or "",
        "configuration_plan": _strip_html(raw.get("ifxConfigurationPlan", "")) or "",
        "automation_method": raw.get("ifxIsAutomated", ""),
        "verification_method": raw.get("ifxVerificationMethod", ""),
        "maturity_level": raw.get("ifxMaturityLevel", ""),
        "test_method": raw.get("ifxTestMethod", ""),
        "test_variant": raw.get("ifxTestVariant", ""),
        "additional_info": _strip_html(raw.get("ifxAdditionalInformation", "")) or "",
        "description": _strip_html(item.get("description_html", "")) or item.get("description", ""),
        "status": item.get("status", ""),
        "outline_number": item.get("outline_number", ""),
        "module": module.upper(),
        "project": PROJECT,
        "source": "polarion",
        "polarion_type": "ifxITSTTestcase",
        "source_document": f"polarion_testspec_{module.lower()}",
    }


def map_config_testcase(item: dict, module: str) -> dict:
    """Map a Polarion ifxConfigurationTestcase to TS_ConfigTestCase properties."""
    raw = item.get("raw_fields", {})
    return {
        "test_case_id": item.get("id", ""),
        "name": item.get("title", ""),
        "test_procedure": _strip_html(raw.get("ifxTestProcedure", "")) or "",
        "expected_results": _strip_html(raw.get("ifxExpectedBehaviour", "")) or "",
        "config_path": _strip_html(raw.get("ifxArchitectureInformation", "")) or "",
        "automation_method": raw.get("ifxIsAutomated", ""),
        "test_design_technique": raw.get("ifxTestDesignTechnique", ""),
        "verification_method": raw.get("ifxVerificationMethod", ""),
        "description": _strip_html(item.get("description_html", "")) or item.get("description", ""),
        "status": item.get("status", ""),
        "outline_number": item.get("outline_number", ""),
        "module": module.upper(),
        "project": PROJECT,
        "source": "polarion",
        "polarion_type": "ifxConfigurationTestcase",
        "source_document": f"polarion_testspec_{module.lower()}",
    }


def map_static_testcase(item: dict, module: str) -> dict:
    """Map a Polarion ifxStaticTestcase to TS_StaticInterfaceTestCase properties."""
    raw = item.get("raw_fields", {})
    return {
        "test_case_id": item.get("id", ""),
        "name": item.get("title", ""),
        "test_objective": _strip_html(raw.get("ifxTestFunctionality", "")) or "",
        "reviewed_artefact": _strip_html(raw.get("ifxReviewedArtefact", "")) or "",
        "automation_method": raw.get("ifxIsAutomated", ""),
        "test_design_technique": raw.get("ifxTestDesignTechnique", ""),
        "verification_method": raw.get("ifxVerificationMethod", ""),
        "description": _strip_html(item.get("description_html", "")) or item.get("description", ""),
        "status": item.get("status", ""),
        "outline_number": item.get("outline_number", ""),
        "module": module.upper(),
        "project": PROJECT,
        "source": "polarion",
        "polarion_type": "ifxStaticTestcase",
        "source_document": f"polarion_testspec_{module.lower()}",
    }


# Map Polarion type → mapper function
_MAPPERS = {
    "ifxITSTTestcase": map_functional_testcase,
    "ifxConfigurationTestcase": map_config_testcase,
    "ifxStaticTestcase": map_static_testcase,
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class RC1TestSpecBuilder:
    """Builds TS_* nodes and relationships from Polarion test spec JSON."""

    BATCH_SIZE = 500

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        data_path: Path,
        *,
        dry_run: bool = False,
        force_incremental: bool = False,
        batch_size: int = 500,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.data_path = Path(data_path)
        self.dry_run = dry_run
        self.force_incremental = force_incremental
        self.BATCH_SIZE = batch_size
        self.stats: dict = Counter()
        self._driver = None

    # -- Neo4j Connection ---------------------------------------------------

    def _connect(self):
        cfg = self.neo4j_cfg
        uri = cfg["uri"]
        logger.info("Connecting to Neo4j at %s …", uri)
        drv_kw = dict(
            auth=(cfg["username"], cfg["password"]),
            max_connection_lifetime=cfg.get("max_connection_lifetime", 3600),
            max_connection_pool_size=cfg.get("max_connection_pool_size", 50),
        )
        if "+s" not in uri.split("://")[0]:
            drv_kw["encrypted"] = cfg.get("encrypted", False)
        self._driver = GraphDatabase.driver(uri, **drv_kw)
        self._driver.verify_connectivity()
        logger.info("Connected to Neo4j (database: %s)", cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
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
                logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None):
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("Read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # -- Public Entry Point -------------------------------------------------

    def build(self):
        """Run the full test spec ingestion pipeline."""
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("RC1 TestSpec Knowledge Graph Builder – module: %s", self.module)
        logger.info("Data file: %s", self.data_path)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.data_path.exists():
            logger.error("Test spec JSON not found: %s", self.data_path)
            print(
                f"\n  ERROR: Test spec JSON not found:\n"
                f"  {self.data_path}\n\n"
                f"  Fetch test specs first:\n"
                f"    python fetch_polarion_testspec.py --module {self.module}\n"
            )
            sys.exit(1)

        # Step 1: Load and map items
        logger.info("Step 1/4: Loading and mapping test spec items…")
        parsed = self._load_and_map()

        if not any(parsed.values()):
            logger.warning("No test spec items found in %s", self.data_path)
            return

        if self.dry_run:
            self._preview(parsed)
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            # Step 3: Create constraints & nodes
            logger.info("Step 2/4: Creating constraints…")
            self._create_constraints(parsed)

            logger.info("Step 3/4: Creating nodes…")
            self._create_nodes(parsed)

            # Step 4: Create relationships
            logger.info("Step 4/4: Creating relationships…")
            self._create_relationships(parsed)

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # -- Load & Map ---------------------------------------------------------

    def _load_and_map(self) -> Dict[str, List[dict]]:
        """Load JSON and map Polarion items to Neo4j-ready property dicts.

        Returns a dict keyed by Neo4j label → list of property dicts.
        Also stores raw items for link extraction.
        """
        with open(self.data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        items = data.get("items", [])
        logger.info("  Loaded %d items from JSON", len(items))

        parsed: Dict[str, List[dict]] = {
            "TS_FunctionalTestCase": [],
            "TS_ConfigTestCase": [],
            "TS_StaticInterfaceTestCase": [],
            "TS_TestSpecDocument": [],
        }

        # Store raw items for link extraction in relationship phase
        self._raw_items: Dict[str, dict] = {}

        for item in items:
            item_type = item.get("item_type", "")
            neo4j_label = POLARION_TC_TYPE_MAP.get(item_type)
            if not neo4j_label:
                continue

            mapper = _MAPPERS.get(item_type)
            if not mapper:
                continue

            props = mapper(item, self.module)

            # Clean: remove None/empty values, stringify complex types
            clean = {}
            for k, v in props.items():
                if v is None:
                    continue
                if isinstance(v, (list, dict, set, frozenset)):
                    clean[k] = json.dumps(v, default=str)
                else:
                    clean[k] = v
            parsed[neo4j_label].append(clean)

            # Store raw item keyed by Polarion ID for link extraction
            self._raw_items[item.get("id", "")] = item

        # Create synthetic document node
        doc_name = f"polarion_testspec_{self.module.lower()}"
        total = sum(len(v) for v in parsed.values())
        parsed["TS_TestSpecDocument"] = [{
            "document_name": doc_name,
            "module": self.module,
            "project": PROJECT,
            "source": "polarion",
            "total_test_cases": total,
            "functional_count": len(parsed["TS_FunctionalTestCase"]),
            "config_count": len(parsed["TS_ConfigTestCase"]),
            "static_count": len(parsed["TS_StaticInterfaceTestCase"]),
        }]

        for label, items_list in parsed.items():
            if items_list:
                logger.info("  %s: %d items", label, len(items_list))

        return parsed

    # -- Constraints --------------------------------------------------------

    def _create_constraints(self, parsed: dict):
        for node_type, uid_prop in UID_MAP.items():
            if node_type not in parsed or not parsed[node_type]:
                continue
            constraint_name = f"unique_{node_type}_{uid_prop}"
            cypher = (
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{node_type}) REQUIRE n.{uid_prop} IS UNIQUE"
            )
            try:
                self._write_tx(cypher)
            except Exception as exc:
                logger.debug("Constraint %s: %s", constraint_name, exc)

    # -- Node Creation ------------------------------------------------------

    def _create_nodes(self, parsed: dict):
        for node_type, items in parsed.items():
            if not items:
                continue

            uid_prop = UID_MAP.get(node_type)
            if not uid_prop:
                logger.warning("Unknown TS node type: %s – skipping", node_type)
                continue

            logger.info("  Creating :%s (%d nodes)…", node_type, len(items))

            for chunk in self._chunked(items, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{node_type} {{{uid_prop}: props.{uid_prop}}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})

            self.stats[f"nodes:{node_type}"] = len(items)
            logger.info("    → created/merged %d :%s nodes", len(items), node_type)

    # -- Relationship Creation ----------------------------------------------

    def _create_relationships(self, parsed: dict):
        self._create_ts_verifies(parsed)
        self._create_ts_validates_ea_by_name(parsed)
        self._create_ts_tests_config_element(parsed)
        self._create_ts_belongs_to_module(parsed)
        self._create_ts_contains_testcase(parsed)
        self._create_vmodel_bridge_edges()

    def _create_ts_verifies(self, parsed: dict):
        """TS_VERIFIES: test case → ProductRequirement via ifxVerify links.

        Extracts linked_workitems with role='ifxVerify' from raw Polarion items.
        """
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]

        for node_type in test_case_types:
            items = parsed.get(node_type, [])
            edges = []

            for item_props in items:
                tc_id = item_props.get("test_case_id", "")
                if not tc_id:
                    continue

                # Look up raw Polarion item for linked_workitems
                raw_item = self._raw_items.get(tc_id, {})
                linked = raw_item.get("linked_workitems", [])

                for link in linked:
                    if link.get("role") == "ifxVerify":
                        target_id = link.get("target_id", "")
                        if target_id:
                            edges.append({
                                "test_case_id": tc_id,
                                "requirement_id": target_id,
                            })

            if not edges:
                continue

            logger.info("  Creating TS_VERIFIES from %s (%d edges)…",
                        node_type, len(edges))

            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (tc:{node_type} {{test_case_id: e.test_case_id}}) "
                    f"MATCH (prq:ProductRequirement {{requirement_id: e.requirement_id}}) "
                    f"MERGE (tc)-[:TS_VERIFIES]->(prq)"
                )
                self._write_tx(cypher, {"edges": chunk})

            self.stats[f"rel:TS_VERIFIES({node_type})"] = len(edges)

    def _create_ts_validates_ea_by_name(self, parsed: dict):
        """TS_VALIDATES_EA: test case → EA_Function via text-mention matching.

        Since RC1 Polarion test cases don't have EA GUIDs (unlike A3G Excel),
        we use text-mention expansion: if a test case's text fields mention
        an EA_Function by name (containing '_'), create the edge.
        """
        module = self.module
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]
        text_fields = [
            "test_objective", "test_procedure", "expected_results",
            "description", "name", "configuration_plan",
            "reviewed_artefact", "additional_info",
        ]

        db = self.neo4j_cfg["database"]

        for tc_type in test_case_types:
            items = parsed.get(tc_type, [])
            if not items:
                continue

            with self._driver.session(database=db) as session:
                before = session.run(
                    f"MATCH (:{tc_type})-[r:TS_VALIDATES_EA]->(f:EA_Function) "
                    f"WHERE f.module = $module RETURN count(r) AS c",
                    module=module,
                ).single()["c"]

            # Build WHERE clause for text field matching
            # Only use fields that exist on this node type
            field_conditions = []
            for field in text_fields:
                field_conditions.append(f"tc.{field} CONTAINS f.name")

            cypher = (
                f"MATCH (f:EA_Function) "
                f"WHERE f.module = $module AND f.name CONTAINS '_' "
                f"WITH f "
                f"MATCH (tc:{tc_type} {{module: $module}}) "
                f"WHERE NOT (tc)-[:TS_VALIDATES_EA]->(f) "
                f"  AND ("
                + " OR ".join(field_conditions)
                + f") "
                f"MERGE (tc)-[:TS_VALIDATES_EA {{source: 'text_mention'}}]->(f)"
            )
            self._write_tx(cypher, {"module": module})

            with self._driver.session(database=db) as session:
                after = session.run(
                    f"MATCH (:{tc_type})-[r:TS_VALIDATES_EA]->(f:EA_Function) "
                    f"WHERE f.module = $module RETURN count(r) AS c",
                    module=module,
                ).single()["c"]

            created = after - before
            if created:
                logger.info("  Text-mention TS_VALIDATES_EA from %s: %d new edges",
                            tc_type, created)
                self.stats[f"rel:TS_VALIDATES_EA_textmention({tc_type})"] = created

    def _create_ts_tests_config_element(self, parsed: dict):
        """TS_TESTS_CONFIG_ELEMENT: TS_ConfigTestCase → EA_ConfigContainer/Parameter.

        Uses config_path from ifxArchitectureInformation to match EA config nodes.
        """
        config_tests = parsed.get("TS_ConfigTestCase", [])
        if not config_tests:
            return

        edges = []
        for item in config_tests:
            cp = item.get("config_path", "")
            if not cp:
                continue
            # Extract the leaf name from config path (e.g. "Gpt/GptChannelConfigSet/..." → last segment)
            segments = [s for s in cp.split("/") if s]
            if segments:
                edges.append({
                    "test_case_id": item["test_case_id"],
                    "config_path": cp,
                    "leaf_name": segments[-1],
                })

        if not edges:
            return

        logger.info("  Creating TS_TESTS_CONFIG_ELEMENT (%d edges)…", len(edges))

        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (tc:TS_ConfigTestCase {test_case_id: e.test_case_id}) "
                "MATCH (c:EA_ConfigContainer {name: e.leaf_name}) "
                "MERGE (tc)-[:TS_TESTS_CONFIG_ELEMENT]->(c)"
            )
            self._write_tx(cypher, {"edges": chunk})

        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (tc:TS_ConfigTestCase {test_case_id: e.test_case_id}) "
                "MATCH (p:EA_ConfigParameter {name: e.leaf_name}) "
                "MERGE (tc)-[:TS_TESTS_CONFIG_ELEMENT]->(p)"
            )
            self._write_tx(cypher, {"edges": chunk})

        self.stats["rel:TS_TESTS_CONFIG_ELEMENT"] = len(edges)

    def _create_ts_belongs_to_module(self, parsed: dict):
        """TS_BELONGS_TO_MODULE: any TS node → MCALModule."""
        module = self.module
        for node_type in parsed:
            if not parsed[node_type]:
                continue
            uid_prop = UID_MAP.get(node_type)
            if not uid_prop:
                continue

            logger.info("  Creating TS_BELONGS_TO_MODULE for %s → %s …",
                        node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:TS_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

            count_res = self._run(
                f"MATCH (n:{node_type} {{module: $module}})"
                f"-[r:TS_BELONGS_TO_MODULE]->(:MCALModule) "
                f"RETURN count(r) AS cnt",
                {"module": module},
            )
            cnt = count_res[0]["cnt"] if count_res else 0
            self.stats[f"rel:TS_BELONGS_TO_MODULE({node_type})"] = cnt

    def _create_ts_contains_testcase(self, parsed: dict):
        """TS_CONTAINS_TESTCASE: TS_TestSpecDocument → individual test cases."""
        doc_nodes = parsed.get("TS_TestSpecDocument", [])
        if not doc_nodes:
            return

        doc_name = doc_nodes[0]["document_name"]
        test_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]

        for tc_type in test_types:
            items = parsed.get(tc_type, [])
            if not items:
                continue

            logger.info("  Creating TS_CONTAINS_TESTCASE: %s → %s (%d) …",
                        doc_name, tc_type, len(items))
            cypher = (
                f"MATCH (doc:TS_TestSpecDocument {{document_name: $doc_name}}) "
                f"MATCH (tc:{tc_type} {{source_document: $doc_name}}) "
                f"MERGE (doc)-[:TS_CONTAINS_TESTCASE]->(tc)"
            )
            self._write_tx(cypher, {"doc_name": doc_name})

            count_res = self._run(
                f"MATCH (:TS_TestSpecDocument {{document_name: $doc_name}})"
                f"-[r:TS_CONTAINS_TESTCASE]->(:{tc_type}) "
                f"RETURN count(r) AS cnt",
                {"doc_name": doc_name},
            )
            cnt = count_res[0]["cnt"] if count_res else 0
            self.stats[f"rel:TS_CONTAINS_TESTCASE({tc_type})"] = cnt

    def _create_vmodel_bridge_edges(self):
        """Create V-Model bridge relationships.

        IMPLEMENTS:  ProductRequirement → EA (reverse of EA_REALISES)
        TRACES_TO:   EA → TS (reverse of TS_VALIDATES_EA)
        """
        module = self.module
        logger.info("  Creating V-Model bridge edges for module %s…", module)

        # IMPLEMENTS: ProductRequirement → EA
        self._write_tx(
            "MATCH (ea)-[:EA_REALISES]->(prq:ProductRequirement) "
            "WHERE ea.module = $module "
            "MERGE (prq)-[:IMPLEMENTS {source: 'bridge', derived_from: 'EA_REALISES'}]->(ea)",
            {"module": module},
        )

        ea_count = self._run(
            "MATCH (prq:ProductRequirement)-[r:IMPLEMENTS]->(ea) "
            "WHERE any(l IN labels(ea) WHERE l STARTS WITH 'EA_') "
            "AND ea.module = $module RETURN count(r) AS c",
            {"module": module},
        )
        n_ea = ea_count[0]["c"] if ea_count else 0
        logger.info("    IMPLEMENTS (PRQ → EA): %d edges", n_ea)
        self.stats["rel:IMPLEMENTS(PRQ→EA)"] = n_ea

        # TRACES_TO: EA → TS test cases
        self._write_tx(
            "MATCH (ts)-[:TS_VALIDATES_EA]->(ea) "
            "WHERE ts.module = $module "
            "MERGE (ea)-[:TRACES_TO {source: 'bridge', derived_from: 'TS_VALIDATES_EA'}]->(ts)",
            {"module": module},
        )

        ts_ea_count = self._run(
            "MATCH (ea)-[r:TRACES_TO {derived_from: 'TS_VALIDATES_EA'}]->(ts) "
            "WHERE ts.module = $module RETURN count(r) AS c",
            {"module": module},
        )
        n_ts_ea = ts_ea_count[0]["c"] if ts_ea_count else 0
        logger.info("    TRACES_TO (EA → TS): %d edges", n_ts_ea)
        self.stats["rel:TRACES_TO(EA→TS)"] = n_ts_ea

        total = n_ea + n_ts_ea
        logger.info("  V-Model bridge edges total: %d", total)

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, parsed: dict):
        print(f"\n{'='*60}")
        print(f"  DRY-RUN PREVIEW – RC1 TestSpec Ingestion, Module: {self.module}")
        print(f"{'='*60}")

        total_nodes = 0
        print(f"\n  Node types:")
        for node_type, items in sorted(parsed.items()):
            if not items:
                continue
            uid = UID_MAP.get(node_type, "?")
            print(f"    :{node_type:<35s}  {len(items):>5,d} nodes  [merge key: {uid}]")
            total_nodes += len(items)
            for item in items[:3]:
                val = str(item.get(uid, "?"))[:60]
                print(f"      - {val}")
            if len(items) > 3:
                print(f"      … and {len(items) - 3} more")
        print(f"    {'TOTAL':<36s}  {total_nodes:>5,d}")

        # Count traceability links
        verify_count = 0
        for tc_id, raw_item in self._raw_items.items():
            for link in raw_item.get("linked_workitems", []):
                if link.get("role") == "ifxVerify":
                    verify_count += 1

        config_paths = sum(1 for t in parsed.get("TS_ConfigTestCase", [])
                           if t.get("config_path"))

        print(f"\n  Traceability links (potential edges):")
        print(f"    TS_VERIFIES              → PRQ  : ~{verify_count}")
        print(f"    TS_VALIDATES_EA (text)    → EA   : (computed at ingestion time)")
        if config_paths:
            print(f"    TS_TESTS_CONFIG_ELEMENT          : ~{config_paths}")
        print(f"{'='*60}\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print(f"\n{'='*60}")
        print(f"  BUILD COMPLETE – RC1 TestSpec Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print(f"{'='*60}")

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print(f"\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<35s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<36s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print(f"\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<35s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<36s}  {total_rels:>6,d}")

        print(f"{'='*60}\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def build_rc1_testspec(
    module: str,
    neo4j_cfg: dict,
    *,
    dry_run: bool = False,
    force_incremental: bool = False,
    data_path: Optional[Path] = None,
    batch_size: int = 500,
):
    """Build RC1 test spec knowledge graph for a module.

    Parameters
    ----------
    module : str
        MCAL module name (e.g. ``"GPT"``).
    neo4j_cfg : dict
        Neo4j connection settings.
    dry_run : bool
        Preview only — no database writes.
    force_incremental : bool
        Skip incremental checks.
    data_path : Path, optional
        Path to test spec JSON.  Default: auto-detect from jama-req/.
    batch_size : int
        UNWIND batch size.
    """
    module = module.upper()

    if data_path is None:
        data_path = JAMA_REQ_DIR / f"polarion_{module.lower()}_testspec.json"

    builder = RC1TestSpecBuilder(
        neo4j_cfg=neo4j_cfg,
        module=module,
        data_path=data_path,
        dry_run=dry_run,
        force_incremental=force_incremental,
        batch_size=batch_size,
    )
    builder.build()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    from build_rc1_knowledge_graph import load_storage_config, get_neo4j_settings

    parser = argparse.ArgumentParser(
        description="Build RC1 test spec knowledge graph from Polarion data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python rc1_testspec_builder.py --module GPT --profile test --dry-run\n"
            "  python rc1_testspec_builder.py --module GPT --profile test\n"
            "  python rc1_testspec_builder.py --module DMA --profile test --data /path/to/testspec.json\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--profile", "-p", default="test",
                        choices=["mcal", "test", "local"],
                        help="Neo4j profile (default: test).")
    parser.add_argument("--data", type=Path, default=None,
                        help="Path to test spec JSON (auto-detected).")
    parser.add_argument("--force", action="store_true",
                        help="Skip incremental tracking (full re-ingestion).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — no database changes.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="UNWIND batch size (default 500).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    storage_cfg = load_storage_config()
    neo4j_cfg = get_neo4j_settings(args.profile, storage_cfg)

    build_rc1_testspec(
        module=args.module,
        neo4j_cfg=neo4j_cfg,
        dry_run=args.dry_run,
        force_incremental=args.force,
        data_path=args.data,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
