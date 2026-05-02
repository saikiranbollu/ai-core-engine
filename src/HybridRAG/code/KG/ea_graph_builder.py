"""EA Graph Builder — Ingest QEAX model data into Neo4j.

Opens a .qeax file via ifx_ea_sqlite, extracts elements/connectors/tagged-values
for a given MCAL module, and writes EA_* nodes + relationships to Neo4j using
UNWIND-batched MERGE statements.

Usage:
    python ea_graph_builder.py --module Adc
    python ea_graph_builder.py --module Adc --clear
    python ea_graph_builder.py --module Adc --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError

from ifx_ea_sqlite.EASQLiteRepository import EASQLiteRepository

# ── paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # .../KG/
CODE_DIR   = SCRIPT_DIR.parent                        # .../code/
CONFIG_DIR = CODE_DIR.parent / "config"               # .../HybridRAG/config/
STORAGE_CFG = CONFIG_DIR / "storage_config.yaml"

# Default QEAX model path (can be overridden via --qeax)
DEFAULT_QEAX = Path(
    r"C:\Users\NairSurajRet\Downloads"
    r"\2.20.0_tc4xx_sw_mcal\2.20.0_tc4xx_sw_mcal.qeax"
)

logger = logging.getLogger(__name__)

# ── Stereotype→Label mapping ──────────────────────────────────────────────
# Maps EA stereotypes to Neo4j node labels + extraction config
STEREOTYPE_MAP = {
    # Functions
    "generic_interface":        {"label": "EA_Function",        "kind": "api"},
    "local_function_interface": {"label": "EA_Function",        "kind": "local"},
    # Types
    "structure":                {"label": "EA_DataType",        "kind": "struct"},
    "enum":                     {"label": "EA_DataType",        "kind": "enum"},
    "type":                     {"label": "EA_DataType",        "kind": "typedef"},
    "function_pointer":         {"label": "EA_DataType",        "kind": "function_pointer"},
    # Config
    "ifx_config_parameter":     {"label": "EA_ConfigParameter"},
    "config_struct":            {"label": "EA_ConfigContainer"},
    "config_macros":            {"label": "EA_ConfigMacro",     "kind": "derived"},
    "code_gen_macros_interface":{"label": "EA_ConfigMacro",     "kind": "codegen"},
    # Errors
    "error_code":               {"label": "EA_ErrorCode"},
    # Requirements
    "ifx_requirement":          {"label": "EA_Requirement"},
    # Design
    "design_decision":          {"label": "EA_DesignDecision"},
    "information":              {"label": "EA_Information"},
    "cover_tag":                {"label": "EA_CoverTag"},
    # Variables & memory
    "global_variable":          {"label": "EA_GlobalVariable"},
    "property_variable":        {"label": "EA_PropertyVariable"},
    "memory_section":           {"label": "EA_MemorySection"},
    # Files
    "header":                   {"label": "EA_SourceFile",      "kind": "header"},
    "source":                   {"label": "EA_SourceFile",      "kind": "source"},
    # Safety / security
    "untrusted":                {"label": "EA_TrustDomain"},
    # HW
    "register":                 {"label": "EA_Register"},
    "registerblock":            {"label": "EA_HwPeripheral"},
}

# Connector type + stereotype → relationship name
CONNECTOR_MAP = {
    ("Dependency", "implements"):   "EA_IMPLEMENTS",
    ("Dependency", "sw_access"):    "EA_ACCESSES_REGISTER",
    ("Dependency", "external"):     "EA_EXTERNAL_DEP",
    ("Dependency", "optional"):     "EA_OPTIONAL_DEP",
    ("Dependency", "includes"):     "EA_INCLUDES",
    ("Dependency", "call"):         "EA_CALLS",
    ("Dependency", ""):             "EA_DEPENDS_ON",
    ("Dependency", None):           "EA_DEPENDS_ON",
    ("InformationFlow", ""):        "EA_THREAT_REACHES",
    ("InformationFlow", None):      "EA_THREAT_REACHES",
    ("Realisation", ""):            "EA_REALISES",
    ("Realisation", None):          "EA_REALISES",
    ("Aggregation", ""):            "EA_AGGREGATES",
    ("Aggregation", None):          "EA_AGGREGATES",
    ("Sequence", ""):               "EA_SEQ_MESSAGE",
    ("Sequence", None):             "EA_SEQ_MESSAGE",
    ("Generalization", ""):         "EA_GENERALISES",
    ("Generalization", None):       "EA_GENERALISES",
}

BATCH_SIZE = 500
MAX_RETRIES = 3


# ═══════════════════════════════════════════════════════════════════════════
class EAGraphBuilder:
    """Extracts data from a QEAX model and writes EA_* nodes to Neo4j."""

    def __init__(
        self,
        module: str,
        qeax_path: Path = DEFAULT_QEAX,
        neo4j_cfg: Optional[dict] = None,
        dry_run: bool = False,
        clear: bool = False,
    ):
        self.module = module
        self.qeax_path = qeax_path
        self.dry_run = dry_run
        self.clear = clear
        self.stats: Counter = Counter()

        # Neo4j config
        if neo4j_cfg is None:
            neo4j_cfg = self._load_neo4j_config()
        self.neo4j_cfg = neo4j_cfg

        self._driver = None
        self._db: Optional[EASQLiteRepository] = None

    # ── config loading ────────────────────────────────────────────────────
    def _load_neo4j_config(self) -> dict:
        sys.path.insert(0, str(CODE_DIR))
        from env_config import load_yaml_with_env

        cfg = load_yaml_with_env(STORAGE_CFG)
        profile = cfg.get("active_instance", "test")
        neo4j_section = cfg["neo4j"][profile]
        logger.info("Loaded Neo4j config for profile '%s'", profile)
        return neo4j_section

    # ── Neo4j connection ──────────────────────────────────────────────────
    def _connect_neo4j(self):
        if self.dry_run:
            logger.info("[DRY-RUN] Would connect to Neo4j")
            return
        uri = self.neo4j_cfg["uri"]
        drv_kw = dict(
            auth=(self.neo4j_cfg["username"], self.neo4j_cfg["password"]),
            max_connection_lifetime=self.neo4j_cfg.get("max_connection_lifetime", 3600),
            max_connection_pool_size=self.neo4j_cfg.get("max_connection_pool_size", 50),
            connection_acquisition_timeout=self.neo4j_cfg.get("connection_acquisition_timeout", 60),
        )
        if "+s" not in uri.split("://")[0]:
            drv_kw["encrypted"] = self.neo4j_cfg.get("encrypted", False)
        self._driver = GraphDatabase.driver(uri, **drv_kw)
        self._driver.verify_connectivity()
        logger.info("Connected to Neo4j at %s", uri)

    def _close_neo4j(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    # ── QEAX connection ──────────────────────────────────────────────────
    def _open_qeax(self):
        logger.info("Opening QEAX: %s", self.qeax_path)
        self._db = EASQLiteRepository(str(self.qeax_path))

    def _close_qeax(self):
        if self._db:
            self._db.close()
            self._db = None

    # ── Neo4j write helpers ───────────────────────────────────────────────
    def _write(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry."""
        if self.dry_run:
            return
        db = self.neo4j_cfg.get("database", "neo4j")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(
                        lambda tx: tx.run(cypher, parameters or {})
                    )
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= MAX_RETRIES:
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning(
                    "Transient error (attempt %d/%d), retrying in %ds: %s",
                    attempt, MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)

    def _merge_nodes(self, label: str, uid_prop: str, batch: list[dict]):
        """UNWIND-batch MERGE nodes."""
        if not batch:
            return
        for chunk in _chunked(batch, BATCH_SIZE):
            cypher = (
                f"UNWIND $items AS props "
                f"MERGE (n:{label} {{{uid_prop}: props.{uid_prop}}}) "
                f"ON CREATE SET n.global_id = randomUUID() "
                f"SET n += props"
            )
            self._write(cypher, {"items": chunk})
        self.stats[f"nodes:{label}"] += len(batch)

    def _merge_edges(
        self,
        rel_type: str,
        from_label: str,
        from_uid: str,
        to_label: str,
        to_uid: str,
        edges: list[dict],
        edge_props: Optional[list[str]] = None,
    ):
        """UNWIND-batch MERGE relationships."""
        if not edges:
            return
        set_parts = []
        if edge_props:
            for p in edge_props:
                set_parts.append(f"r.{p} = e.{p}")
        set_clause = ("SET " + ", ".join(set_parts)) if set_parts else ""

        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                f"UNWIND $edges AS e "
                f"MATCH (a:{label_match(from_label)} {{{from_uid}: e.from_key}}) "
                f"MATCH (b:{label_match(to_label)} {{{to_uid}: e.to_key}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"{set_clause}"
            )
            self._write(cypher, {"edges": chunk})
        self.stats[f"rel:{rel_type}"] += len(edges)

    # ── Package traversal ─────────────────────────────────────────────────
    def _get_module_package_ids(self) -> set[int]:
        """Find the module's root package and collect all descendant IDs."""
        rows = self._db.sql_query(
            "SELECT Package_ID FROM t_package "
            f"WHERE UPPER(Name) = '{self.module.upper()}' "
            "AND Parent_ID IN ("
            "  SELECT Package_ID FROM t_package WHERE Name IN ("
            "    'Software Architecture', 'Software Unit Design', "
            "    'Test Specification', 'Source Code'"
            "  )"
            ")"
        )
        if not rows:
            # Fallback: find by matching the component's Package_ID parent
            rows = self._db.sql_query(
                "SELECT Package_ID FROM t_package "
                f"WHERE UPPER(Name) = '{self.module.upper()}'"
            )
        root_ids = {r[0] for r in rows}
        logger.info("Module '%s' root packages: %s", self.module, root_ids)

        all_ids = set()
        for rid in root_ids:
            all_ids |= self._bfs_packages(rid)
        logger.info("Total packages in scope: %d", len(all_ids))
        return all_ids

    def _bfs_packages(self, root_id: int) -> set[int]:
        """BFS to collect all descendant package IDs."""
        visited = {root_id}
        queue = [root_id]
        while queue:
            pid = queue.pop(0)
            children = self._db.sql_query(
                f"SELECT Package_ID FROM t_package WHERE Parent_ID = {pid}"
            )
            for c in children:
                if c[0] not in visited:
                    visited.add(c[0])
                    queue.append(c[0])
        return visited

    # ── Element extraction ────────────────────────────────────────────────
    def _extract_elements(self, pkg_ids: set[int]) -> dict[int, dict]:
        """Query all typed elements in the package scope, return {Object_ID: props}."""
        pkg_list = ",".join(str(p) for p in pkg_ids)
        rows = self._db.sql_query(
            "SELECT Object_ID, Name, Object_Type, Stereotype, Note, "
            "Package_ID, ParentID, Classifier, ea_guid "
            f"FROM t_object WHERE Package_ID IN ({pkg_list}) "
            "AND Stereotype IS NOT NULL AND Stereotype != ''"
        )
        elements = {}
        for r in rows:
            obj_id, name, obj_type, stereo, note, pkg_id, parent_id, classifier, ea_guid = r
            if stereo not in STEREOTYPE_MAP:
                continue
            mapping = STEREOTYPE_MAP[stereo]
            props = {
                "ea_id": obj_id,
                "name": name or "",
                "object_type": obj_type,
                "stereotype": stereo,
                "note": _strip_html(note) if note else "",
                "package_id": pkg_id,
                "module": self.module,
                "label": mapping["label"],
            }
            # Store EA GUID as feature_id for cross-builder traceability
            if ea_guid:
                props["feature_id"] = ea_guid.strip("{}")
            if "kind" in mapping:
                props["kind"] = mapping["kind"]
            if classifier:
                props["classifier_id"] = classifier
            elements[obj_id] = props
        logger.info("Extracted %d typed elements", len(elements))
        return elements

    def _tag_isr_functions(self, elements: dict[int, dict], pkg_ids: set[int]):
        """Tag EA_Function elements with is_isr based on package hierarchy.

        Functions under packages named "Interrupt handlers" (within the
        module's "SW interfaces" or "Exported SW interfaces" tree) are
        tagged with ``is_isr=True``; all other EA_Functions get ``is_isr=False``.
        """
        # Find all "Interrupt handlers" packages within the module scope
        pkg_list = ",".join(str(p) for p in pkg_ids)
        rows = self._db.sql_query(
            "SELECT Package_ID FROM t_package "
            f"WHERE Package_ID IN ({pkg_list}) "
            "AND UPPER(Name) = 'INTERRUPT HANDLERS'"
        )
        isr_pkg_ids = {r[0] for r in rows}

        isr_count = 0
        func_count = 0
        for elem in elements.values():
            if elem.get("label") == "EA_Function":
                func_count += 1
                if elem["package_id"] in isr_pkg_ids:
                    elem["is_isr"] = True
                    isr_count += 1
                else:
                    elem["is_isr"] = False

        logger.info("Tagged %d/%d EA_Functions as ISR (from %d 'Interrupt handlers' packages)",
                     isr_count, func_count, len(isr_pkg_ids))

    def _enrich_with_tagged_values(self, elements: dict[int, dict]):
        """Add tagged values as node properties."""
        if not elements:
            return
        id_list = ",".join(str(eid) for eid in elements)
        rows = self._db.sql_query(
            "SELECT Object_ID, Property, Value, Notes "
            f"FROM t_objectproperties WHERE Object_ID IN ({id_list})"
        )
        count = 0
        for r in rows:
            obj_id, prop, value, notes = r
            if obj_id not in elements:
                continue
            # EA stores long values in Notes field when Value is <memo>
            actual_val = notes if (value == "<memo>" and notes) else (value or "")
            if actual_val:
                safe_key = f"tv_{_safe_prop_name(prop)}"
                elements[obj_id][safe_key] = actual_val
                count += 1
        logger.info("Enriched elements with %d tagged values", count)

    def _enrich_with_attributes(self, elements: dict[int, dict]):
        """Add struct/enum attributes as a JSON-like list property."""
        struct_ids = [
            eid for eid, props in elements.items()
            if props.get("label") in ("EA_DataType",)
        ]
        if not struct_ids:
            return
        id_list = ",".join(str(i) for i in struct_ids)
        rows = self._db.sql_query(
            "SELECT Object_ID, Name, Type, [Default], Stereotype "
            f"FROM t_attribute WHERE Object_ID IN ({id_list}) "
            "ORDER BY Object_ID, Pos"
        )
        # Group by element
        from collections import defaultdict
        by_elem = defaultdict(list)
        for r in rows:
            obj_id, name, atype, default, stereo = r
            by_elem[obj_id].append(f"{name}: {atype}" + (f" = {default}" if default else ""))

        for eid, attrs in by_elem.items():
            if eid in elements:
                elements[eid]["attributes"] = "; ".join(attrs)

    def _enrich_with_operations(self, elements: dict[int, dict]):
        """Add function operations (parameters, return type) to EA_Function nodes."""
        func_ids = [
            eid for eid, props in elements.items()
            if props.get("label") == "EA_Function"
        ]
        if not func_ids:
            return
        id_list = ",".join(str(i) for i in func_ids)

        # Get operations
        op_rows = self._db.sql_query(
            "SELECT OperationID, Object_ID, Name, Type, Stereotype, Notes "
            f"FROM t_operation WHERE Object_ID IN ({id_list})"
        )
        op_map = {}  # OperationID -> Object_ID
        for r in op_rows:
            op_id, obj_id, op_name, ret_type, stereo, notes = r
            op_map[op_id] = obj_id
            if obj_id in elements:
                if ret_type:
                    elements[obj_id]["return_type"] = ret_type
                if notes:
                    elements[obj_id]["description"] = _strip_html(notes)

        # Get parameters for those operations
        if op_map:
            op_list = ",".join(str(o) for o in op_map)
            param_rows = self._db.sql_query(
                "SELECT OperationID, Name, Type, Kind "
                f"FROM t_operationparams WHERE OperationID IN ({op_list}) "
                "ORDER BY OperationID, Pos"
            )
            from collections import defaultdict
            params_by_op = defaultdict(list)
            for r in param_rows:
                op_id, pname, ptype, pkind = r
                params_by_op[op_id].append(f"{pname}: {ptype}")

            for op_id, params in params_by_op.items():
                obj_id = op_map.get(op_id)
                if obj_id and obj_id in elements:
                    elements[obj_id]["parameters"] = "; ".join(params)

        # Get operation-level tagged values (service_id, etc.)
        if op_map:
            op_list = ",".join(str(o) for o in op_map)
            tv_rows = self._db.sql_query(
                "SELECT ElementID, Property, Value, Notes "
                f"FROM t_operationtag WHERE ElementID IN ({op_list})"
            )
            for r in tv_rows:
                op_id, prop, value, notes = r
                obj_id = op_map.get(op_id)
                if obj_id and obj_id in elements:
                    actual_val = notes if (value == "<memo>" and notes) else (value or "")
                    if actual_val:
                        safe_key = f"op_{_safe_prop_name(prop)}"
                        elements[obj_id][safe_key] = actual_val

    # ── HSI: pull out-of-scope register targets into scope ───────────────
    def _pull_hsi_registers(
        self,
        elements: dict[int, dict],
        pkg_ids: set[int],
    ) -> list[dict]:
        """Find registers targeted by sw_access connectors from function_design
        elements in-scope, but living outside the module package tree
        (e.g. in the shared 'AURIX 3G Family' register model).

        function_design elements are NOT in STEREOTYPE_MAP so they're absent
        from *elements*.  We query them from the DB, find their sw_access
        connectors to out-of-scope targets, pull those register targets into
        *elements*, and return remapped connectors where the source is the
        parent EA_Function (not the function_design)."""

        # Step 1: Find function_design elements in module scope
        pkg_list = ",".join(str(p) for p in pkg_ids)
        fd_rows = self._db.sql_query(
            "SELECT Object_ID, ParentID, Name FROM t_object "
            f"WHERE Package_ID IN ({pkg_list}) "
            "AND Stereotype = 'function_design'"
        )
        if not fd_rows:
            logger.info("No function_design elements found in scope")
            return []

        # Map function_design_id → parent EA_Function id (by matching name)
        func_by_name = {
            p["name"]: eid for eid, p in elements.items()
            if p.get("label") == "EA_Function"
        }
        fd_to_parent = {}
        for obj_id, _, name in fd_rows:
            parent = func_by_name.get(name)
            if parent:
                fd_to_parent[obj_id] = parent
        logger.info("Found %d function_design elements (%d with name-matched EA_Function)",
                     len(fd_rows), len(fd_to_parent))
        if not fd_to_parent:
            return []

        # Step 2: Find sw_access connectors from function_design → out-of-scope targets
        fd_list = ",".join(str(i) for i in fd_to_parent)
        elem_list = ",".join(str(e) for e in elements)
        rows = self._db.sql_query(
            "SELECT c.Connector_ID, c.Start_Object_ID, c.End_Object_ID, "
            "c.Name, c.Stereotype "
            "FROM t_connector c "
            f"WHERE c.Start_Object_ID IN ({fd_list}) "
            "AND c.Stereotype = 'sw_access' "
            f"AND c.End_Object_ID NOT IN ({elem_list})"
        )
        if not rows:
            logger.info("No out-of-scope sw_access targets found")
            return []
        logger.info("Found %d sw_access connectors to out-of-scope targets", len(rows))

        # Step 3: Pull register targets into elements
        out_ids = list({r[2] for r in rows})
        oid_list = ",".join(str(i) for i in out_ids)
        elem_rows = self._db.sql_query(
            "SELECT Object_ID, Name, Object_Type, Stereotype, Note, "
            "Package_ID, ParentID, Classifier, ea_guid "
            f"FROM t_object WHERE Object_ID IN ({oid_list})"
        )
        pulled = 0
        for r in elem_rows:
            obj_id, name, obj_type, stereo, note, pkg_id, parent_id, classifier, ea_guid = r
            if stereo not in STEREOTYPE_MAP:
                continue
            mapping = STEREOTYPE_MAP[stereo]
            props = {
                "ea_id": obj_id,
                "name": name or "",
                "object_type": obj_type,
                "stereotype": stereo,
                "note": _strip_html(note) if note else "",
                "package_id": pkg_id,
                "module": self.module,
                "label": mapping["label"],
            }
            if ea_guid:
                props["feature_id"] = ea_guid.strip("{}")
            if "kind" in mapping:
                props["kind"] = mapping["kind"]
            if parent_id:
                props["parent_element_id"] = parent_id
            elements[obj_id] = props
            pulled += 1
        logger.info("Pulled %d out-of-scope HSI register targets into scope", pulled)

        # Step 4: Build remapped connectors (parent function → register)
        hsi_connectors = []
        for conn_id, start_id, end_id, name, stereo in rows:
            if end_id not in elements:
                continue
            parent_func = fd_to_parent.get(start_id)
            if not parent_func:
                continue
            hsi_connectors.append({
                "connector_id": conn_id,
                "rel_type": "EA_ACCESSES_REGISTER",
                "from_id": parent_func,
                "to_id": end_id,
                "name": name or "",
                "stereotype": stereo or "",
            })
        logger.info("Created %d HSI connector edges (remapped to parent functions)",
                     len(hsi_connectors))
        return hsi_connectors

    def _enrich_registers_with_tagged_values(self, elements: dict[int, dict]):
        """Add register-specific tagged values (access, APU, CPU mode, SFR ID, size)."""
        reg_ids = [
            eid for eid, p in elements.items()
            if p.get("stereotype") in ("register", "registerblock")
        ]
        if not reg_ids:
            return
        id_list = ",".join(str(i) for i in reg_ids)
        rows = self._db.sql_query(
            "SELECT Object_ID, Property, Value "
            f"FROM t_objectproperties WHERE Object_ID IN ({id_list})"
        )
        REG_TAG_MAP = {
            "SFREA_registerId":      "sfr_id",
            "accesstype":            "access_type",
            "shortdescription":      "description",
            "size":                  "size_bits",
            "rAPU":                  "read_apu",
            "wAPU":                  "write_apu",
            "rCpuMode":              "read_cpu_mode",
            "wCpuMode":              "write_cpu_mode",
            "SFREA_registerBlockId": "sfr_block_id",
        }
        count = 0
        for r in rows:
            obj_id, prop, value = r
            if obj_id not in elements or not value:
                continue
            neo_key = REG_TAG_MAP.get(prop)
            if neo_key:
                elements[obj_id][neo_key] = value
                count += 1
        logger.info("Enriched registers with %d tagged values", count)

    def _enrich_connectors_with_tagged_values(self, connectors: list[dict]):
        """Add connector tagged values (e.g. Access Type) as edge properties."""
        if not connectors:
            return
        conn_ids = [c["connector_id"] for c in connectors]
        id_list = ",".join(str(i) for i in conn_ids)
        rows = self._db.sql_query(
            "SELECT ElementID, Property, Value "
            f"FROM t_connectortag WHERE ElementID IN ({id_list})"
        )
        tag_by_conn: dict[int, dict] = {}
        for r in rows:
            conn_id, prop, value = r
            if not value:
                continue
            tag_by_conn.setdefault(conn_id, {})[prop] = value

        count = 0
        for conn in connectors:
            tags = tag_by_conn.get(conn["connector_id"], {})
            access = tags.get("Access Type")
            if access:
                conn["access_type"] = access
                count += 1
        logger.info("Enriched %d connectors with Access Type", count)

    # ── Connector extraction ──────────────────────────────────────────────
    def _extract_connectors(self, elements: dict[int, dict]) -> list[dict]:
        """Get all connectors between in-scope elements."""
        if not elements:
            return []
        id_list = ",".join(str(eid) for eid in elements)
        rows = self._db.sql_query(
            "SELECT Connector_ID, Connector_Type, Name, Stereotype, "
            "Start_Object_ID, End_Object_ID, SeqNo, Direction "
            "FROM t_connector "
            f"WHERE Start_Object_ID IN ({id_list}) "
            f"OR End_Object_ID IN ({id_list})"
        )
        connectors = []
        for r in rows:
            conn_id, conn_type, name, stereo, src_id, tgt_id, seq_no, direction = r
            # Both ends must be in scope (skip external refs for now)
            if src_id not in elements or tgt_id not in elements:
                self.stats["connectors:out_of_scope"] += 1
                continue
            rel_name = CONNECTOR_MAP.get((conn_type, stereo or ""))
            if rel_name is None:
                rel_name = CONNECTOR_MAP.get((conn_type, None))
            if rel_name is None:
                self.stats[f"connectors:unmapped:{conn_type}:{stereo}"] += 1
                continue
            conn = {
                "connector_id": conn_id,
                "rel_type": rel_name,
                "from_id": src_id,
                "to_id": tgt_id,
                "name": name or "",
                "stereotype": stereo or "",
            }
            if seq_no:
                conn["seq_no"] = seq_no
            connectors.append(conn)
        logger.info("Extracted %d connectors (%d out-of-scope skipped)",
                     len(connectors), self.stats["connectors:out_of_scope"])
        return connectors

    # ── Neo4j ingestion ───────────────────────────────────────────────────
    def _create_constraints(self):
        """Create uniqueness constraints for EA node types."""
        labels = {m["label"] for m in STEREOTYPE_MAP.values()}
        for label in sorted(labels):
            try:
                cypher = (
                    f"CREATE CONSTRAINT IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.ea_id IS UNIQUE"
                )
                self._write(cypher)
            except Exception as exc:
                logger.debug("Constraint for %s skipped: %s", label, exc)
        # Module node constraint (may already exist as index)
        try:
            self._write(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (n:MCALModule) REQUIRE n.name IS UNIQUE"
            )
        except Exception:
            logger.debug("MCALModule constraint skipped (index may already exist)")
        logger.info("Created uniqueness constraints for %d labels", len(labels))

    def _ingest_module_node(self):
        """Create (or merge) the MCALModule node."""
        self._merge_nodes("MCALModule", "name", [{"name": self.module}])

    def _ingest_elements(self, elements: dict[int, dict]):
        """Group elements by label and MERGE into Neo4j."""
        from collections import defaultdict
        by_label = defaultdict(list)
        for props in elements.values():
            label = props.pop("label")
            by_label[label].append(props)

        for label, batch in by_label.items():
            self._merge_nodes(label, "ea_id", batch)
            logger.info("  → %d :%s nodes", len(batch), label)

        # Restore label field (needed for connector resolution)
        for eid, props in elements.items():
            stereo = props.get("stereotype", "")
            if stereo in STEREOTYPE_MAP:
                props["label"] = STEREOTYPE_MAP[stereo]["label"]

    def _ingest_connectors(self, connectors: list[dict], elements: dict[int, dict]):
        """Group connectors by relationship type and MERGE into Neo4j."""
        from collections import defaultdict
        by_rel = defaultdict(list)
        for conn in connectors:
            edge = {
                "from_key": conn["from_id"],
                "to_key": conn["to_id"],
            }
            if conn.get("name"):
                edge["name"] = conn["name"]
            if conn.get("seq_no"):
                edge["seq_no"] = conn["seq_no"]
            if conn.get("stereotype"):
                edge["connector_stereotype"] = conn["stereotype"]
            if conn.get("access_type"):
                edge["access_type"] = conn["access_type"]
            by_rel[conn["rel_type"]].append(edge)

        for rel_type, edges in by_rel.items():
            # Determine source/target labels from edges
            # For simplicity, use multi-label match (any EA node with ea_id)
            edge_props = [k for k in edges[0] if k not in ("from_key", "to_key")]
            self._merge_edges_generic(rel_type, edges, edge_props)
            logger.info("  → %d :%s relationships", len(edges), rel_type)

    def _merge_edges_generic(self, rel_type: str, edges: list[dict],
                             edge_props: Optional[list[str]] = None):
        """MERGE edges matching on ea_id (works across all EA labels)."""
        if not edges:
            return
        set_parts = []
        if edge_props:
            for p in edge_props:
                set_parts.append(f"r.{p} = e.{p}")
        set_clause = ("SET " + ", ".join(set_parts)) if set_parts else ""

        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (a {ea_id: e.from_key}) "
                "MATCH (b {ea_id: e.to_key}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"{set_clause}"
            )
            self._write(cypher, {"edges": chunk})
        self.stats[f"rel:{rel_type}"] += len(edges)

    def _ingest_belongs_to_module(self, elements: dict[int, dict]):
        """Create BELONGS_TO_MODULE relationships from all EA nodes to MCALModule."""
        edges = [{"from_key": eid, "to_key": self.module} for eid in elements]
        if not edges:
            return
        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (a {ea_id: e.from_key}) "
                "MATCH (m:MCALModule {name: e.to_key}) "
                "MERGE (a)-[r:BELONGS_TO_MODULE]->(m)"
            )
            self._write(cypher, {"edges": chunk})
        self.stats["rel:BELONGS_TO_MODULE"] += len(edges)

    def _clear_module_data(self):
        """Delete all EA nodes + relationships for this module."""
        logger.warning("Clearing all EA data for module '%s'", self.module)
        self._write(
            "MATCH (n {module: $module}) DETACH DELETE n",
            {"module": self.module},
        )

    # ── Main orchestration ────────────────────────────────────────────────
    def build(self):
        """Run the full extraction → ingestion pipeline."""
        t0 = time.time()
        try:
            self._open_qeax()
            self._connect_neo4j()

            if self.clear:
                self._clear_module_data()

            self._create_constraints()

            # ── Extract from QEAX ──
            pkg_ids = self._get_module_package_ids()
            elements = self._extract_elements(pkg_ids)
            self._tag_isr_functions(elements, pkg_ids)
            self._enrich_with_tagged_values(elements)
            self._enrich_with_attributes(elements)
            self._enrich_with_operations(elements)

            # ── HSI: pull out-of-scope registers & enrich ──
            hsi_connectors = self._pull_hsi_registers(elements, pkg_ids)
            self._enrich_registers_with_tagged_values(elements)

            connectors = self._extract_connectors(elements)
            self._enrich_connectors_with_tagged_values(connectors)

            # Append HSI connectors (remapped function_design → parent func)
            if hsi_connectors:
                self._enrich_connectors_with_tagged_values(hsi_connectors)
                connectors.extend(hsi_connectors)

            # ── Ingest into Neo4j ──
            self._ingest_module_node()
            self._ingest_elements(elements)
            self._ingest_connectors(connectors, elements)
            self._ingest_belongs_to_module(elements)

            elapsed = time.time() - t0
            self._print_summary(elapsed)

        finally:
            self._close_qeax()
            self._close_neo4j()

    def _print_summary(self, elapsed: float):
        logger.info("=" * 60)
        logger.info("EA Graph Builder — %s — %.1fs", self.module, elapsed)
        logger.info("=" * 60)
        for key in sorted(self.stats):
            logger.info("  %-40s %d", key, self.stats[key])
        logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════════════════════════════════════

def _chunked(lst: list, size: int):
    """Yield successive chunks."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def _strip_html(text: str) -> str:
    """Remove HTML tags from EA Note fields."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return text.strip()


def _safe_prop_name(name: str) -> str:
    """Convert a tagged-value name to a safe Neo4j property key."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower().strip("_")


def label_match(label: str) -> str:
    """Return label string for Cypher MATCH clause."""
    return label


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="EA Graph Builder")
    parser.add_argument("--module", required=True, help="MCAL module name (e.g. Adc, Port, Dma)")
    parser.add_argument("--qeax", type=Path, default=DEFAULT_QEAX, help="Path to .qeax file")
    parser.add_argument("--dry-run", action="store_true", help="Extract only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true", help="Clear existing module data first")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    builder = EAGraphBuilder(
        module=args.module,
        qeax_path=args.qeax,
        dry_run=args.dry_run,
        clear=args.clear,
    )
    builder.build()


if __name__ == "__main__":
    main()
