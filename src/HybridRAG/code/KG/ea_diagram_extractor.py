"""EA Diagram Extractor — Ingest QEAX diagram structure into Neo4j.

Extracts Activity, Sequence, Statechart, and Logical diagrams for a given
MCAL module.  Creates EA_Diagram, EA_ActivityNode, EA_State, and
EA_SeqParticipant nodes plus flow/transition/message relationships.

This is a companion to ea_graph_builder.py and should be run AFTER it
so that existing EA_* nodes can be linked to diagrams.

Usage:
    python ea_diagram_extractor.py --module Adc
    python ea_diagram_extractor.py --module Adc --dry-run
    python ea_diagram_extractor.py --module Adc --clear
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError

from ifx_ea_sqlite.EASQLiteRepository import EASQLiteRepository

# ── paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CODE_DIR   = SCRIPT_DIR.parent
CONFIG_DIR = CODE_DIR.parent / "config"
STORAGE_CFG = CONFIG_DIR / "storage_config.yaml"

DEFAULT_QEAX = Path(
    r"C:\Users\NairSurajRet\Downloads"
    r"\2.20.0_tc4xx_sw_mcal\2.20.0_tc4xx_sw_mcal.qeax"
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
MAX_RETRIES = 3

# Object types that form activity diagram structure
ACTIVITY_NODE_TYPES = {
    "Action", "Decision", "MergeNode", "StateNode",
    "ActivityPartition", "LoopNode", "Activity", "ConditionalNode",
}

# Object types that form statechart structure
STATE_NODE_TYPES = {"State", "StateNode", "StateMachine"}

# Diagram types we process
DIAGRAM_TYPES = {"Activity", "Sequence", "Statechart", "Logical"}


# ═══════════════════════════════════════════════════════════════════════════
class EADiagramExtractor:
    """Extracts diagram metadata + internal structure from a QEAX model."""

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

        if neo4j_cfg is None:
            neo4j_cfg = self._load_neo4j_config()
        self.neo4j_cfg = neo4j_cfg

        self._driver = None
        self._db: Optional[EASQLiteRepository] = None

    # ── config ────────────────────────────────────────────────────────────
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
                logger.warning("Transient error (attempt %d/%d), retrying in %ds: %s",
                               attempt, MAX_RETRIES, wait, exc)
                time.sleep(wait)

    def _merge_nodes(self, label: str, uid_prop: str, batch: list[dict]):
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

    # ── Package traversal (shared logic) ──────────────────────────────────
    def _get_module_package_ids(self) -> set[int]:
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

    # ══════════════════════════════════════════════════════════════════════
    # EXTRACTION
    # ══════════════════════════════════════════════════════════════════════

    def _extract_diagrams(self, pkg_ids: set[int]) -> list[dict]:
        """Get all diagrams within the module's package scope."""
        pkg_list = ",".join(str(p) for p in pkg_ids)
        rows = self._db.sql_query(
            "SELECT d.Diagram_ID, d.Name, d.Diagram_Type, d.Package_ID, "
            "d.cx, d.cy, "
            "(SELECT COUNT(*) FROM t_diagramobjects do "
            " WHERE do.Diagram_ID = d.Diagram_ID) AS obj_count, "
            "(SELECT COUNT(*) FROM t_diagramlinks dl "
            " WHERE dl.DiagramID = d.Diagram_ID) AS link_count "
            f"FROM t_diagram d WHERE d.Package_ID IN ({pkg_list}) "
            "ORDER BY d.Diagram_Type, d.Name"
        )
        diagrams = []
        for r in rows:
            did, name, dtype, pkg_id, cx, cy, obj_count, link_count = r
            if dtype not in DIAGRAM_TYPES:
                self.stats[f"diagrams:skipped:{dtype}"] += 1
                continue
            diagrams.append({
                "ea_id": did,
                "name": name or "",
                "diagram_type": dtype,
                "package_id": pkg_id,
                "width": cx or 0,
                "height": cy or 0,
                "element_count": obj_count,
                "link_count": link_count,
                "module": self.module,
            })
        logger.info("Extracted %d diagrams", len(diagrams))
        return diagrams

    def _extract_diagram_elements(self, diagram_ids: list[int]) -> dict:
        """Get all objects placed in diagrams, grouped by diagram ID.

        Returns {diagram_id: [(Object_ID, Name, Object_Type, Stereotype, Sequence), ...]}
        """
        if not diagram_ids:
            return {}
        result = {}
        for chunk in _chunked(diagram_ids, 200):
            id_list = ",".join(str(d) for d in chunk)
            rows = self._db.sql_query(
                "SELECT do.Diagram_ID, o.Object_ID, o.Name, o.Object_Type, "
                "o.Stereotype, do.Sequence "
                "FROM t_diagramobjects do "
                "JOIN t_object o ON do.Object_ID = o.Object_ID "
                f"WHERE do.Diagram_ID IN ({id_list}) "
                "ORDER BY do.Diagram_ID, do.Sequence"
            )
            for r in rows:
                did, obj_id, name, obj_type, stereo, seq = r
                result.setdefault(did, []).append(
                    (obj_id, name, obj_type, stereo, seq)
                )
        return result

    def _extract_connectors_for_objects(self, object_ids: set[int],
                                         connector_types: set[str]) -> list[tuple]:
        """Get connectors of given types between the given objects.

        Returns [(Connector_ID, Type, Name, Stereotype, Start_ID, End_ID, SeqNo)]
        """
        if not object_ids:
            return []
        connectors = []
        obj_list_items = list(object_ids)
        for chunk in _chunked(obj_list_items, 500):
            id_list = ",".join(str(o) for o in chunk)
            type_filter = ",".join(f"'{t}'" for t in connector_types)
            rows = self._db.sql_query(
                "SELECT Connector_ID, Connector_Type, Name, Stereotype, "
                "Start_Object_ID, End_Object_ID, SeqNo "
                "FROM t_connector "
                f"WHERE Connector_Type IN ({type_filter}) "
                f"AND Start_Object_ID IN ({id_list}) "
                f"AND End_Object_ID IN ({id_list})"
            )
            connectors.extend(rows)
        return connectors

    # ── Activity diagram extraction ──────────────────────────────────────
    def _build_activity_nodes_and_flows(
        self,
        diagrams: list[dict],
        diagram_elements: dict,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Extract EA_ActivityNode nodes and EA_CONTROL_FLOW edges.

        Returns (activity_nodes, control_flow_edges, node_to_diagram_edges)
        """
        activity_diagrams = [d for d in diagrams if d["diagram_type"] == "Activity"]
        if not activity_diagrams:
            return [], [], []

        all_activity_obj_ids: set[int] = set()
        obj_to_diagram: dict[int, int] = {}
        node_props: dict[int, dict] = {}

        for diag in activity_diagrams:
            did = diag["ea_id"]
            elements = diagram_elements.get(did, [])
            for obj_id, name, obj_type, stereo, seq in elements:
                if obj_type not in ACTIVITY_NODE_TYPES:
                    continue
                all_activity_obj_ids.add(obj_id)
                obj_to_diagram[obj_id] = did
                node_props[obj_id] = {
                    "ea_id": obj_id,
                    "name": name or "",
                    "node_type": obj_type,
                    "sequence": seq or 0,
                    "diagram_id": did,
                    "module": self.module,
                }

        # Extract ControlFlow connectors between activity nodes
        cf_connectors = self._extract_connectors_for_objects(
            all_activity_obj_ids, {"ControlFlow"}
        )

        flow_edges = []
        for r in cf_connectors:
            conn_id, conn_type, name, stereo, src_id, tgt_id, seq_no = r
            if src_id in all_activity_obj_ids and tgt_id in all_activity_obj_ids:
                edge = {
                    "from_key": src_id,
                    "to_key": tgt_id,
                }
                if name:
                    edge["label"] = name
                flow_edges.append(edge)

        # Node → Diagram edges
        n2d_edges = [
            {"from_key": obj_id, "to_key": did}
            for obj_id, did in obj_to_diagram.items()
        ]

        logger.info("Activity: %d nodes, %d control-flow edges across %d diagrams",
                     len(node_props), len(flow_edges), len(activity_diagrams))
        return list(node_props.values()), flow_edges, n2d_edges

    # ── Sequence diagram extraction ──────────────────────────────────────
    def _build_sequence_data(
        self,
        diagrams: list[dict],
        diagram_elements: dict,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Extract participants and messages from sequence diagrams.

        Returns (participant_nodes, seq_message_edges, participant_to_diagram_edges)
        """
        seq_diagrams = [d for d in diagrams if d["diagram_type"] == "Sequence"]
        if not seq_diagrams:
            return [], [], []

        all_participant_ids: set[int] = set()
        participant_to_diagram: dict[int, int] = {}
        participant_props: dict[int, dict] = {}

        for diag in seq_diagrams:
            did = diag["ea_id"]
            elements = diagram_elements.get(did, [])
            for obj_id, name, obj_type, stereo, seq in elements:
                # Participants are Components, Sequences, Actors, etc.
                # Skip Notes
                if obj_type == "Note":
                    continue
                all_participant_ids.add(obj_id)
                participant_to_diagram[obj_id] = did
                participant_props[obj_id] = {
                    "ea_id": obj_id,
                    "name": name or obj_type,
                    "participant_type": obj_type,
                    "stereotype": stereo or "",
                    "sequence": seq or 0,
                    "diagram_id": did,
                    "module": self.module,
                }

        # Extract Sequence connectors (messages) between participants
        msg_connectors = self._extract_connectors_for_objects(
            all_participant_ids, {"Sequence"}
        )

        message_edges = []
        for r in msg_connectors:
            conn_id, conn_type, name, stereo, src_id, tgt_id, seq_no = r
            if src_id in all_participant_ids and tgt_id in all_participant_ids:
                edge = {
                    "from_key": src_id,
                    "to_key": tgt_id,
                    "seq_no": seq_no or 0,
                }
                if name:
                    edge["message"] = name
                message_edges.append(edge)

        # Participant → Diagram edges
        p2d_edges = [
            {"from_key": obj_id, "to_key": did}
            for obj_id, did in participant_to_diagram.items()
        ]

        logger.info("Sequence: %d participants, %d messages across %d diagrams",
                     len(participant_props), len(message_edges), len(seq_diagrams))
        return list(participant_props.values()), message_edges, p2d_edges

    # ── Statechart diagram extraction ────────────────────────────────────
    def _build_statechart_data(
        self,
        diagrams: list[dict],
        diagram_elements: dict,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Extract states and transitions from statechart diagrams.

        Returns (state_nodes, state_transition_edges, state_to_diagram_edges)
        """
        sc_diagrams = [d for d in diagrams if d["diagram_type"] == "Statechart"]
        if not sc_diagrams:
            return [], [], []

        all_state_ids: set[int] = set()
        state_to_diagram: dict[int, int] = {}
        state_props: dict[int, dict] = {}

        for diag in sc_diagrams:
            did = diag["ea_id"]
            elements = diagram_elements.get(did, [])
            for obj_id, name, obj_type, stereo, seq in elements:
                if obj_type not in STATE_NODE_TYPES:
                    continue
                all_state_ids.add(obj_id)
                state_to_diagram[obj_id] = did
                state_props[obj_id] = {
                    "ea_id": obj_id,
                    "name": name or "",
                    "state_type": obj_type,
                    "sequence": seq or 0,
                    "diagram_id": did,
                    "module": self.module,
                }

        # Extract StateFlow connectors
        sf_connectors = self._extract_connectors_for_objects(
            all_state_ids, {"StateFlow"}
        )

        transition_edges = []
        for r in sf_connectors:
            conn_id, conn_type, name, stereo, src_id, tgt_id, seq_no = r
            if src_id in all_state_ids and tgt_id in all_state_ids:
                edge = {"from_key": src_id, "to_key": tgt_id}
                if name:
                    edge["label"] = name
                transition_edges.append(edge)

        # State → Diagram edges
        s2d_edges = [
            {"from_key": obj_id, "to_key": did}
            for obj_id, did in state_to_diagram.items()
        ]

        logger.info("Statechart: %d states, %d transitions across %d diagrams",
                     len(state_props), len(transition_edges), len(sc_diagrams))
        return list(state_props.values()), transition_edges, s2d_edges

    # ── Logical diagram → existing EA_* node linkage ─────────────────────
    def _build_logical_links(
        self,
        diagrams: list[dict],
        diagram_elements: dict,
    ) -> list[dict]:
        """Build APPEARS_IN edges from existing EA_* elements to logical diagrams.

        Returns list of {from_key: Object_ID (ea_id), to_key: Diagram_ID (ea_id)}
        """
        logical_diagrams = [d for d in diagrams if d["diagram_type"] == "Logical"]
        if not logical_diagrams:
            return []

        edges = []
        for diag in logical_diagrams:
            did = diag["ea_id"]
            elements = diagram_elements.get(did, [])
            for obj_id, name, obj_type, stereo, seq in elements:
                # Only link elements that have EA stereotypes (already in graph)
                if stereo:
                    edges.append({"from_key": obj_id, "to_key": did})

        logger.info("Logical: %d APPEARS_IN links across %d diagrams",
                     len(edges), len(logical_diagrams))
        return edges

    # ══════════════════════════════════════════════════════════════════════
    # INGESTION
    # ══════════════════════════════════════════════════════════════════════

    def _create_constraints(self):
        labels = ["EA_Diagram", "EA_ActivityNode", "EA_State", "EA_SeqParticipant"]
        for label in labels:
            try:
                self._write(
                    f"CREATE CONSTRAINT IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.ea_id IS UNIQUE"
                )
            except Exception as exc:
                logger.debug("Constraint for %s skipped: %s", label, exc)
        logger.info("Created constraints for diagram labels")

    def _ingest_diagrams(self, diagrams: list[dict]):
        self._merge_nodes("EA_Diagram", "ea_id", diagrams)
        logger.info("  → %d EA_Diagram nodes", len(diagrams))

    def _ingest_activity(self, nodes, flow_edges, n2d_edges):
        if nodes:
            self._merge_nodes("EA_ActivityNode", "ea_id", nodes)
            logger.info("  → %d EA_ActivityNode nodes", len(nodes))

        if flow_edges:
            self._merge_edges_by_ea_id("EA_CONTROL_FLOW", flow_edges,
                                        ["label"])
            logger.info("  → %d EA_CONTROL_FLOW edges", len(flow_edges))

        if n2d_edges:
            self._merge_diagram_membership("EA_ActivityNode", n2d_edges)
            logger.info("  → %d ActivityNode PART_OF edges", len(n2d_edges))

    def _ingest_sequence(self, participants, msg_edges, p2d_edges):
        if participants:
            self._merge_nodes("EA_SeqParticipant", "ea_id", participants)
            logger.info("  → %d EA_SeqParticipant nodes", len(participants))

        if msg_edges:
            self._merge_edges_by_ea_id("EA_SEQ_MESSAGE", msg_edges,
                                        ["message", "seq_no"])
            logger.info("  → %d EA_SEQ_MESSAGE edges", len(msg_edges))

        if p2d_edges:
            self._merge_diagram_membership("EA_SeqParticipant", p2d_edges)
            logger.info("  → %d SeqParticipant PART_OF edges", len(p2d_edges))

    def _ingest_statechart(self, states, transition_edges, s2d_edges):
        if states:
            self._merge_nodes("EA_State", "ea_id", states)
            logger.info("  → %d EA_State nodes", len(states))

        if transition_edges:
            self._merge_edges_by_ea_id("EA_STATE_TRANSITION", transition_edges,
                                        ["label"])
            logger.info("  → %d EA_STATE_TRANSITION edges", len(transition_edges))

        if s2d_edges:
            self._merge_diagram_membership("EA_State", s2d_edges)
            logger.info("  → %d State PART_OF edges", len(s2d_edges))

    def _ingest_logical_links(self, edges):
        """Create APPEARS_IN from existing EA_* nodes to EA_Diagram."""
        if not edges:
            return
        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (a {ea_id: e.from_key}) "
                "MATCH (d:EA_Diagram {ea_id: e.to_key}) "
                "MERGE (a)-[r:APPEARS_IN]->(d)"
            )
            self._write(cypher, {"edges": chunk})
        self.stats["rel:APPEARS_IN"] += len(edges)
        logger.info("  → %d APPEARS_IN edges", len(edges))

    def _ingest_diagram_belongs_to_module(self, diagrams):
        edges = [{"from_key": d["ea_id"], "to_key": self.module} for d in diagrams]
        if not edges:
            return
        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (d:EA_Diagram {ea_id: e.from_key}) "
                "MATCH (m:MCALModule {name: e.to_key}) "
                "MERGE (d)-[r:BELONGS_TO_MODULE]->(m)"
            )
            self._write(cypher, {"edges": chunk})
        self.stats["rel:BELONGS_TO_MODULE"] += len(edges)

    # ── Generic edge helpers ──────────────────────────────────────────────
    def _merge_edges_by_ea_id(self, rel_type: str, edges: list[dict],
                               prop_names: list[str]):
        """MERGE edges matching on ea_id with optional properties."""
        if not edges:
            return
        set_parts = []
        for p in prop_names:
            set_parts.append(f"r.{p} = e.{p}")
        set_clause = ("SET " + ", ".join(set_parts)) if set_parts else ""

        for chunk in _chunked(edges, BATCH_SIZE):
            # Filter out edges missing the property keys to avoid Neo4j errors
            clean = []
            for e in chunk:
                ce = {"from_key": e["from_key"], "to_key": e["to_key"]}
                for p in prop_names:
                    if p in e:
                        ce[p] = e[p]
                clean.append(ce)
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (a {ea_id: e.from_key}) "
                "MATCH (b {ea_id: e.to_key}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"{set_clause}"
            )
            self._write(cypher, {"edges": clean})
        self.stats[f"rel:{rel_type}"] += len(edges)

    def _merge_diagram_membership(self, node_label: str, edges: list[dict]):
        """Create PART_OF edges from diagram-internal nodes to EA_Diagram."""
        if not edges:
            return
        for chunk in _chunked(edges, BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                f"MATCH (n:{node_label} {{ea_id: e.from_key}}) "
                "MATCH (d:EA_Diagram {ea_id: e.to_key}) "
                "MERGE (n)-[r:PART_OF]->(d)"
            )
            self._write(cypher, {"edges": chunk})
        self.stats[f"rel:PART_OF:{node_label}"] += len(edges)

    def _clear_diagram_data(self):
        logger.warning("Clearing diagram data for module '%s'", self.module)
        for label in ["EA_Diagram", "EA_ActivityNode", "EA_State", "EA_SeqParticipant"]:
            self._write(
                f"MATCH (n:{label} {{module: $module}}) DETACH DELETE n",
                {"module": self.module},
            )
        # Also remove APPEARS_IN edges to cleaned diagrams
        # (those diagrams are already deleted above, so edges are gone)

    # ══════════════════════════════════════════════════════════════════════
    # MAIN ORCHESTRATION
    # ══════════════════════════════════════════════════════════════════════

    def build(self):
        t0 = time.time()
        try:
            self._open_qeax()
            self._connect_neo4j()

            if self.clear:
                self._clear_diagram_data()

            self._create_constraints()

            # ── Extract ──
            pkg_ids = self._get_module_package_ids()
            diagrams = self._extract_diagrams(pkg_ids)
            if not diagrams:
                logger.warning("No diagrams found for module '%s'", self.module)
                return

            diagram_ids = [d["ea_id"] for d in diagrams]
            diagram_elements = self._extract_diagram_elements(diagram_ids)

            act_nodes, act_flows, act_n2d = self._build_activity_nodes_and_flows(
                diagrams, diagram_elements
            )
            seq_parts, seq_msgs, seq_p2d = self._build_sequence_data(
                diagrams, diagram_elements
            )
            sc_states, sc_trans, sc_s2d = self._build_statechart_data(
                diagrams, diagram_elements
            )
            logical_links = self._build_logical_links(diagrams, diagram_elements)

            # ── Dry-run summary ──
            if self.dry_run:
                logger.info("=" * 60)
                logger.info("[DRY-RUN] Extraction complete for '%s'", self.module)
                logger.info("  Diagrams:         %d", len(diagrams))
                by_type = defaultdict(int)
                for d in diagrams:
                    by_type[d["diagram_type"]] += 1
                for dt, cnt in sorted(by_type.items()):
                    logger.info("    %-15s %d", dt, cnt)
                logger.info("  Activity nodes:   %d", len(act_nodes))
                logger.info("  Control flows:    %d", len(act_flows))
                logger.info("  Seq participants: %d", len(seq_parts))
                logger.info("  Seq messages:     %d", len(seq_msgs))
                logger.info("  States:           %d", len(sc_states))
                logger.info("  State transitions:%d", len(sc_trans))
                logger.info("  Logical links:    %d", len(logical_links))
                logger.info("=" * 60)
                return

            # ── Ingest ──
            self._ingest_diagrams(diagrams)
            self._ingest_diagram_belongs_to_module(diagrams)
            self._ingest_activity(act_nodes, act_flows, act_n2d)
            self._ingest_sequence(seq_parts, seq_msgs, seq_p2d)
            self._ingest_statechart(sc_states, sc_trans, sc_s2d)
            self._ingest_logical_links(logical_links)

            elapsed = time.time() - t0
            self._print_summary(elapsed)

        finally:
            self._close_qeax()
            self._close_neo4j()

    def _print_summary(self, elapsed: float):
        logger.info("=" * 60)
        logger.info("EA Diagram Extractor — %s — %.1fs", self.module, elapsed)
        logger.info("=" * 60)
        for key in sorted(self.stats):
            logger.info("  %-40s %d", key, self.stats[key])
        logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════
# Utility
# ═══════════════════════════════════════════════════════════════════════════

def _chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="EA Diagram Extractor")
    parser.add_argument("--module", required=True,
                        help="MCAL module name (e.g. Adc, Port, Dma)")
    parser.add_argument("--qeax", type=Path, default=DEFAULT_QEAX,
                        help="Path to .qeax file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing diagram data first")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    extractor = EADiagramExtractor(
        module=args.module,
        qeax_path=args.qeax,
        dry_run=args.dry_run,
        clear=args.clear,
    )
    extractor.build()


if __name__ == "__main__":
    main()
