#!/usr/bin/env python3
"""
RC1 Knowledge Graph Builder (Polarion-based)
=============================================

Builds a Neo4j knowledge graph from Polarion-sourced RC1 MCAL data.
This is the RC1 counterpart of ``build_knowledge_graph.py`` (A3G/Jama).

The two builders are intentionally separate so that changes to either
project's ingestion logic do not affect the other.

Supported artefact types (added incrementally):
  - Requirements: PRQ + SHRQ from ``polarion_<module>_combined_requirements.json``
  - Relationships: from ``polarion_<module>_relationships.json``

Usage:
    python build_rc1_knowledge_graph.py --module GPT --profile test
    python build_rc1_knowledge_graph.py --module GPT --profile test --clear
    python build_rc1_knowledge_graph.py --module GPT --profile test --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
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

CONFIG_DIR = HYBRIDRAG_DIR / "config"
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"
ONTOLOGY_PATH = CONFIG_DIR / "ontology.yaml"
STORAGE_CONFIG_PATH = CONFIG_DIR / "storage_config.yaml"

PROJECT = "RC1"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("rc1_kg_builder")


# ---------------------------------------------------------------------------
# HTML Stripping
# ---------------------------------------------------------------------------
class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def strip_html(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    stripper = _HTMLStripper()
    stripper.feed(str(text))
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Module Prefix Extraction
# ---------------------------------------------------------------------------
MODULE_PREFIX_RE = re.compile(r"^(?P<module>[A-Za-z_0-9]+):\s+")


def extract_module_prefix(title: str) -> Optional[str]:
    """Extract module prefix from Polarion title (e.g. 'Gpt: ...' → 'Gpt')."""
    m = MODULE_PREFIX_RE.match(title or "")
    return m.group("module") if m else None


# ---------------------------------------------------------------------------
# Configuration Loaders
# ---------------------------------------------------------------------------
def load_ontology(path: Path = ONTOLOGY_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_storage_config(path: Path = STORAGE_CONFIG_PATH) -> dict:
    from env_config import load_yaml_with_env
    return load_yaml_with_env(path)


def get_neo4j_settings(profile: str, storage_cfg: dict) -> dict:
    neo4j_section = storage_cfg.get("neo4j", {})
    if profile not in neo4j_section:
        raise ValueError(
            f"No Neo4j config for profile '{profile}'. "
            f"Available: {list(neo4j_section.keys())}"
        )
    return neo4j_section[profile]


# ---------------------------------------------------------------------------
# Polarion → Neo4j Property Mapping
# ---------------------------------------------------------------------------

# Polarion status enum → human-readable
STATUS_MAP = {
    "ifxApproved": "Approved",
    "ifxDraft": "Draft",
    "ifxRejected": "Rejected",
    "ifxObsolete": "Obsolete",
    "ifxInProgress": "In Progress",
    "ifxReview": "Review",
    "ifxNew": "New",
}

# Polarion ASIL enum → standard label
ASIL_MAP = {
    "ifxQm": "QM",
    "ifxAsilA": "ASIL_A",
    "ifxAsilB": "ASIL_B",
    "ifxAsilC": "ASIL_C",
    "ifxAsilD": "ASIL_D",
}

# Polarion item types → Neo4j labels
POLARION_TYPE_MAP = {
    "ifxProductRequirement": "ProductRequirement",
    "ifxStakeholderRequirement": "StakeholderRequirement",
}

# Polarion link roles → Neo4j relationship types
ROLE_TO_REL = {
    "ifxRefines": "DERIVES_FROM",
    "ifxChild": "CHILD_OF",
    "ifxImplements": "IMPLEMENTS",
    "ifxVerify": "VERIFIED_BY",
    "ifxContained": "CONTAINED_IN",
    "ifxRelatedTo": "RELATED_TO",
}


def get_module_paths(module: str) -> dict:
    """Return Polarion-specific file paths for a module."""
    mod = module.lower()
    return {
        "data": JAMA_REQ_DIR / f"polarion_{mod}_combined_requirements.json",
        "relationships": JAMA_REQ_DIR / f"polarion_{mod}_relationships.json",
    }


def map_polarion_item(item: dict, module: str) -> Optional[dict]:
    """Convert a Polarion JSON item into a flat Neo4j-ready property dict.

    Returns None if the item type is not recognised.
    """
    item_type = item.get("item_type", item.get("type", ""))
    label = POLARION_TYPE_MAP.get(item_type)
    if not label:
        return None

    raw = item.get("raw_fields", {})

    # Core properties
    props: dict[str, Any] = {
        "requirement_id": item.get("id", ""),
        "name": item.get("title", ""),
        "description": strip_html(item.get("description_html", "")) or item.get("description", ""),
        "status": STATUS_MAP.get(item.get("status", ""), item.get("status", "")),
        "outline_number": item.get("outline_number", ""),
        "project": PROJECT,
        "source": "polarion",
        "module": module.upper(),
    }

    # Module prefix from title (e.g. "Gpt: ..." → "Gpt")
    pfx = extract_module_prefix(item.get("title", ""))
    if pfx:
        props["module_prefix"] = pfx

    # Timestamps
    if item.get("created"):
        props["created_date"] = item["created"]
    if item.get("updated"):
        props["modified_date"] = item["updated"]

    # ASIL from custom fields
    asil_raw = raw.get("ifxAsil", "")
    if asil_raw:
        props["asil"] = ASIL_MAP.get(asil_raw, asil_raw)

    # Cross-reference to Jama (AU3GM-PRQ-xxxxx)
    jama_xref = raw.get("ifxInfineonSystemId", "")
    if jama_xref:
        props["jama_cross_ref"] = jama_xref

    # Polarion project ID
    if item.get("project_id"):
        props["polarion_project_id"] = item["project_id"]

    # Verification domain
    vd = raw.get("ifxVerificationDomain", "")
    if vd:
        props["verification_domain"] = vd.replace("ifx", "")

    # PRQ type
    prq_type = raw.get("ifxPRQType", "")
    if prq_type:
        props["prq_type"] = prq_type.replace("ifx", "")

    # Cybersecurity relevance
    cs = raw.get("ifxCybersecurityRelevance", "")
    if cs:
        props["cybersecurity_relevance"] = cs.replace("ifx", "")

    # Authoring state
    auth_state = raw.get("ifxAuthoringState", "")
    if auth_state:
        props["authoring_state"] = auth_state.replace("ifx", "")

    # Drop empty-string values
    props = {k: v for k, v in props.items() if v not in (None, "")}

    # Attach label for downstream grouping
    props["_label"] = label

    return props


# ---------------------------------------------------------------------------
# RC1 Knowledge Graph Builder
# ---------------------------------------------------------------------------
class RC1KnowledgeGraphBuilder:
    """
    Builds Neo4j requirement nodes and relationships from Polarion RC1 data.

    Workflow:
        1. Load Polarion JSON (combined requirements)
        2. Map items → Neo4j properties
        3. Create constraints / indexes
        4. Create ProductRequirement + StakeholderRequirement nodes
        5. Create MCALModule synthetic node (for the module)
        6. Create BELONGS_TO_MODULE relationships
        7. Load relationships JSON → create DERIVES_FROM, CHILD_OF, etc.
        8. Print summary
    """

    BATCH_SIZE = 500

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        data_path: Path,
        relationships_path: Optional[Path] = None,
        dry_run: bool = False,
        clear_db: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.data_path = data_path
        self.relationships_path = relationships_path
        self.dry_run = dry_run
        self.clear_db = clear_db

        self.stats: dict[str, int] = Counter()
        self._driver = None

    # -- Neo4j connection ---------------------------------------------------

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

        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _run(self, cypher: str, parameters: Optional[dict] = None, _attempt: int = 0):
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        try:
            with self._driver.session(database=db) as session:
                result = session.run(cypher, parameters or {})
                return [rec.data() for rec in result]
        except (ServiceUnavailable, TransientError, OSError) as exc:
            _attempt += 1
            if _attempt >= max_attempts:
                raise
            wait = min(2 ** _attempt, 8)
            logger.warning("Transient error (attempt %d/%d), retrying in %ds…",
                           _attempt, max_attempts, wait)
            time.sleep(wait)
            return self._run(cypher, parameters, _attempt=_attempt)

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None, _attempt: int = 0):
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        try:
            with self._driver.session(database=db) as session:
                return session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
        except (ServiceUnavailable, TransientError, OSError) as exc:
            _attempt += 1
            if _attempt >= max_attempts:
                raise
            wait = min(2 ** _attempt, 8)
            logger.warning("Transient write error (attempt %d/%d), retrying in %ds…",
                           _attempt, max_attempts, wait)
            time.sleep(wait)
            return self._write_tx(cypher, parameters, _attempt=_attempt)

    def _write_tx_counted(self, cypher: str, parameters: Optional[dict] = None) -> int:
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    records = session.execute_write(
                        lambda tx: list(tx.run(cypher, parameters or {}))
                    )
                    if records:
                        return records[0].get("cnt", 0)
                    return 0
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient error (attempt %d/%d), retrying in %ds…",
                               attempt, max_attempts, wait)
                time.sleep(wait)
        return 0

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]

    # -- Data Loading -------------------------------------------------------

    def _load_data(self) -> list[dict]:
        """Load Polarion combined requirements JSON and map to Neo4j properties."""
        if not self.data_path.exists():
            logger.error("Data file not found: %s", self.data_path)
            return []

        with open(self.data_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        # Support both formats: list or { "metadata": {...}, "items": [...] }
        if isinstance(raw, list):
            items = raw
        else:
            items = raw.get("items", [])

        logger.info("Loaded %d items from %s", len(items), self.data_path.name)

        # Map to Neo4j-ready property dicts
        mapped = []
        skipped = 0
        for item in items:
            props = map_polarion_item(item, self.module)
            if props:
                mapped.append(props)
            else:
                skipped += 1

        if skipped:
            logger.info("Skipped %d items with unrecognised types", skipped)
        logger.info("Mapped %d items for ingestion", len(mapped))
        return mapped

    def _load_relationships(self) -> list[dict]:
        """Load the pre-extracted relationships JSON."""
        if not self.relationships_path or not self.relationships_path.exists():
            logger.info("No relationships file found – skipping relationship creation from file.")
            return []

        with open(self.relationships_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        rels = raw.get("relationships", [])
        logger.info("Loaded %d relationships from %s", len(rels), self.relationships_path.name)
        return rels

    # -- Constraints --------------------------------------------------------

    def _create_constraints(self):
        logger.info("Creating constraints and indexes…")

        for label in ["ProductRequirement", "StakeholderRequirement"]:
            constraint_name = f"unique_{label}_requirement_id".lower()
            try:
                self._run(
                    f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.requirement_id IS UNIQUE"
                )
            except Exception as exc:
                logger.warning("Constraint %s: %s", constraint_name, exc)

        # Index on project for efficient filtering
        try:
            self._run(
                "CREATE INDEX idx_project IF NOT EXISTS "
                "FOR (n:ProductRequirement) ON (n.project)"
            )
        except Exception:
            pass

        # MCALModule uniqueness
        try:
            self._run(
                "CREATE CONSTRAINT unique_mcalmodule_module_name IF NOT EXISTS "
                "FOR (n:MCALModule) REQUIRE n.module_name IS UNIQUE"
            )
        except Exception:
            pass

        logger.info("Constraints and indexes created.")

    # -- Node Creation ------------------------------------------------------

    def _create_nodes(self, data: list[dict]):
        """Create requirement nodes grouped by label, using UNWIND batching."""
        by_label: Dict[str, list] = defaultdict(list)
        for props in data:
            label = props.get("_label", "")
            # Build a copy without the internal _label key for Neo4j
            neo4j_props = {k: v for k, v in props.items() if k != "_label"}
            by_label[label].append(neo4j_props)

        for label, items in by_label.items():
            logger.info("Creating :%s nodes (%d items)…", label, len(items))

            created = 0
            for chunk in self._chunked(items, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{label} {{requirement_id: props.requirement_id}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})
                created += len(chunk)

            self.stats[f"nodes:{label}"] = created
            logger.info("  → created/merged %d :%s nodes", created, label)

    # -- Synthetic Module Node ----------------------------------------------

    def _create_module_node(self):
        """Create or merge the MCALModule node for this module."""
        logger.info("Creating MCALModule node for %s…", self.module)
        cypher = (
            "MERGE (m:MCALModule {module_name: $module}) "
            "ON CREATE SET m.global_id = randomUUID(), m.project = $project "
            "SET m.project = $project "
            "RETURN m.module_name AS name"
        )
        self._write_tx(cypher, {"module": self.module, "project": PROJECT})
        self.stats["nodes:MCALModule"] = 1
        logger.info("  → MCALModule '%s' ready", self.module)

    # -- BELONGS_TO_MODULE --------------------------------------------------

    def _create_belongs_to_module(self):
        """Link requirement nodes to their MCALModule."""
        logger.info("Creating BELONGS_TO_MODULE relationships…")

        total = 0
        for label in ["ProductRequirement", "StakeholderRequirement"]:
            cypher = (
                f"MATCH (n:{label}) "
                f"WHERE n.module = $module AND n.project = $project "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:BELONGS_TO_MODULE]->(m) "
                f"RETURN count(*) AS cnt"
            )
            cnt = self._write_tx_counted(cypher, {
                "module": self.module,
                "project": PROJECT,
            })
            total += cnt
            logger.info("  → :%s – %d edges", label, cnt)

        self.stats["rel:BELONGS_TO_MODULE"] = total

    # -- Relationship Creation from JSON ------------------------------------

    def _create_relationships(self, rels: list[dict], data: list[dict]):
        """Create DERIVES_FROM, CHILD_OF, etc. from the relationships JSON."""
        if not rels:
            return

        # Build ID → label index from the mapped data
        id_to_label: Dict[str, str] = {}
        for props in data:
            rid = props.get("requirement_id", "")
            label = props.get("_label", "")
            if rid and label:
                id_to_label[rid] = label

        # Group relationships by Neo4j rel type and (from_label, to_label)
        edges_by_type: Dict[str, list] = defaultdict(list)
        skipped = 0

        for rel in rels:
            role = rel.get("relationship_role", "")
            neo4j_rel = ROLE_TO_REL.get(role)
            if not neo4j_rel:
                skipped += 1
                continue

            from_id = rel.get("from_item", "")
            to_id = rel.get("to_item", "")

            from_label = id_to_label.get(from_id)
            to_label = id_to_label.get(to_id)

            if not from_label or not to_label:
                skipped += 1
                continue

            edges_by_type[neo4j_rel].append({
                "from_id": from_id,
                "to_id": to_id,
                "from_label": from_label,
                "to_label": to_label,
                "suspect": rel.get("suspect", False),
            })

        if skipped:
            logger.info("Skipped %d relationships (unknown role or external endpoints)", skipped)

        # Create edges in Neo4j
        for rel_type, edges in edges_by_type.items():
            # Group by (from_label, to_label) for efficient Cypher
            by_combo: Dict[Tuple[str, str], list] = defaultdict(list)
            for e in edges:
                by_combo[(e["from_label"], e["to_label"])].append(e)

            total = 0
            for (fl, tl), edge_list in by_combo.items():
                batch = [
                    {"from_key": e["from_id"], "to_key": e["to_id"], "suspect": e["suspect"]}
                    for e in edge_list
                ]

                for chunk in self._chunked(batch, self.BATCH_SIZE):
                    cypher = (
                        f"UNWIND $edges AS e "
                        f"MATCH (from_node:{fl} {{requirement_id: e.from_key}}) "
                        f"MATCH (to_node:{tl} {{requirement_id: e.to_key}}) "
                        f"MERGE (from_node)-[r:{rel_type}]->(to_node) "
                        f"SET r.suspect = e.suspect, r.project = '{PROJECT}' "
                        f"RETURN count(*) AS cnt"
                    )
                    cnt = self._write_tx_counted(cypher, {"edges": chunk})
                    total += cnt

            self.stats[f"rel:{rel_type}"] = total
            logger.info("  → %s: %d edges", rel_type, total)

    # -- Clear DB -----------------------------------------------------------

    def _clear_module_data(self):
        """Delete only RC1 data for the current module (not the whole DB)."""
        logger.warning("Clearing RC1 %s data…", self.module)
        cypher = (
            "MATCH (n) "
            "WHERE n.project = $project AND n.module = $module "
            "DETACH DELETE n"
        )
        self._write_tx(cypher, {"project": PROJECT, "module": self.module})
        logger.info("  → Cleared RC1 %s nodes", self.module)

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, data: list[dict]):
        label_counts = Counter(p.get("_label", "?") for p in data)

        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – RC1 / Polarion / {self.module}")
        print("=" * 60)
        print(f"\n  Data file: {self.data_path}")
        print(f"  Relationships: {self.relationships_path or '(none)'}")
        print(f"\n  Node types to create:")
        for label, count in sorted(label_counts.items()):
            print(f"    :{label:<30s}  {count:>6,d}")
        print(f"    :{'MCALModule':<30s}  {'1':>6s}  (synthetic)")

        # Relationships preview
        rels = self._load_relationships()
        if rels:
            role_counts = Counter(r.get("relationship_role", "?") for r in rels)
            print(f"\n  Relationships to create ({len(rels)} total):")
            for role, count in sorted(role_counts.items()):
                neo4j_rel = ROLE_TO_REL.get(role, f"? ({role})")
                print(f"    {role:<25s} → :{neo4j_rel:<20s}  {count:>6,d}")
        else:
            print(f"\n  No relationships file found.")

        print(f"\n  Total nodes: {sum(label_counts.values())} + 1 MCALModule")
        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – RC1 / {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<30s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<31s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<30s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<31s}  {total_rels:>6,d}")

        print()

    # -- Main build ---------------------------------------------------------

    def build(self):
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("RC1 Knowledge Graph Builder – Module: %s", self.module)
        logger.info("Data source: %s", self.data_path)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        # Load and map data
        data = self._load_data()
        if not data:
            logger.error("No data loaded. Aborting.")
            return

        if self.dry_run:
            self._preview(data)
            return

        # Connect
        self._connect()

        try:
            if self.clear_db:
                self._clear_module_data()

            self._create_constraints()

            self._create_nodes(data)
            self._create_module_node()
            self._create_belongs_to_module()

            # Relationships
            rels = self._load_relationships()
            if rels:
                self._create_relationships(rels, data)

            self._print_summary(time.time() - t0)
        finally:
            self._close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
RC1_DEFAULT_QEAX = Path(
    r"C:\Users\NairSurajRet\Downloads\master_rc1_sw_mcal.qeax"
)


def _run_ea_ingestion(module: str, neo4j_cfg: dict, qeax_path: Path,
                      dry_run: bool, clear: bool):
    """Run the RC1 EA element + diagram extraction pipeline.

    Phase 1: Elements + Connectors (rc1_ea_graph_builder)
    Phase 2: Diagram structure (rc1_ea_diagram_extractor)
    """
    from rc1_ea_graph_builder import RC1EAGraphBuilder
    from rc1_ea_diagram_extractor import RC1EADiagramExtractor

    logger.info("=" * 60)
    logger.info("RC1 EA Knowledge Graph — module: %s", module)
    logger.info("  QEAX path : %s", qeax_path)
    logger.info("  Dry-run   : %s", dry_run)
    logger.info("  Clear     : %s", clear)
    logger.info("=" * 60)

    # Phase 1: Elements + Connectors
    logger.info("Phase 1/2: EA elements and connectors …")
    element_builder = RC1EAGraphBuilder(
        module=module,
        qeax_path=qeax_path,
        neo4j_cfg=neo4j_cfg,
        dry_run=dry_run,
        clear=clear,
    )
    element_builder.build()

    # Phase 2: Diagram structure
    logger.info("Phase 2/2: EA diagram structure …")
    diagram_builder = RC1EADiagramExtractor(
        module=module,
        qeax_path=qeax_path,
        neo4j_cfg=neo4j_cfg,
        dry_run=dry_run,
        clear=False,   # already cleared in phase 1 if requested
    )
    diagram_builder.build()

    logger.info("RC1 EA Knowledge Graph — %s — complete.", module)


def _run_source_ingestion(module: str, neo4j_cfg: dict,
                          dry_run: bool, sum_mode: bool,
                          sum_configs: Optional[list],
                          force_fetch: bool,
                          force_incremental: bool,
                          source_dir: Optional[Path],
                          sfr_include_dir: Optional[Path]):
    """Run the RC1 source code ingestion pipeline."""
    from rc1_source_code_builder import build_rc1_source_code

    build_rc1_source_code(
        module=module,
        neo4j_cfg=neo4j_cfg,
        dry_run=dry_run,
        sum_mode=sum_mode,
        sum_configs=sum_configs,
        force_fetch=force_fetch,
        force_incremental=force_incremental,
        source_dir=source_dir,
        sfr_include_dir=sfr_include_dir,
    )


def _run_sfr_ingestion(module: str, neo4j_cfg: dict,
                       dry_run: bool,
                       force_incremental: bool,
                       sfr_dir: Optional[Path],
                       devices: Optional[list]):
    """Run the RC1 SFR ingestion pipeline."""
    from rc1_sfr_builder import build_rc1_sfr

    build_rc1_sfr(
        module=module,
        neo4j_cfg=neo4j_cfg,
        dry_run=dry_run,
        devices=devices,
        force_incremental=force_incremental,
        sfr_dir=sfr_dir,
    )


def _run_testspec_ingestion(module: str, neo4j_cfg: dict,
                            dry_run: bool,
                            force_incremental: bool,
                            data_path: Optional[Path],
                            batch_size: int):
    """Run the RC1 test spec ingestion pipeline."""
    from rc1_testspec_builder import build_rc1_testspec

    build_rc1_testspec(
        module=module,
        neo4j_cfg=neo4j_cfg,
        dry_run=dry_run,
        force_incremental=force_incremental,
        data_path=data_path,
        batch_size=batch_size,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Build RC1 knowledge graph from Polarion requirements, QEAX model, source code, or SFR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Requirements (default)\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --clear\n"
            "\n"
            "  # EA/QEAX ingestion\n"
            "  python build_rc1_knowledge_graph.py --module Gpt --profile test --ingest-ea\n"
            "  python build_rc1_knowledge_graph.py --module Gpt --profile test --ingest-ea --dry-run\n"
            "  python build_rc1_knowledge_graph.py --module Gpt --profile test --ingest-ea --qeax-path /path/to.qeax\n"
            "\n"
            "  # Source code ingestion\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-source --dry-run\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-source --sum-mode\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-source --source-dir /path/to/repo\n"
            "\n"
            "  # SFR ingestion\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-sfr --dry-run\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-sfr\n"
            "\n"
            "  # Test specification ingestion\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-testspec --dry-run\n"
            "  python build_rc1_knowledge_graph.py --module GPT --profile test --ingest-testspec\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--profile", "-p", default="test",
                        choices=["mcal", "test", "local"],
                        help="Neo4j profile from storage_config.yaml (default: test).")
    parser.add_argument("--data", type=Path, default=None,
                        help="Path to combined requirements JSON (auto-detected).")
    parser.add_argument("--relationships", type=Path, default=None,
                        help="Path to relationships JSON (auto-detected).")

    # Ingestion mode flags (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--ingest-ea", action="store_true",
                            help="Ingest EA architecture from QEAX instead of requirements.")
    mode_group.add_argument("--ingest-source", action="store_true",
                            help="Ingest source code from Bitbucket repos.")
    mode_group.add_argument("--ingest-sfr", action="store_true",
                            help="Ingest SFR (Special Function Register) headers.")
    mode_group.add_argument("--ingest-testspec", action="store_true",
                            help="Ingest test specifications from Polarion.")

    # EA options
    parser.add_argument("--qeax-path", type=Path, default=RC1_DEFAULT_QEAX,
                        help="Path to RC1 .qeax file (used with --ingest-ea).")

    # Source code options
    parser.add_argument("--source-dir", type=Path, default=None,
                        help="Override: path to RC1 module source repo (used with --ingest-source).")
    parser.add_argument("--sfr-include-dir", type=Path, default=None,
                        help="Override: path to SFR include directory (used with --ingest-source).")
    parser.add_argument("--sum-mode", action="store_true",
                        help="Use real Sum configs from Bitbucket (used with --ingest-source).")
    parser.add_argument("--sum-configs", nargs="+", default=None,
                        help="Specific Sum config names to use (used with --ingest-source --sum-mode).")
    parser.add_argument("--force-fetch", action="store_true",
                        help="Force re-download of headers (used with --ingest-source).")

    # SFR options
    parser.add_argument("--sfr-dir", type=Path, default=None,
                        help="Override: path to SFR repo (used with --ingest-sfr).")
    parser.add_argument("--devices", nargs="+", default=None,
                        help="Specific device folders to process (default: RC1S16, used with --ingest-sfr).")

    # Test spec options
    parser.add_argument("--testspec-data", type=Path, default=None,
                        help="Path to test spec JSON (auto-detected, used with --ingest-testspec).")

    # Common options
    parser.add_argument("--clear", action="store_true",
                        help="Delete existing RC1 data for this module before building.")
    parser.add_argument("--force", action="store_true",
                        help="Skip incremental tracking (full re-ingestion).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only – no database changes.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="UNWIND batch size (default 500).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load configs
    storage_cfg = load_storage_config()
    neo4j_cfg = get_neo4j_settings(args.profile, storage_cfg)

    if args.ingest_ea:
        # EA/QEAX ingestion mode
        _run_ea_ingestion(
            module=args.module,
            neo4j_cfg=neo4j_cfg,
            qeax_path=args.qeax_path,
            dry_run=args.dry_run,
            clear=args.clear,
        )
    elif args.ingest_source:
        # Source code ingestion mode
        _run_source_ingestion(
            module=args.module,
            neo4j_cfg=neo4j_cfg,
            dry_run=args.dry_run,
            sum_mode=args.sum_mode,
            sum_configs=args.sum_configs,
            force_fetch=args.force_fetch,
            force_incremental=args.force,
            source_dir=args.source_dir,
            sfr_include_dir=args.sfr_include_dir,
        )
    elif args.ingest_sfr:
        # SFR ingestion mode
        _run_sfr_ingestion(
            module=args.module,
            neo4j_cfg=neo4j_cfg,
            dry_run=args.dry_run,
            force_incremental=args.force,
            sfr_dir=args.sfr_dir,
            devices=args.devices,
        )
    elif args.ingest_testspec:
        # Test specification ingestion mode
        _run_testspec_ingestion(
            module=args.module,
            neo4j_cfg=neo4j_cfg,
            dry_run=args.dry_run,
            force_incremental=args.force,
            data_path=args.testspec_data,
            batch_size=args.batch_size,
        )
    else:
        # Requirements ingestion mode (default)
        module = args.module.upper()

        # Resolve module-specific paths
        paths = get_module_paths(module)
        data_path = args.data or paths["data"]
        relationships_path = args.relationships or paths["relationships"]

        logger.info("Data file        : %s", data_path)
        logger.info("Relationships    : %s", relationships_path)

        builder = RC1KnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            data_path=data_path,
            relationships_path=relationships_path,
            dry_run=args.dry_run,
            clear_db=args.clear,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()


if __name__ == "__main__":
    main()
