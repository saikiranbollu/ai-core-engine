"""
Knowledge Graph Builder for Automotive Embedded Software Ontology
=================================================================

Reads the unified ontology (config/ontology.yaml), prompts the user to select
a profile (mcal or illd), loads requirements data from JSON, and populates a
Neo4j knowledge graph according to the ontology's node types and relationship
definitions.

Usage:
    python build_knowledge_graph.py                         # interactive profile selection
    python build_knowledge_graph.py --profile mcal          # skip prompt
    python build_knowledge_graph.py --profile mcal --clear  # wipe DB first
    python build_knowledge_graph.py --profile mcal --dry-run  # preview only
    python build_knowledge_graph.py --profile mcal --relationships jama_adc_relationships.json
    # Normal build (uses cache if available, fetches live if not)
python build_knowledge_graph.py --profile mcal --clear

# Force re-fetch relationships from Jama API
python build_knowledge_graph.py --profile mcal --clear --refresh-relationships
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

# Retry decorator for transient Neo4j failures
try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False

# ---------------------------------------------------------------------------
# Paths  (adjusted for code/KG/ subfolder)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

# Ensure the code dir is on sys.path so sibling modules (env_config, etc.) resolve
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

# Add repo root for src.* imports (IngestionPipeline, etc.)
ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ILLD parser import (code directory)
try:
    from illd_parsers import parse_all_files as illd_parse_all_files, Node as ILLDNode, Edge as ILLDEdge  # type: ignore[import-not-found]
except ImportError:
    illd_parse_all_files = None
    ILLDNode = None
    ILLDEdge = None

# SWA parser import (IngestionPipeline/Parsers)
try:
    from src.IngestionPipeline.Parsers.swa_parsers import parse_swa_directory
except ImportError:
    parse_swa_directory = None

# SWUD parser import (IngestionPipeline/Parsers)
try:
    from src.IngestionPipeline.Parsers.swud_parsers import parse_swud_directory
except ImportError:
    parse_swud_directory = None

# TestSpec parser import (IngestionPipeline/Parsers)
try:
    from src.IngestionPipeline.Parsers.testspec_parsers import parse_testspec_workbook
except ImportError:
    parse_testspec_workbook = None

# SFR parser import (KG directory)
try:
    from sfr_parsers import parse_sfr_repo
except ImportError:
    parse_sfr_repo = None

# JamaConnector import
try:
    from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
except ImportError:
    JamaConnector = None  # graceful fallback
CONFIG_DIR = HYBRIDRAG_DIR / "config"
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"
DATA_DIR = HYBRIDRAG_DIR / "data"                     # ILLD processed data root
SWA_DIR = HYBRIDRAG_DIR / "swa"                       # SWA markdown sections
SWUD_DIR = HYBRIDRAG_DIR / "swud"                     # SWUD markdown sections
TESTSPEC_DIR = HYBRIDRAG_DIR / "testspec"              # Test spec Excel workbooks
VISUALIZE_DIR = HYBRIDRAG_DIR / "visualize"
ONTOLOGY_PATH = CONFIG_DIR / "ontology.yaml"
STORAGE_CONFIG_PATH = CONFIG_DIR / "storage_config.yaml"

# Legacy fallbacks – only used when KnowledgeGraphBuilder is instantiated
# without explicit paths.  Prefer get_module_paths(module) for module-aware paths.
DEFAULT_DATA_PATH = None
DEFAULT_RELATIONSHIPS_PATH = None
DEFAULT_FOLDERS_CACHE_PATH = None


def get_module_paths(module: str) -> dict:
    """Return module-specific file paths for MCAL Jama data.

    Returns a dict with keys ``data``, ``relationships``, ``folders``
    pointing to the expected JSON files under ``jama-req/``.

    Example::

        paths = get_module_paths("GPT")
        # paths["data"]          → jama-req/jama_gpt_combined_requirements.json
        # paths["relationships"] → jama-req/jama_gpt_relationships.json
        # paths["folders"]       → jama-req/jama_gpt_folders.json
    """
    mod = module.lower()
    return {
        "data":          JAMA_REQ_DIR / f"jama_{mod}_combined_requirements.json",
        "relationships": JAMA_REQ_DIR / f"jama_{mod}_relationships.json",
        "folders":       JAMA_REQ_DIR / f"jama_{mod}_folders.json",
    }

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("kg_builder")


# ---------------------------------------------------------------------------
# Progress Bar Helper
# ---------------------------------------------------------------------------
class ProgressBar:
    """Thread-safe console progress bar (no external dependencies)."""

    def __init__(self, total: int, prefix: str = "", width: int = 40):
        self.total = total
        self.prefix = prefix
        self.width = width
        self._count = 0
        self._lock = threading.Lock()
        self._start = time.time()

    def update(self, n: int = 1):
        with self._lock:
            self._count += n
            self._draw()

    def _draw(self):
        pct = self._count / self.total if self.total else 1
        filled = int(self.width * pct)
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = time.time() - self._start
        eta = (elapsed / self._count * (self.total - self._count)) if self._count else 0
        sys.stdout.write(
            f"\r  {self.prefix} |{bar}| {self._count}/{self.total} "
            f"({pct:.0%}) ETA {eta:.0f}s  "
        )
        sys.stdout.flush()

    def finish(self):
        self._count = self.total
        self._draw()
        elapsed = time.time() - self._start
        sys.stdout.write(f"  [{elapsed:.1f}s]\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# HTML Stripping Helper
# ---------------------------------------------------------------------------
class _HTMLStripper(HTMLParser):
    """Simple HTML tag stripper."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def strip_html(text: Optional[str]) -> Optional[str]:
    """Remove HTML tags from *text*, returning plain text."""
    if not text:
        return text
    stripper = _HTMLStripper()
    stripper.feed(str(text))
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Configuration Loaders
# ---------------------------------------------------------------------------
def load_ontology(path: Path = ONTOLOGY_PATH) -> dict:
    """Load the unified ontology YAML."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_storage_config(path: Path = STORAGE_CONFIG_PATH) -> dict:
    """Load storage_config.yaml with env-var resolution."""
    from env_config import load_yaml_with_env
    return load_yaml_with_env(path)


def get_neo4j_settings(profile: str, storage_cfg: dict) -> dict:
    """Return the Neo4j connection dict for *profile*."""
    neo4j_section = storage_cfg.get("neo4j", {})
    if profile not in neo4j_section:
        raise ValueError(
            f"No Neo4j config for profile '{profile}'. "
            f"Available: {list(neo4j_section.keys())}"
        )
    return neo4j_section[profile]


# ---------------------------------------------------------------------------
# Ontology Helpers
# ---------------------------------------------------------------------------
def get_profile_config(ontology: dict, profile: str) -> dict:
    """Return the profile sub-dict from the ontology."""
    profiles = ontology.get("profiles", {})
    if profile not in profiles:
        raise ValueError(
            f"Unknown profile '{profile}'. Available: {list(profiles.keys())}"
        )
    return profiles[profile]


def build_item_type_map(node_types: list) -> Dict[int, dict]:
    """Map Jama item_type int → node type definition (skip synthetic nodes)."""
    mapping: Dict[int, dict] = {}
    for nt in node_types:
        jt = nt.get("jama_item_type")
        if jt is not None:
            mapping[int(jt)] = nt
    return mapping


def build_property_field_map(properties: list) -> List[dict]:
    """Return the list of property defs that have a jama_field mapping."""
    return [p for p in properties if p.get("jama_field")]


def get_unique_id_property(node_type: dict) -> Optional[str]:
    """Identify the first unique property name for a node type (used as merge key)."""
    for p in node_type.get("properties", []):
        if p.get("unique"):
            return p["name"]
    return None


# ---------------------------------------------------------------------------
# Module Prefix Extraction
# ---------------------------------------------------------------------------
MODULE_PREFIX_RE = re.compile(r"^(?P<module>[A-Za-z]+):\s+")


def extract_module_prefix(name: str) -> Optional[str]:
    """Extract MCAL module prefix from an item name (e.g. 'Adc: ...' → 'Adc')."""
    m = MODULE_PREFIX_RE.match(name or "")
    return m.group("module") if m else None


# ---------------------------------------------------------------------------
# Value-Map Resolution
# ---------------------------------------------------------------------------
def resolve_value_maps(properties: list, raw_fields: dict, flat: dict) -> dict:
    """
    For integer-valued fields that have a ``value_map``, replace the numeric
    ID with its human-readable label in the output dict *flat*.
    """
    for prop in properties:
        pname = prop["name"]
        vmap = prop.get("value_map")
        if vmap and pname in flat:
            raw_val = flat[pname]
            if raw_val is not None:
                readable = vmap.get(int(raw_val)) if raw_val != -1 else None
                if readable:
                    flat[pname] = readable
    return flat


# ---------------------------------------------------------------------------
# Data Extraction – build flat property dict from a JamaItem dict
# ---------------------------------------------------------------------------
def extract_node_properties(
    item: dict,
    node_type_def: dict,
    html_fields: list[str],
) -> dict:
    """
    Given a raw JSON item dict and its ontology node-type definition, return
    a flat dict of Neo4j-ready properties.
    """
    props_defs = node_type_def.get("properties", [])
    raw_fields = item.get("raw_fields", {})
    flat: dict[str, Any] = {}

    for pdef in props_defs:
        pname = pdef["name"]
        jf = pdef.get("jama_field")
        dt = pdef.get("data_type", "string")

        # Skip vector / embedding fields – they are not stored in Neo4j as regular props
        if dt == "vector":
            continue

        value: Any = None

        if jf:
            # Try raw_fields first, then top-level item keys
            if jf in raw_fields:
                value = raw_fields[jf]
            elif jf in item:
                value = item[jf]

        # Derived fields
        if pdef.get("extraction_rule") == "derived_from_name_prefix":
            value = extract_module_prefix(item.get("name", ""))

        if value is None or value == "":
            continue

        # Type coercions
        if dt == "integer":
            try:
                value = int(value)
            except (ValueError, TypeError):
                pass
        elif dt in ("text", "string"):
            value = str(value)
            if pname in html_fields:
                value = strip_html(value)

        flat[pname] = value

    # Resolve value maps (replace IDs with labels)
    resolve_value_maps(props_defs, raw_fields, flat)

    # Always include Jama numeric ID for cross-referencing
    if "jama_id" not in flat and "id" in item:
        flat["jama_id"] = item["id"]

    return flat


# ---------------------------------------------------------------------------
# Neo4j Graph Builder
# ---------------------------------------------------------------------------
class KnowledgeGraphBuilder:
    """
    Builds a Neo4j knowledge graph from JSON data guided by the ontology.

    Workflow:
        1. Parse ontology profile → node types, relationship types, extraction rules
        2. Load JSON data
        3. Create constraints / indexes
        4. Create nodes (batched with UNWIND)
        5. Create CHILD_OF relationships (from sequence hierarchy)
        6. Create BELONGS_TO_MODULE relationships (from name prefix)
        7. Create TARGETED_FOR relationships (from release field)
        8. Create Jama API relationships (DERIVES_FROM, VERIFIED_BY, ASSUMES, etc.)
        9. Print summary statistics
    """

    # Maximum items per UNWIND batch
    BATCH_SIZE = 500

    def __init__(
        self,
        profile: str,
        ontology: dict,
        neo4j_cfg: dict,
        data_path: Path,
        dry_run: bool = False,
        clear_db: bool = False,
        relationships_path: Optional[Path] = None,
        folders_path: Optional[Path] = None,
        jama_cfg: Optional[dict] = None,
    ):
        self.profile = profile
        self.profile_cfg = get_profile_config(ontology, profile)
        self.neo4j_cfg = neo4j_cfg
        self.data_path = data_path
        self.dry_run = dry_run
        self.clear_db = clear_db
        # Derive module-aware cache paths from data_path if not explicitly given
        if not relationships_path or not folders_path:
            import re as _re
            _m = _re.search(r'jama_(\w+)_combined', data_path.name) if data_path else None
            _mod = _m.group(1) if _m else None
            if not relationships_path:
                relationships_path = JAMA_REQ_DIR / f"jama_{_mod}_relationships.json" if _mod else None
            if not folders_path:
                folders_path = JAMA_REQ_DIR / f"jama_{_mod}_folders.json" if _mod else None
        self.relationships_path = relationships_path
        self.folders_path = folders_path
        self.jama_cfg = jama_cfg or {}

        # Ontology lookups
        self.node_types: list = self.profile_cfg.get("node_types", [])
        self.relationship_types: list = self.profile_cfg.get("relationship_types", [])
        self.extraction_rules: dict = self.profile_cfg.get("extraction_rules", {})

        # item_type int → node type def
        self.item_type_map: Dict[int, dict] = build_item_type_map(self.node_types)

        # Determine which fields need HTML stripping
        html_rule = self.extraction_rules.get("html_to_text", {})
        self.html_strip_fields: list[str] = html_rule.get("apply_to", []) if isinstance(html_rule, dict) else []

        # Stats
        self.stats: dict[str, int] = Counter()

        # Driver
        self._driver = None

    # -- Connection ---------------------------------------------------------

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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        except Exception as exc:
            logger.error("Unexpected error connecting to Neo4j: %s", exc)
            print(f"\n  ERROR: {exc}\n")
            sys.exit(1)

        logger.info(
            "Connected to Neo4j at %s (database: %s)",
            uri,
            cfg["database"],
        )

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _run(self, cypher: str, parameters: Optional[dict] = None, **kwargs):
        """Execute a single Cypher statement with retry on transient errors."""
        return self._run_with_retry(cypher, parameters, **kwargs)

    def _run_with_retry(self, cypher: str, parameters: Optional[dict] = None, _attempt: int = 0, **kwargs):
        """Execute a read query with up to 3 retries on transient failures."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        try:
            with self._driver.session(database=db, **kwargs) as session:
                result = session.run(cypher, parameters or {})
                return [rec.data() for rec in result]
        except (ServiceUnavailable, TransientError, OSError) as exc:
            _attempt += 1
            if _attempt >= max_attempts:
                logger.error("Query failed after %d attempts: %s", max_attempts, exc)
                raise
            wait = min(2 ** _attempt, 8)
            logger.warning("Transient error (attempt %d/%d), retrying in %ds: %s",
                           _attempt, max_attempts, wait, exc)
            time.sleep(wait)
            return self._run_with_retry(cypher, parameters, _attempt=_attempt, **kwargs)

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry on transient errors."""
        return self._write_tx_with_retry(cypher, parameters)

    def _write_tx_with_retry(self, cypher: str, parameters: Optional[dict] = None, _attempt: int = 0):
        """Execute a write transaction with up to 3 retries on transient failures."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        try:
            with self._driver.session(database=db) as session:
                result = session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return result
        except (ServiceUnavailable, TransientError, OSError) as exc:
            _attempt += 1
            if _attempt >= max_attempts:
                logger.error("Write transaction failed after %d attempts: %s", max_attempts, exc)
                raise
            wait = min(2 ** _attempt, 8)
            logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                           _attempt, max_attempts, wait, exc)
            time.sleep(wait)
            return self._write_tx_with_retry(cypher, parameters, _attempt=_attempt)

    def _write_tx_counted(self, cypher: str, parameters: Optional[dict] = None) -> int:
        """Execute a write transaction and return the number of rows affected.

        The Cypher must include a ``RETURN count(*) AS cnt`` clause.
        """
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
                    logger.error("Counted write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return 0

    # -- Public entry point -------------------------------------------------

    def build(self):
        """Run the full build pipeline."""
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("Knowledge Graph Builder – profile: %s", self.profile)
        logger.info("Data source: %s", self.data_path)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        # Load data
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
                self._clear_database()

            self._create_constraints()
            self._create_nodes(data)
            self._create_synthetic_modules(data)
            self._create_folder_hierarchy(data)
            self._create_child_of_relationships(data)
            self._create_belongs_to_module_relationships()
            self._create_targeted_for_relationships()
            self._create_jama_relationships(data)
            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # -- Data Loading -------------------------------------------------------

    def _load_data(self) -> list[dict]:
        """Load JSON items from disk."""
        if not self.data_path.exists():
            logger.error("Data file not found: %s", self.data_path)
            return []

        with open(self.data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        logger.info("Loaded %d items from %s", len(data), self.data_path.name)

        # Filter to only item types defined in the ontology
        known_types = set(self.item_type_map.keys())
        filtered = [item for item in data if item.get("item_type") in known_types]
        skipped = len(data) - len(filtered)
        if skipped:
            logger.info(
                "Filtered out %d items with item_types not in ontology (e.g. attachments)",
                skipped,
            )
        logger.info("Proceeding with %d items across %d node types", len(filtered), len(known_types))
        return filtered

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, data: list[dict]):
        """Print a summary of what would be created."""
        type_counts = Counter(item["item_type"] for item in data)

        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – Profile: {self.profile}")
        print("=" * 60)
        print(f"\n  Node types to create ({len(self.item_type_map)}):")
        for jt, nt_def in sorted(self.item_type_map.items()):
            label = nt_def["name"]
            count = type_counts.get(jt, 0)
            uid = get_unique_id_property(nt_def) or "(none)"
            print(f"    :{label}  (item_type={jt})  →  {count} nodes  [merge key: {uid}]")

        # Synthetic modules (mcal only)
        synthetic = [nt for nt in self.node_types if nt.get("extraction_strategy") == "derived"]
        if synthetic:
            prefixes = set()
            for item in data:
                pfx = extract_module_prefix(item.get("name", ""))
                if pfx:
                    prefixes.add(pfx.upper())
            print(f"\n  Synthetic node types:")
            for s in synthetic:
                print(f"    :{s['name']}  →  ~{len(prefixes)} nodes (derived from name prefix)")

        print(f"\n  Relationship types defined ({len(self.relationship_types)}):")
        for rt in self.relationship_types:
            src = rt.get("extraction_source", "N/A")
            auto = src in (
                "jama_location",
                "derived_from_name_prefix",
                "release_field",
            )
            jama_api = src == "jama_relationships"
            if auto:
                marker = " [auto]"
            elif jama_api:
                _rp = self.relationships_path
                marker = " [from relationships file]" if (_rp and _rp.exists()) else " [requires fetch_jama_relationships.py]"
            else:
                marker = f" [requires data: {src}]"
            print(
                f"    (:{'/'.join(rt['from_types'])})-[:{rt['name']}]->"
                f"(:{'/'.join(rt['to_types'])})  source={src}{marker}"
            )

        print(f"\n  Total nodes to create: {len(data)} + synthetic")
        print("=" * 60 + "\n")

    # -- Clear database -----------------------------------------------------

    def _clear_database(self):
        """Delete all nodes and relationships in the target database."""
        logger.warning("Clearing ALL data in database '%s'…", self.neo4j_cfg["database"])
        self._write_tx("MATCH (n) DETACH DELETE n")
        logger.info("Database cleared.")

    # -- Constraints & Indexes -----------------------------------------------

    def _create_constraints(self):
        """
        Create uniqueness constraints for each node type's unique property.
        Also creates a general index on jama_id for fast lookups.
        """
        logger.info("Creating constraints and indexes…")

        for nt in self.node_types:
            label = nt["name"]
            uid_prop = get_unique_id_property(nt)
            if uid_prop:
                constraint_name = f"unique_{label}_{uid_prop}".lower()
                cypher = (
                    f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{uid_prop} IS UNIQUE"
                )
                try:
                    self._run(cypher)
                    logger.debug("Constraint: %s", constraint_name)
                except Exception as exc:
                    logger.warning("Constraint %s: %s", constraint_name, exc)

            # Index on jama_id for all types that have it
            has_jama_id = any(p["name"] == "jama_id" for p in nt.get("properties", []))
            if has_jama_id:
                idx_name = f"idx_{label}_jama_id".lower()
                try:
                    self._run(
                        f"CREATE INDEX {idx_name} IF NOT EXISTS FOR (n:{label}) ON (n.jama_id)"
                    )
                except Exception:
                    pass

        # Global index on global_id for cross-label lookups
        try:
            self._run(
                "CREATE INDEX idx_global_id IF NOT EXISTS "
                "FOR (n) ON (n.global_id)"
            )
        except Exception:
            pass

        logger.info("Constraints and indexes created.")

    # -- Node Creation (batched) --------------------------------------------

    def _create_nodes(self, data: list[dict]):
        """Create nodes for all items, grouped by node type, using UNWIND batching."""
        # Group items by item_type
        by_type: Dict[int, list] = defaultdict(list)
        for item in data:
            by_type[item["item_type"]].append(item)

        for jama_type, items in by_type.items():
            nt_def = self.item_type_map.get(jama_type)
            if not nt_def:
                continue

            label = nt_def["name"]
            uid_prop = get_unique_id_property(nt_def)
            if not uid_prop:
                logger.warning(
                    "Skipping %s (item_type=%d): no unique property defined.",
                    label, jama_type,
                )
                continue

            logger.info("Creating :%s nodes (%d items)…", label, len(items))

            # Extract properties for each item
            batch: list[dict] = []
            for item in items:
                props = extract_node_properties(item, nt_def, self.html_strip_fields)
                if uid_prop in props:
                    # Derive module from module_prefix for traceability queries
                    pfx = props.get("module_prefix")
                    if pfx and "module" not in props:
                        props["module"] = pfx.upper()
                    batch.append(props)

            # UNWIND in chunks
            created = 0
            for chunk in self._chunked(batch, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{label} {{{uid_prop}: props.{uid_prop}}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})
                created += len(chunk)

            self.stats[f"nodes:{label}"] = created
            logger.info("  → created/merged %d :%s nodes", created, label)

    # -- Synthetic Module Nodes (MCAL) --------------------------------------

    def _create_synthetic_modules(self, data: list[dict]):
        """Create synthetic MCALModule / Module nodes derived from name prefixes."""
        synthetic_types = [
            nt for nt in self.node_types
            if nt.get("extraction_strategy") == "derived"
        ]
        if not synthetic_types:
            return

        # Gather all unique module prefixes from item names
        prefixes: set[str] = set()
        for item in data:
            pfx = extract_module_prefix(item.get("name", ""))
            if pfx:
                prefixes.add(pfx)

        if not prefixes:
            logger.info("No module prefixes found in data – skipping synthetic nodes.")
            return

        for syn in synthetic_types:
            label = syn["name"]
            uid_prop = get_unique_id_property(syn) or "module_name"

            # Also add supported_modules from metadata (in case data doesn't cover all)
            supported = self.profile_cfg.get("metadata", {}).get("supported_modules", [])
            all_modules = prefixes | {m.upper() for m in supported} | {m for m in supported}

            # Normalise: store the canonical upper-case form
            module_nodes = []
            seen = set()
            for m in all_modules:
                canonical = m.upper()
                if canonical not in seen:
                    seen.add(canonical)
                    module_nodes.append({uid_prop: canonical})

            logger.info("Creating %d :%s synthetic nodes…", len(module_nodes), label)

            for chunk in self._chunked(module_nodes, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{label} {{{uid_prop}: props.{uid_prop}}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})

            self.stats[f"nodes:{label}"] = len(module_nodes)
            logger.info("  → created/merged %d :%s nodes", len(module_nodes), label)

    # -- CHILD_OF Relationships (from sequence hierarchy) -------------------

    # -- Folder Hierarchy (from Jama API) -----------------------------------

    def _fetch_folder_hierarchy_from_jama(self, data: list[dict]) -> dict:
        """
        Fetch the folder tree for the current module from the Jama API.

        Returns a dict with keys:
            - ``module_name``: e.g. "ADC"
            - ``module_folder_id``: Jama ID of the module root folder
            - ``folders``: list of folder dicts (jama_id, name, parent_id,
              folder_level, requirement_category)
            - ``item_to_folder``: mapping   jama_item_id → parent_folder_id
        """
        if JamaConnector is None:
            logger.error("JamaConnector not available – cannot fetch folder hierarchy.")
            return {}

        jama_cfg = self.jama_cfg
        if not jama_cfg.get("base_url"):
            logger.error("Jama config missing – cannot fetch folder hierarchy.")
            return {}

        containers = jama_cfg.get("containers", {})
        prq_container_id = containers.get("prq")
        if not prq_container_id:
            logger.error("jama.containers.prq not set in storage_config.yaml.")
            return {}

        # Detect the module name from data items
        prefixes: Counter = Counter()
        for item in data:
            pfx = extract_module_prefix(item.get("name", ""))
            if pfx:
                prefixes[pfx.upper()] += 1
        if not prefixes:
            logger.warning("Cannot detect module name from data – skipping folder fetch.")
            return {}

        module_name = prefixes.most_common(1)[0][0]
        logger.info("Detected module: %s – fetching folder hierarchy from Jama…", module_name)

        # Suppress httpx INFO logs during fetch
        httpx_logger = logging.getLogger("httpx")
        original_level = httpx_logger.level
        httpx_logger.setLevel(logging.WARNING)

        connector = JamaConnector(
            base_url=jama_cfg["base_url"],
            api_key=jama_cfg["api_key"],
            api_secret=jama_cfg["api_secret"],
            verify_ssl=jama_cfg.get("verify_ssl", True),
            timeout=jama_cfg.get("timeout", 120),
        )

        FOLDER_TYPE = 32

        try:
            # Step 1: Find the module folder under the PRQ container
            top_children = connector.get_children_items(prq_container_id)
            module_folder = None
            for child in top_children:
                if child.item_type == FOLDER_TYPE and child.name.upper() == module_name:
                    module_folder = child
                    break

            if module_folder is None:
                logger.warning(
                    "Module folder '%s' not found under PRQ container %d.",
                    module_name, prq_container_id,
                )
                return {}

            module_folder_id = module_folder.id

            # Step 2: Recursively walk the folder tree
            folders: list[dict] = []
            item_to_folder: Dict[int, int] = {}

            def _walk(parent_id: int, level: int, category: Optional[str]):
                children = connector.get_children_items(parent_id)
                for child in children:
                    if child.item_type == FOLDER_TYPE:
                        # For level-1 folders, the folder IS the category
                        cat = child.name if level == 0 else category
                        folder_dict = {
                            "jama_id": child.id,
                            "name": child.name,
                            "parent_id": parent_id,
                            "folder_level": level + 1,
                            "requirement_category": cat,
                            "document_key": child.document_key,
                        }
                        folders.append(folder_dict)
                        _walk(child.id, level + 1, cat)
                    else:
                        # Non-folder child → requirement item
                        item_to_folder[child.id] = parent_id

            # Add the module root folder itself (level 0)
            folders.append({
                "jama_id": module_folder_id,
                "name": module_folder.name,
                "parent_id": prq_container_id,
                "folder_level": 0,
                "requirement_category": None,
                "document_key": module_folder.document_key,
            })

            print(f"\n  Fetching folder hierarchy for {module_name} from Jama API…")
            _walk(module_folder_id, 0, None)

        finally:
            connector.close()
            httpx_logger.setLevel(original_level)

        result = {
            "module_name": module_name,
            "module_folder_id": module_folder_id,
            "folders": folders,
            "item_to_folder": {str(k): v for k, v in item_to_folder.items()},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # Cache to disk
        cache_path = self.folders_path
        cache_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"  Fetched {len(folders)} folders, "
            f"{len(item_to_folder)} item→folder mappings → {cache_path.name}"
        )

        return result

    def _create_folder_hierarchy(self, data: list[dict]):
        """
        Create Folder nodes, CHILD_OF between folders, BELONGS_TO_FOLDER
        from requirements to their parent folders, and set
        ``requirement_category`` on each PRQ node.

        Uses cached data from disk if available; otherwise fetches live
        from the Jama API.

        Ontology references:
            - Node type:  Folder  (jama_item_type 32)
            - Relationship: BELONGS_TO_FOLDER
            - Relationship: CHILD_OF  (for folder→folder hierarchy)
            - Property:  ProductRequirement.requirement_category
        """
        # Check ontology defines BELONGS_TO_FOLDER
        btf_def = next(
            (r for r in self.relationship_types if r["name"] == "BELONGS_TO_FOLDER"),
            None,
        )
        if not btf_def:
            logger.info("No BELONGS_TO_FOLDER relationship defined – skipping folder hierarchy.")
            return

        # Load or fetch folder data
        cache_path = self.folders_path
        folder_data: dict = {}

        if cache_path.exists():
            logger.info("Loading cached folder hierarchy from %s …", cache_path.name)
            with open(cache_path, "r", encoding="utf-8") as fh:
                folder_data = json.load(fh)
            logger.info(
                "Loaded %d folders, %d item→folder mappings.",
                len(folder_data.get("folders", [])),
                len(folder_data.get("item_to_folder", {})),
            )
        else:
            logger.info("No cached folder data – fetching from Jama API…")
            folder_data = self._fetch_folder_hierarchy_from_jama(data)
            if not folder_data:
                logger.warning("Could not fetch folder hierarchy. Skipping.")
                return

        folders = folder_data.get("folders", [])
        item_to_folder = folder_data.get("item_to_folder", {})
        module_folder_id = folder_data.get("module_folder_id")

        if not folders:
            logger.info("No folders found in hierarchy data.")
            return

        # --- 1. Create Folder nodes ---
        logger.info("Creating %d Folder nodes…", len(folders))
        folder_nodes = []
        for f in folders:
            node = {
                "jama_id": f["jama_id"],
                "name": f["name"],
                "folder_level": f.get("folder_level", 0),
            }
            if f.get("requirement_category"):
                node["requirement_category"] = f["requirement_category"]
            if f.get("document_key"):
                node["folder_id"] = f["document_key"]
            folder_nodes.append(node)

        for chunk in self._chunked(folder_nodes, self.BATCH_SIZE):
            cypher = (
                "UNWIND $items AS props "
                "MERGE (n:Folder {jama_id: props.jama_id}) "
                "ON CREATE SET n.global_id = randomUUID() "
                "SET n += props"
            )
            self._write_tx(cypher, {"items": chunk})

        self.stats["nodes:Folder"] = len(folder_nodes)
        logger.info("  → created/merged %d Folder nodes", len(folder_nodes))

        # --- 2. Create CHILD_OF between folders ---
        folder_edges = []
        for f in folders:
            parent_id = f.get("parent_id")
            # Only create CHILD_OF if the parent is also a folder we created
            folder_ids = {ff["jama_id"] for ff in folders}
            if parent_id and parent_id in folder_ids:
                folder_edges.append({
                    "child_id": f["jama_id"],
                    "parent_id": parent_id,
                })

        if folder_edges:
            for chunk in self._chunked(folder_edges, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (child:Folder {jama_id: e.child_id}) "
                    "MATCH (parent:Folder {jama_id: e.parent_id}) "
                    "MERGE (child)-[:CHILD_OF]->(parent)"
                )
                self._write_tx(cypher, {"edges": chunk})

            self.stats["rel:CHILD_OF(folder)"] = len(folder_edges)
            logger.info("  → created %d CHILD_OF (folder→folder) relationships", len(folder_edges))

        # --- 3. Build folder jama_id → requirement_category lookup ---
        folder_category: Dict[int, str] = {}
        for f in folders:
            if f.get("requirement_category"):
                folder_category[f["jama_id"]] = f["requirement_category"]

        # --- 4. Create BELONGS_TO_FOLDER & set requirement_category on PRQs ---
        btf_edges = []
        category_updates: Dict[str, list] = defaultdict(list)  # category → [jama_id, ...]

        for item_id_str, folder_id in item_to_folder.items():
            item_id = int(item_id_str)
            btf_edges.append({
                "item_id": item_id,
                "folder_id": folder_id,
            })
            # Determine the requirement_category for this item
            cat = folder_category.get(folder_id)
            if cat:
                category_updates[cat].append(item_id)

        if btf_edges:
            # Use generic match on jama_id across requirement types
            for chunk in self._chunked(btf_edges, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (item {jama_id: e.item_id}) "
                    "MATCH (folder:Folder {jama_id: e.folder_id}) "
                    "MERGE (item)-[:BELONGS_TO_FOLDER]->(folder)"
                )
                self._write_tx(cypher, {"edges": chunk})

            self.stats["rel:BELONGS_TO_FOLDER"] = len(btf_edges)
            logger.info("  → created %d BELONGS_TO_FOLDER relationships", len(btf_edges))

        # --- 5. Set requirement_category property on PRQ nodes ---
        total_categorised = 0
        for cat, item_ids in category_updates.items():
            for chunk in self._chunked(item_ids, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $ids AS jid "
                    "MATCH (n:ProductRequirement {jama_id: jid}) "
                    "SET n.requirement_category = $category"
                )
                self._write_tx(cypher, {"ids": chunk, "category": cat})
                total_categorised += len(chunk)

        if total_categorised:
            logger.info("  → set requirement_category on %d PRQ nodes", total_categorised)
            # Log category distribution
            for cat, ids in sorted(category_updates.items()):
                logger.info("      %s: %d PRQs", cat, len(ids))

    def _create_child_of_relationships(self, data: list[dict]):
        """
        Derive CHILD_OF relationships using the ``sequence`` field.

        Sequence encodes hierarchy: "2.1.3.5" means the 5th child of 2.1.3.
        Two items share a parent–child relation when one's sequence is a
        direct prefix of the other.

        Strategy: for each item, compute the parent sequence (strip last
        segment), look up the item with that sequence, create edge.
        We build an in-memory index: sequence → (jama_id, item_type).
        """
        # Check if CHILD_OF is defined in this profile
        child_of_def = next(
            (r for r in self.relationship_types if r["name"] == "CHILD_OF"),
            None,
        )
        if not child_of_def:
            logger.info("No CHILD_OF relationship defined – skipping hierarchy.")
            return

        logger.info("Building CHILD_OF hierarchy from sequence fields…")

        # Build sequence index
        seq_index: Dict[str, dict] = {}
        for item in data:
            seq = item.get("sequence", "")
            if seq:
                seq_index[seq] = item

        # For each item, find its parent
        edges: list[dict] = []
        for item in data:
            seq = item.get("sequence", "")
            if not seq or "." not in seq:
                continue  # root-level or no sequence

            parent_seq = seq.rsplit(".", 1)[0]
            parent = seq_index.get(parent_seq)
            if parent is None:
                continue

            child_type = self.item_type_map.get(item["item_type"])
            parent_type = self.item_type_map.get(parent["item_type"])
            if not child_type or not parent_type:
                continue

            child_label = child_type["name"]
            parent_label = parent_type["name"]
            child_uid = get_unique_id_property(child_type)
            parent_uid = get_unique_id_property(parent_type)

            if not child_uid or not parent_uid:
                continue

            child_key = self._get_uid_value(item, child_type, child_uid)
            parent_key = self._get_uid_value(parent, parent_type, parent_uid)

            if child_key and parent_key:
                edges.append({
                    "child_label": child_label,
                    "parent_label": parent_label,
                    "child_uid": child_uid,
                    "parent_uid": parent_uid,
                    "child_key": child_key,
                    "parent_key": parent_key,
                    "sequence": seq,
                })

        if not edges:
            logger.info("  No CHILD_OF edges derived.")
            return

        # Group by (child_label, parent_label) for efficient Cypher
        by_combo: Dict[Tuple[str, str, str, str], list] = defaultdict(list)
        for e in edges:
            key = (e["child_label"], e["parent_label"], e["child_uid"], e["parent_uid"])
            by_combo[key].append(e)

        total_attempted = 0
        total_created = 0
        for (cl, pl, cu, pu), edge_list in by_combo.items():
            batch = [
                {"child_key": e["child_key"], "parent_key": e["parent_key"], "sequence": e["sequence"]}
                for e in edge_list
            ]
            for chunk in self._chunked(batch, self.BATCH_SIZE):
                total_attempted += len(chunk)
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (child:{cl} {{{cu}: e.child_key}}) "
                    f"MATCH (parent:{pl} {{{pu}: e.parent_key}}) "
                    f"MERGE (child)-[r:CHILD_OF]->(parent) "
                    f"SET r.sequence = e.sequence "
                    f"RETURN count(*) AS cnt"
                )
                cnt = self._write_tx_counted(cypher, {"edges": chunk})
                total_created += cnt

        if total_attempted != total_created:
            logger.warning(
                "  CHILD_OF: attempted %d, actually matched %d (%.0f%% miss rate)",
                total_attempted, total_created,
                (1 - total_created / max(total_attempted, 1)) * 100,
            )
        self.stats["rel:CHILD_OF"] = total_created
        logger.info("  → created %d CHILD_OF relationships (of %d attempted)", total_created, total_attempted)

    # -- BELONGS_TO_MODULE Relationships ------------------------------------

    def _create_belongs_to_module_relationships(self):
        """
        Link items that have a module_prefix property to the corresponding
        MCALModule / Module synthetic node via BELONGS_TO_MODULE / BELONGS_TO.
        """
        # Determine which relationship name and target label to use
        btm_def = next(
            (r for r in self.relationship_types if r["name"] in ("BELONGS_TO_MODULE", "BELONGS_TO")),
            None,
        )
        if not btm_def:
            logger.info("No BELONGS_TO_MODULE/BELONGS_TO relationship defined – skipping.")
            return

        rel_name = btm_def["name"]
        target_labels = btm_def.get("to_types", [])
        target_label = target_labels[0] if target_labels else "MCALModule"

        # Determine module_name uid field on the target
        target_type = next((nt for nt in self.node_types if nt["name"] == target_label), None)
        mod_uid = "module_name"
        if target_type:
            mod_uid = get_unique_id_property(target_type) or "module_name"

        source_labels = btm_def.get("from_types", [])
        logger.info("Creating %s relationships for %s → :%s…", rel_name, source_labels, target_label)

        total = 0
        for src_label in source_labels:
            cypher = (
                f"MATCH (n:{src_label}) "
                f"WHERE n.module_prefix IS NOT NULL "
                f"MATCH (m:{target_label} {{{mod_uid}: toUpper(n.module_prefix)}}) "
                f"MERGE (n)-[:{rel_name}]->(m) "
                f"RETURN count(*) AS cnt"
            )
            cnt = self._write_tx_counted(cypher)
            total += cnt
            logger.info("  → :%s – %d edges", src_label, cnt)

        self.stats[f"rel:{rel_name}"] = total

    # -- JAMA API Relationships (DERIVES_FROM, VERIFIED_BY, etc.) ----------

    def _fetch_relationships_from_jama(self, data: list[dict]) -> list[dict]:
        """
        Fetch relationships from the Jama REST API using parallel threads.

        Returns a list of normalised relationship dicts.
        Caches the result to DEFAULT_RELATIONSHIPS_PATH for future runs.
        """
        if JamaConnector is None:
            logger.error(
                "JamaConnector not available. Cannot fetch relationships.\n"
                "  Ensure src/IngestionPipeline/Connectors/JamaConnector.py is accessible."
            )
            return []

        jama_cfg = self.jama_cfg
        if not jama_cfg.get("base_url"):
            logger.error(
                "Jama configuration missing in storage_config.yaml.\n"
                "  Add a 'jama' section with base_url, api_key, api_secret."
            )
            return []

        # Build item-ID set for internal tagging
        item_id_set: Set[int] = {item["id"] for item in data if "id" in item}
        item_type_index: Dict[int, int] = {
            item["id"]: item["item_type"] for item in data if "id" in item
        }
        items_with_ids = [item for item in data if "id" in item]
        total = len(items_with_ids)

        max_workers = jama_cfg.get("max_workers", 10)

        print(f"\n  Fetching relationships from Jama API ({total} items, {max_workers} threads)…")

        # Suppress ALL console logging during the progress bar so that
        # logger output (httpx, aice.ingestion.jama, etc.) doesn't
        # interleave with the \r-based single-line progress bar.
        _root_logger = logging.getLogger()
        _saved_root_level = _root_logger.level
        _root_logger.setLevel(logging.CRITICAL)

        # Thread-safe containers
        seen_ids: Set[int] = set()
        seen_lock = threading.Lock()
        relationships: list[dict] = []
        rels_lock = threading.Lock()
        errors = 0
        errors_lock = threading.Lock()
        _error_samples: list[str] = []  # collect first N error messages for diagnostics
        _MAX_ERROR_SAMPLES = 5

        progress = ProgressBar(total, prefix="Fetching")

        def _record_error(exc: Exception, item_id: int, direction: str):
            nonlocal errors
            with errors_lock:
                errors += 1
                if len(_error_samples) < _MAX_ERROR_SAMPLES:
                    _error_samples.append(
                        f"  item {item_id} ({direction}): {type(exc).__name__}: {exc}"
                    )

        def _fetch_one(item: dict) -> None:
            item_id = item["id"]
            connector = JamaConnector(
                base_url=jama_cfg["base_url"],
                api_key=jama_cfg["api_key"],
                api_secret=jama_cfg["api_secret"],
                verify_ssl=jama_cfg.get("verify_ssl", True),
                timeout=jama_cfg.get("timeout", 120),
            )
            local_rels = []
            try:
                # Downstream
                try:
                    for rel in connector.get_downstream_relationships(item_id):
                        rid = rel.get("id")
                        if rid:
                            with seen_lock:
                                if rid in seen_ids:
                                    continue
                                seen_ids.add(rid)
                            local_rels.append(_enrich_rel(rel, item_id_set))
                except Exception as exc:
                    _record_error(exc, item_id, "downstream")

                # Upstream
                try:
                    for rel in connector.get_upstream_relationships(item_id):
                        rid = rel.get("id")
                        if rid:
                            with seen_lock:
                                if rid in seen_ids:
                                    continue
                                seen_ids.add(rid)
                            local_rels.append(_enrich_rel(rel, item_id_set))
                except Exception as exc:
                    _record_error(exc, item_id, "upstream")
            finally:
                connector.close()

            if local_rels:
                with rels_lock:
                    relationships.extend(local_rels)
            progress.update()

        def _enrich_rel(rel: dict, ids: Set[int]) -> dict:
            from_id = rel.get("fromItem")
            to_id = rel.get("toItem")
            return {
                "relationship_id": rel.get("id"),
                "from_item": from_id,
                "to_item": to_id,
                "relationship_type": rel.get("relationshipType"),
                "internal": (from_id in ids and to_id in ids),
                "suspect": rel.get("suspect", False),
            }

        # Run in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_one, item) for item in items_with_ids]
            for f in as_completed(futures):
                f.result()  # raise exceptions

        progress.finish()

        # Restore logging
        _root_logger.setLevel(_saved_root_level)

        logger.info(
            "Fetched %d unique relationships (%d errors)", len(relationships), errors
        )

        # Report error samples so users can diagnose API issues
        if _error_samples:
            logger.warning("Sample errors from Jama API fetch (%d total errors):", errors)
            for sample in _error_samples:
                logger.warning(sample)

        # Cache to disk
        cache_path = self.relationships_path
        internal_count = sum(1 for r in relationships if r["internal"])
        rel_type_counts: Dict[int, int] = {}
        for r in relationships:
            rt = r["relationship_type"]
            rel_type_counts[rt] = rel_type_counts.get(rt, 0) + 1

        output = {
            "metadata": {
                "description": "Jama item relationships extracted via REST API.",
                "total_relationships": len(relationships),
                "internal_relationships": internal_count,
                "external_relationships": len(relationships) - internal_count,
                "relationship_type_counts": rel_type_counts,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "item_type_index": {str(k): v for k, v in item_type_index.items()},
            "relationships": relationships,
        }
        cache_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Cached {len(relationships)} relationships → {cache_path.name}")

        return relationships

    def _create_jama_relationships(self, data: list[dict]):
        """
        Create relationships sourced from the Jama REST API relationship
        endpoints (``extraction_source: jama_relationships``).

        If a cached relationships JSON exists, it is loaded directly.
        Otherwise, relationships are fetched live from the Jama API using
        parallel threads and cached to disk for future runs.
        """
        # Identify relationship types that require Jama API data
        jama_rel_types = [
            rt for rt in self.relationship_types
            if rt.get("extraction_source") == "jama_relationships"
        ]
        if not jama_rel_types:
            logger.info("No jama_relationships-sourced relationship types in this profile.")
            return

        # Determine relationships source: cached file or live API
        rel_path = self.relationships_path
        raw_rels: list[dict] = []

        if rel_path.exists():
            logger.info("Loading cached Jama relationships from %s …", rel_path.name)
            with open(rel_path, "r", encoding="utf-8") as fh:
                rel_data = json.load(fh)
            raw_rels = rel_data.get("relationships", [])
            logger.info("Loaded %d cached relationships.", len(raw_rels))
        else:
            # Fetch live from Jama API
            logger.info("No cached relationships file found – fetching from Jama API…")
            raw_rels = self._fetch_relationships_from_jama(data)
            if not raw_rels:
                logger.warning("No relationships fetched. Skipping Jama relationship creation.")
                return

        if not raw_rels:
            logger.warning("Relationships data is empty – no edges to create.")
            return

        # Build jama_id → (item_type, label, uid_prop) index from the loaded data
        id_index: Dict[int, dict] = {}
        for item in data:
            jid = item.get("id")
            jtype = item.get("item_type")
            nt_def = self.item_type_map.get(jtype)
            if jid and nt_def:
                uid_prop = get_unique_id_property(nt_def)
                uid_val = self._get_uid_value(item, nt_def, uid_prop) if uid_prop else None
                id_index[jid] = {
                    "item_type": jtype,
                    "label": nt_def["name"],
                    "uid_prop": uid_prop,
                    "uid_val": uid_val,
                }

        # Build a lookup: (from_label, to_label) → relationship name(s)
        label_pair_to_rel: Dict[Tuple[str, str], str] = {}
        for rt in jama_rel_types:
            for ft in rt.get("from_types", []):
                for tt in rt.get("to_types", []):
                    label_pair_to_rel[(ft, tt)] = rt["name"]

        # Process each relationship
        edges_by_type: Dict[str, list] = defaultdict(list)
        skipped_external = 0
        skipped_no_match = 0

        for rel in raw_rels:
            from_id = rel.get("from_item")
            to_id = rel.get("to_item")

            if from_id not in id_index or to_id not in id_index:
                skipped_external += 1
                continue

            from_info = id_index[from_id]
            to_info = id_index[to_id]

            from_label = from_info["label"]
            to_label = to_info["label"]

            # Find the matching ontology relationship
            rel_name = label_pair_to_rel.get((from_label, to_label))

            if not rel_name:
                # Try the reverse direction (Jama may store the inverse)
                rel_name_rev = label_pair_to_rel.get((to_label, from_label))
                if rel_name_rev:
                    from_info, to_info = to_info, from_info
                    rel_name = rel_name_rev
                else:
                    skipped_no_match += 1
                    continue

            edges_by_type[rel_name].append({
                "from_label": from_info["label"],
                "from_uid_prop": from_info["uid_prop"],
                "from_uid_val": from_info["uid_val"],
                "to_label": to_info["label"],
                "to_uid_prop": to_info["uid_prop"],
                "to_uid_val": to_info["uid_val"],
                "suspect": rel.get("suspect", False),
            })

        if skipped_external:
            logger.info(
                "  Skipped %d relationships where one or both ends are outside the dataset.",
                skipped_external,
            )
        if skipped_no_match:
            logger.info(
                "  Skipped %d relationships with no matching ontology relationship type.",
                skipped_no_match,
            )

        # Create edges in Neo4j, grouped by relationship type and label combo
        for rel_name, edges in edges_by_type.items():
            by_combo: Dict[Tuple, list] = defaultdict(list)
            for e in edges:
                key = (e["from_label"], e["to_label"], e["from_uid_prop"], e["to_uid_prop"])
                by_combo[key].append(e)

            total = 0
            total_attempted = 0
            for (fl, tl, fu, tu), edge_list in by_combo.items():
                batch = [
                    {
                        "from_key": e["from_uid_val"],
                        "to_key": e["to_uid_val"],
                        "suspect": e.get("suspect", False),
                    }
                    for e in edge_list
                    if e["from_uid_val"] and e["to_uid_val"]
                ]

                for chunk in self._chunked(batch, self.BATCH_SIZE):
                    total_attempted += len(chunk)
                    cypher = (
                        f"UNWIND $edges AS e "
                        f"MATCH (from_node:{fl} {{{fu}: e.from_key}}) "
                        f"MATCH (to_node:{tl} {{{tu}: e.to_key}}) "
                        f"MERGE (from_node)-[r:{rel_name}]->(to_node) "
                        f"SET r.suspect = e.suspect "
                        f"RETURN count(*) AS cnt"
                    )
                    cnt = self._write_tx_counted(cypher, {"edges": chunk})
                    total += cnt

            if total_attempted != total:
                logger.warning(
                    "  %s: attempted %d, actually matched %d (%.0f%% miss rate)",
                    rel_name, total_attempted, total,
                    (1 - total / max(total_attempted, 1)) * 100,
                )
            self.stats[f"rel:{rel_name}"] = total
            logger.info("  → created %d %s relationships (of %d attempted)", total, rel_name, total_attempted)

        # Summary
        total_created = sum(
            v for k, v in self.stats.items()
            if k.startswith("rel:") and k.split(":", 1)[1] in edges_by_type
        )
        logger.info(
            "Jama relationship creation complete: %d edges across %d types",
            total_created, len(edges_by_type),
        )

    # -- TARGETED_FOR Relationships -----------------------------------------

    def _create_targeted_for_relationships(self):
        """
        Link items that have a ``release`` field to SoftwareRelease nodes
        via TARGETED_FOR.
        """
        tf_def = next(
            (r for r in self.relationship_types if r["name"] == "TARGETED_FOR"),
            None,
        )
        if not tf_def:
            return

        target_label = tf_def["to_types"][0] if tf_def.get("to_types") else "SoftwareRelease"
        target_type = next((nt for nt in self.node_types if nt["name"] == target_label), None)
        target_uid = get_unique_id_property(target_type) if target_type else "release_id"

        logger.info("Creating TARGETED_FOR relationships…")

        # For each source type with a release property, link to matching SoftwareRelease.name
        source_labels = tf_def.get("from_types", [])
        total = 0
        for src_label in source_labels:
            cypher = (
                f"MATCH (n:{src_label}) "
                f"WHERE n.release IS NOT NULL AND n.release <> '' "
                f"MATCH (rel:{target_label}) "
                f"WHERE rel.name = n.release "
                f"MERGE (n)-[:TARGETED_FOR]->(rel)"
            )
            self._run(cypher)

            count_cypher = (
                f"MATCH (n:{src_label})-[r:TARGETED_FOR]->(:{target_label}) "
                f"RETURN count(r) AS cnt"
            )
            counts = self._run(count_cypher)
            cnt = counts[0]["cnt"] if counts else 0
            total += cnt

        self.stats["rel:TARGETED_FOR"] = total
        logger.info("  → created %d TARGETED_FOR relationships", total)

    # -- Utilities ----------------------------------------------------------

    def _get_uid_value(self, item: dict, nt_def: dict, uid_prop: str) -> Optional[Any]:
        """Retrieve the value for the unique-id property from an item.

        Preserves the original data type so that Neo4j MATCH clauses
        compare int-to-int and str-to-str correctly.
        """
        props_defs = nt_def.get("properties", [])
        for pdef in props_defs:
            if pdef["name"] == uid_prop:
                dt = pdef.get("data_type", "string")
                jf = pdef.get("jama_field")
                if jf:
                    val = item.get("raw_fields", {}).get(jf)
                    if val is None:
                        val = item.get(jf)
                    if val is None:
                        return None
                    # Coerce to match the type stored by extract_node_properties
                    if dt == "integer":
                        try:
                            return int(val)
                        except (ValueError, TypeError):
                            return str(val)
                    return str(val)
        # Fallback: check top-level keys
        val = item.get(uid_prop)
        return val if val is not None else None

    @staticmethod
    def _chunked(lst: list, size: int):
        """Yield successive chunks of *lst* with at most *size* elements."""
        for i in range(0, len(lst), size):
            yield lst[i : i + size]

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        """Print build statistics."""
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – Profile: {self.profile}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        # Node stats
        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<30s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<31s}  {total_nodes:>6,d}")

        # Relationship stats
        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<30s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<31s}  {total_rels:>6,d}")

        # Get live DB stats
        try:
            db_stats = self._run(
                "MATCH (n) RETURN count(n) AS nodes "
            )
            rel_count = self._run(
                "MATCH ()-[r]->() RETURN count(r) AS rels"
            )
            labels = self._run(
                "CALL db.labels() YIELD label RETURN collect(label) AS labels"
            )
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# ILLD Knowledge Graph Builder
# ---------------------------------------------------------------------------

class ILLDKnowledgeGraphBuilder:
    """
    Builds a Neo4j knowledge graph from processed JSON/MD files using
    the ILLD parser pipeline (illd_parsers.py).

    Unlike the MCAL builder which reads Jama JSON, this pipeline ingests
    pre-processed files (C analysis, SWA headers, register defs, hardware
    specs, requirement docs, PUML patterns) produced by the raw-input
    processing stage.

    Workflow:
        1. Discover processed JSON/MD files under data/<module>/processed/
        2. Run all applicable parsers → Node / Edge objects
        3. Generate derived + semantic relationships
        4. Batch-insert into Neo4j using MERGE
        5. Print summary statistics

    Usage::

        python build_knowledge_graph.py --profile illd --module CXPI --clear
    """

    BATCH_SIZE = 500

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        data_path: Path,
        dry_run: bool = False,
        clear_db: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.data_path = data_path
        self.dry_run = dry_run
        self.clear_db = clear_db
        self.stats: dict = Counter()
        self._driver = None

    # -- Connection ---------------------------------------------------------

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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("ILLD write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a read query with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("ILLD read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # -- Public entry point -------------------------------------------------

    def build(self):
        """Run the full ILLD build pipeline."""
        if illd_parse_all_files is None:
            logger.error(
                "illd_parsers module not found. Ensure src/HybridRAG/code/illd_parsers.py exists (on sys.path)."
            )
            sys.exit(1)

        t0 = time.time()

        logger.info("=" * 60)
        logger.info("ILLD Knowledge Graph Builder – module: %s", self.module)
        logger.info("Data path: %s", self.data_path)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.data_path.exists():
            logger.error("Data path not found: %s", self.data_path)
            print(
                f"\n  ERROR: Processed data path not found:\n"
                f"  {self.data_path}\n\n"
                f"  Place processed JSON/MD files there, or run:\n"
                f"    python pdf_pipeline.py --module {self.module}\n\n"
                f"  See data/README.md for expected file naming.\n"
            )
            sys.exit(1)

        # Step 1: Parse all files
        logger.info("Step 1/4: Parsing processed files…")
        nodes, edges = illd_parse_all_files(str(self.data_path), self.module)
        logger.info("Parsed %d nodes, %d edges", len(nodes), len(edges))

        if not nodes:
            logger.warning("No nodes produced from parsing. Check that files exist in %s", self.data_path)
            return

        if self.dry_run:
            self._preview(nodes, edges)
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            if self.clear_db:
                logger.warning("Clearing ALL data in database '%s'…", self.neo4j_cfg["database"])
                self._write_tx("MATCH (n) DETACH DELETE n")
                logger.info("Database cleared.")

            # Step 3: Create nodes
            logger.info("Step 2/4: Creating %d nodes…", len(nodes))
            self._create_nodes(nodes)

            # Step 4: Create edges
            logger.info("Step 3/4: Creating %d edges…", len(edges))
            self._create_edges(edges)

            # Step 5: Summary
            logger.info("Step 4/4: Printing summary…")
            self._print_summary(time.time() - t0)

        finally:
            self._close()

    # -- Node Creation (batched by type) ------------------------------------

    def _create_nodes(self, nodes: List):
        """Batch create nodes grouped by type using UNWIND + MERGE."""
        by_type: Dict[str, list] = defaultdict(list)
        for n in nodes:
            props = {k: v for k, v in n.properties.items() if v is not None}
            # Convert non-primitive values to JSON strings
            for k, v in list(props.items()):
                if isinstance(v, (list, dict, set, frozenset)):
                    import json as _json
                    props[k] = _json.dumps(v, default=str)
            props["id"] = n.id
            by_type[n.type].append(props)

        for ntype, items in by_type.items():
            logger.info("  Creating :%s (%d nodes)…", ntype, len(items))
            for chunk in self._chunked(items, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{ntype} {{id: props.id}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})
            self.stats[f"nodes:{ntype}"] = len(items)

    # -- Edge Creation (batched by type) ------------------------------------

    def _create_edges(self, edges: List):
        """Batch create edges using MATCH + MERGE."""
        by_type: Dict[str, list] = defaultdict(list)
        for e in edges:
            props = {k: v for k, v in e.properties.items() if v is not None}
            for k, v in list(props.items()):
                if isinstance(v, (list, dict, set, frozenset)):
                    import json as _json
                    props[k] = _json.dumps(v, default=str)
            by_type[e.relationship_type].append({
                "source_id": e.source_id,
                "target_id": e.target_id,
                "props": props,
            })

        for rtype, items in by_type.items():
            logger.info("  Creating :%s (%d edges)…", rtype, len(items))
            for chunk in self._chunked(items, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (a {{id: e.source_id}}) "
                    f"MATCH (b {{id: e.target_id}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) "
                    f"SET r += e.props"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:{rtype}"] = len(items)

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, nodes: List, edges: List):
        node_counts: Dict[str, int] = Counter(n.type for n in nodes)
        edge_counts: Dict[str, int] = Counter(e.relationship_type for e in edges)

        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – ILLD Profile, Module: {self.module}")
        print("=" * 60)
        print(f"\n  Node types ({len(node_counts)}):")
        for ntype, cnt in sorted(node_counts.items()):
            print(f"    :{ntype:<30s}  {cnt:>6,d}")
        print(f"    {'TOTAL':<31s}  {sum(node_counts.values()):>6,d}")

        print(f"\n  Relationship types ({len(edge_counts)}):")
        for rtype, cnt in sorted(edge_counts.items()):
            print(f"    :{rtype:<30s}  {cnt:>6,d}")
        print(f"    {'TOTAL':<31s}  {sum(edge_counts.values()):>6,d}")
        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – ILLD Profile, Module: {self.module}")
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

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# SWA Knowledge Graph Builder
# ---------------------------------------------------------------------------

class SWAKnowledgeGraphBuilder:
    """
    Builds SWA (Software Architecture) nodes and relationships in the
    MCAL Neo4j database from parsed SWA markdown section files.

    This builder is **document-agnostic** and **section-agnostic**:
    it auto-detects the module from filenames, infers content types
    from structural patterns in the markdown (headings, [req] tags,
    table layouts, stereotypes), and works for any MCAL module's SWA
    document (ADC, SPI, CAN, GPT, …).

    Node types created (from the MCAL ontology):
        - SWA_ArchitecturalDecision
        - SWA_HwPeripheral
        - SWA_SwDependency
        - SWA_ConfigContainer
        - SWA_ConfigParam
        - SWA_Function
        - SWA_DataType
        - SWA_Macro

    Relationships created:
        - SWA_ARCH_DECISION_TRACES_TO   (decision → PRQ)
        - SWA_ARCH_DECISION_PARENT      (decision hierarchy)
        - SWA_ARCH_DECISION_BELONGS_TO_MODULE
        - SWA_DEPENDS_ON_HW             (module → HW peripheral)
        - SWA_CALLS_EXTERNAL            (module → SW dependency)
        - SWA_DEPENDENCY_PROVIDED_BY     (SW dep → provider module)
        - SWA_CONTAINS_CONTAINER        (container hierarchy)
        - SWA_CONTAINS_PARAM            (container → parameter)
        - SWA_CONFIG_TRACES_TO          (container/param → PRQ)

    Usage::

        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swa
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swa --dry-run
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swa --swa-dir path/to/swa/
    """

    BATCH_SIZE = 500

    # Unique-key property for each SWA node type
    _UID_MAP = {
        "SWA_ArchitecturalDecision": "decision_id",
        "SWA_HwPeripheral":         "peripheral_name",
        "SWA_SwDependency":         "api_name",
        "SWA_ConfigContainer":      "container_name",
        "SWA_ConfigParam":          "param_name",
        "SWA_Function":             "function_name",
        "SWA_DataType":             "type_name",
        "SWA_Macro":                "macro_name",
    }

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        swa_dir: Path,
        dry_run: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.swa_dir = swa_dir
        self.dry_run = dry_run
        self.stats: dict = Counter()
        self._driver = None

    # -- Connection ---------------------------------------------------------

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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("SWA write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a read query with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("SWA read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # -- Public entry point -------------------------------------------------

    def build(self):
        """Run the full SWA ingestion pipeline."""
        if parse_swa_directory is None:
            logger.error(
                "swa_parsers module not found. Ensure "
                "src/HybridRAG/code/KG/swa_parsers.py exists."
            )
            sys.exit(1)

        t0 = time.time()

        logger.info("=" * 60)
        logger.info("SWA Knowledge Graph Builder – module: %s", self.module)
        logger.info("SWA directory: %s", self.swa_dir)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.swa_dir.exists():
            logger.error("SWA directory not found: %s", self.swa_dir)
            print(
                f"\n  ERROR: SWA directory not found:\n"
                f"  {self.swa_dir}\n\n"
                f"  Convert SWA PDF sections first using:\n"
                f"    python prepare_markdowns.py --input <SWA_PDF> ...\n"
            )
            sys.exit(1)

        # Step 1: Parse SWA markdown files
        logger.info("Step 1/4: Parsing SWA markdown files…")
        parsed = parse_swa_directory(
            swa_dir=self.swa_dir,
            module=self.module,
        )

        if not parsed or all(k.startswith("_") for k in parsed):
            logger.warning("No SWA nodes parsed from %s", self.swa_dir)
            return

        if self.dry_run:
            self._preview(parsed)
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            # Step 3: Create constraints / indexes for SWA node types
            logger.info("Step 2/4: Creating SWA constraints and indexes…")
            self._create_constraints(parsed)

            # Step 4: Create nodes
            logger.info("Step 3/4: Creating SWA nodes…")
            self._create_nodes(parsed)

            # Step 5: Create relationships
            logger.info("Step 4/4: Creating SWA relationships…")
            self._create_relationships(parsed)

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # -- Constraints --------------------------------------------------------

    def _create_constraints(self, parsed: dict):
        """Create uniqueness constraints for SWA node types."""
        for node_type, uid_prop in self._UID_MAP.items():
            if node_type not in parsed:
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

    # -- Node Creation (batched by type) ------------------------------------

    def _create_nodes(self, parsed: dict):
        """Create nodes for each SWA node type using UNWIND + MERGE."""
        for node_type, items in parsed.items():
            if node_type.startswith("_"):  # skip edge lists
                continue

            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                logger.warning("Unknown SWA node type: %s – skipping", node_type)
                continue

            logger.info("  Creating :%s (%d nodes)…", node_type, len(items))

            # Prepare items for Neo4j: convert lists to JSON strings,
            # remove None values
            batch = []
            for item in items:
                clean = {}
                for k, v in item.items():
                    if v is None:
                        continue
                    if isinstance(v, (list, dict, set, frozenset)):
                        clean[k] = json.dumps(v, default=str)
                    else:
                        clean[k] = v
                batch.append(clean)

            for chunk in self._chunked(batch, self.BATCH_SIZE):
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
        """Create all SWA relationships."""
        self._create_arch_decision_traces_to(parsed)
        self._create_arch_decision_parent(parsed)
        self._create_arch_decision_belongs_to_module(parsed)
        self._create_depends_on_hw(parsed)
        self._create_sw_dependency_rels(parsed)
        self._create_config_hierarchy(parsed)
        self._create_config_traces_to(parsed)
        self._create_exported_interface_rels(parsed)
        self._create_new_type_module_membership(parsed)
        self._create_new_type_traces_to_prq(parsed)

    def _create_arch_decision_traces_to(self, parsed: dict):
        """
        SWA_ArchitecturalDecision → ProductRequirement via prq_references.
        """
        decisions = parsed.get("SWA_ArchitecturalDecision", [])
        edges = []
        for d in decisions:
            prqs_raw = d.get("prq_references")
            if not prqs_raw:
                continue
            # prq_references may be a string (JSON) or list
            prqs = json.loads(prqs_raw) if isinstance(prqs_raw, str) else prqs_raw
            for prq_id in prqs:
                edges.append({
                    "decision_id": d["decision_id"],
                    "requirement_id": prq_id,
                })

        if not edges:
            return

        logger.info("  Creating SWA_ARCH_DECISION_TRACES_TO (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (d:SWA_ArchitecturalDecision {decision_id: e.decision_id}) "
                "MATCH (p:ProductRequirement {requirement_id: e.requirement_id}) "
                "MERGE (d)-[:SWA_ARCH_DECISION_TRACES_TO]->(p)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWA_ARCH_DECISION_TRACES_TO"] = len(edges)

    def _create_arch_decision_parent(self, parsed: dict):
        """Hierarchical parent-child between decisions."""
        edges = parsed.get("_edges_SWA_ARCH_DECISION_PARENT", [])
        if not edges:
            return

        logger.info("  Creating SWA_ARCH_DECISION_PARENT (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (child:SWA_ArchitecturalDecision {decision_id: e.child_id}) "
                "MATCH (parent:SWA_ArchitecturalDecision {decision_id: e.parent_id}) "
                "MERGE (child)-[:SWA_ARCH_DECISION_PARENT]->(parent)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWA_ARCH_DECISION_PARENT"] = len(edges)

    def _create_arch_decision_belongs_to_module(self, parsed: dict):
        """Link decisions to the MCALModule node."""
        decisions = parsed.get("SWA_ArchitecturalDecision", [])
        if not decisions:
            return

        module = self.module.upper()
        logger.info("  Creating SWA_ARCH_DECISION_BELONGS_TO_MODULE → %s …", module)
        cypher = (
            "MATCH (d:SWA_ArchitecturalDecision {module: $module}) "
            "MATCH (m:MCALModule {module_name: $module}) "
            "MERGE (d)-[:SWA_ARCH_DECISION_BELONGS_TO_MODULE]->(m)"
        )
        self._write_tx(cypher, {"module": module})

        count_res = self._run(
            "MATCH (:SWA_ArchitecturalDecision)-[r:SWA_ARCH_DECISION_BELONGS_TO_MODULE]->(:MCALModule) "
            "RETURN count(r) AS cnt"
        )
        cnt = count_res[0]["cnt"] if count_res else 0
        self.stats["rel:SWA_ARCH_DECISION_BELONGS_TO_MODULE"] = cnt

    def _create_depends_on_hw(self, parsed: dict):
        """MCALModule → SWA_HwPeripheral via SWA_DEPENDS_ON_HW."""
        peripherals = parsed.get("SWA_HwPeripheral", [])
        if not peripherals:
            return

        module = self.module.upper()
        edges = [
            {
                "peripheral_name": p["peripheral_name"],
                "dependency_role": p.get("peripheral_type", "dependent"),
            }
            for p in peripherals
        ]

        logger.info("  Creating SWA_DEPENDS_ON_HW (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (m:MCALModule {module_name: $module}) "
                "MATCH (h:SWA_HwPeripheral {peripheral_name: e.peripheral_name}) "
                "MERGE (m)-[r:SWA_DEPENDS_ON_HW]->(h) "
                "SET r.dependency_role = e.dependency_role"
            )
            self._write_tx(cypher, {"edges": chunk, "module": module})
        self.stats["rel:SWA_DEPENDS_ON_HW"] = len(edges)

    def _create_sw_dependency_rels(self, parsed: dict):
        """MCALModule → SWA_SwDependency, SWA_SwDependency → provider MCALModule."""
        deps = parsed.get("SWA_SwDependency", [])
        if not deps:
            return

        module = self.module.upper()

        # SWA_CALLS_EXTERNAL: module → dependency
        logger.info("  Creating SWA_CALLS_EXTERNAL (%d edges)…", len(deps))
        cypher = (
            "MATCH (d:SWA_SwDependency {module: $module}) "
            "MATCH (m:MCALModule {module_name: $module}) "
            "MERGE (m)-[:SWA_CALLS_EXTERNAL]->(d)"
        )
        self._write_tx(cypher, {"module": module})

        count_res = self._run(
            "MATCH (:MCALModule)-[r:SWA_CALLS_EXTERNAL]->(:SWA_SwDependency) "
            "RETURN count(r) AS cnt"
        )
        self.stats["rel:SWA_CALLS_EXTERNAL"] = count_res[0]["cnt"] if count_res else 0

        # SWA_DEPENDENCY_PROVIDED_BY: dependency → provider module
        deps_with_provider = [
            {"api_name": d["api_name"], "provider": d["provider_module"].upper()}
            for d in deps
            if d.get("provider_module")
        ]
        if deps_with_provider:
            logger.info("  Creating SWA_DEPENDENCY_PROVIDED_BY (%d edges)…", len(deps_with_provider))
            for chunk in self._chunked(deps_with_provider, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (d:SWA_SwDependency {api_name: e.api_name}) "
                    "MATCH (m:MCALModule {module_name: e.provider}) "
                    "MERGE (d)-[:SWA_DEPENDENCY_PROVIDED_BY]->(m)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats["rel:SWA_DEPENDENCY_PROVIDED_BY"] = len(deps_with_provider)

    def _create_config_hierarchy(self, parsed: dict):
        """SWA_CONTAINS_CONTAINER and SWA_CONTAINS_PARAM relationships."""
        containers = parsed.get("SWA_ConfigContainer", [])
        params = parsed.get("SWA_ConfigParam", [])

        # Container → sub-container
        container_edges = []
        for c in containers:
            subs_raw = c.get("sub_containers")
            if not subs_raw:
                continue
            subs = json.loads(subs_raw) if isinstance(subs_raw, str) else subs_raw
            for sub in subs:
                container_edges.append({
                    "parent": c["container_name"],
                    "child": sub.strip(),
                })

        if container_edges:
            logger.info("  Creating SWA_CONTAINS_CONTAINER (%d edges)…", len(container_edges))
            for chunk in self._chunked(container_edges, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (p:SWA_ConfigContainer {container_name: e.parent}) "
                    "MATCH (c:SWA_ConfigContainer {container_name: e.child}) "
                    "MERGE (p)-[:SWA_CONTAINS_CONTAINER]->(c)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats["rel:SWA_CONTAINS_CONTAINER"] = len(container_edges)

        # Container → parameter (via parent_container field)
        param_edges = [
            {"container": p["parent_container"], "param": p["param_name"]}
            for p in params
            if p.get("parent_container")
        ]
        if param_edges:
            logger.info("  Creating SWA_CONTAINS_PARAM (%d edges)…", len(param_edges))
            for chunk in self._chunked(param_edges, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (c:SWA_ConfigContainer {container_name: e.container}) "
                    "MATCH (p:SWA_ConfigParam {param_name: e.param}) "
                    "MERGE (c)-[:SWA_CONTAINS_PARAM]->(p)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats["rel:SWA_CONTAINS_PARAM"] = len(param_edges)

        # Config containers belong to module
        if containers:
            module = self.module.upper()
            logger.info("  Creating SWA_CONFIG_BELONGS_TO_MODULE → %s …", module)
            cypher = (
                "MATCH (c:SWA_ConfigContainer {module: $module}) "
                "MATCH (m:MCALModule {module_name: $module}) "
                "MERGE (c)-[:SWA_CONFIG_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

    def _create_config_traces_to(self, parsed: dict):
        """SWA_ConfigContainer/Param → ProductRequirement via prq_references."""
        for node_type in ("SWA_ConfigContainer", "SWA_ConfigParam"):
            items = parsed.get(node_type, [])
            uid_prop = self._UID_MAP.get(node_type)
            if not items or not uid_prop:
                continue

            edges = []
            for item in items:
                prqs_raw = item.get("prq_references")
                if not prqs_raw:
                    continue
                prqs = json.loads(prqs_raw) if isinstance(prqs_raw, str) else prqs_raw
                for prq_id in prqs:
                    edges.append({
                        "uid": item[uid_prop],
                        "requirement_id": prq_id,
                    })

            if not edges:
                continue

            rel_name = "SWA_CONFIG_TRACES_TO"
            logger.info("  Creating %s from %s (%d edges)…", rel_name, node_type, len(edges))
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (n:{node_type} {{{uid_prop}: e.uid}}) "
                    f"MATCH (p:ProductRequirement {{requirement_id: e.requirement_id}}) "
                    f"MERGE (n)-[:{rel_name}]->(p)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:{rel_name}({node_type})"] = len(edges)

    def _create_exported_interface_rels(self, parsed: dict):
        """Create relationships for SWA_Function, SWA_DataType, SWA_Macro.

        Creates:
        - ARCHITECTURALLY_REALIZES  (Function/DataType/Macro → PRQ)
        - SWA_BELONGS_TO_MODULE     (Function/DataType/Macro → MCALModule)
        - SWA_USES_TYPE             (Function → DataType via parameter types)
        - SWA_REPORTS_ERROR         (Function → SWA_ErrorCode) — skipped if no error nodes
        """
        module = self.module.upper()

        # ── PRQ traceability ──────────────────────────────────────
        for node_type in ("SWA_Function", "SWA_DataType", "SWA_Macro"):
            items = parsed.get(node_type, [])
            uid_prop = self._UID_MAP.get(node_type)
            if not items or not uid_prop:
                continue

            edges = []
            for item in items:
                prqs_raw = item.get("prq_references")
                if not prqs_raw:
                    continue
                prqs = json.loads(prqs_raw) if isinstance(prqs_raw, str) else prqs_raw
                for prq_id in prqs:
                    edges.append({
                        "uid": item[uid_prop],
                        "requirement_id": prq_id,
                    })

            if edges:
                logger.info(
                    "  Creating ARCHITECTURALLY_REALIZES from %s (%d edges)…",
                    node_type, len(edges),
                )
                for chunk in self._chunked(edges, self.BATCH_SIZE):
                    cypher = (
                        f"UNWIND $edges AS e "
                        f"MATCH (n:{node_type} {{{uid_prop}: e.uid}}) "
                        f"MATCH (p:ProductRequirement {{requirement_id: e.requirement_id}}) "
                        f"MERGE (n)-[:ARCHITECTURALLY_REALIZES]->(p)"
                    )
                    self._write_tx(cypher, {"edges": chunk})
                self.stats[f"rel:ARCHITECTURALLY_REALIZES({node_type})"] = len(edges)

        # ── Module membership ─────────────────────────────────────
        for node_type in ("SWA_Function", "SWA_DataType", "SWA_Macro"):
            items = parsed.get(node_type, [])
            if not items:
                continue

            logger.info("  Creating SWA_BELONGS_TO_MODULE for %s → %s …", node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:SWA_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})
            self.stats[f"rel:SWA_BELONGS_TO_MODULE({node_type})"] = len(items)

        # ── SWA_USES_TYPE (Function → DataType) ───────────────────
        functions = parsed.get("SWA_Function", [])
        datatypes = parsed.get("SWA_DataType", [])
        if functions and datatypes:
            type_names = {dt["type_name"] for dt in datatypes}
            edges = []
            for func in functions:
                # Scan syntax, parameters, return type for type references
                text_fields = " ".join(
                    str(func.get(f, ""))
                    for f in ("syntax", "parameters_in", "parameters_out",
                              "parameters_inout", "return_type")
                )
                for tname in type_names:
                    if tname in text_fields:
                        edges.append({
                            "function_name": func["function_name"],
                            "type_name": tname,
                        })

            if edges:
                logger.info("  Creating SWA_USES_TYPE (%d edges)…", len(edges))
                for chunk in self._chunked(edges, self.BATCH_SIZE):
                    cypher = (
                        "UNWIND $edges AS e "
                        "MATCH (f:SWA_Function {function_name: e.function_name}) "
                        "MATCH (t:SWA_DataType {type_name: e.type_name}) "
                        "MERGE (f)-[:SWA_USES_TYPE]->(t)"
                    )
                    self._write_tx(cypher, {"edges": chunk})
                self.stats["rel:SWA_USES_TYPE"] = len(edges)

    # -- New SWA type relationships ------------------------------------------

    # Node types added by the new parsers
    _NEW_SWA_TYPES = {
        "SWA_SafetyMeasure":    "feature_id",
        "SWA_SafetyAoU":        "feature_id",
        "SWA_SecurityMeasure":  "feature_id",
        "SWA_SecurityAoU":      "feature_id",
        "SWA_ErrorCode":        "error_name",
        "SWA_MemorySection":    "section_name",
        "SWA_SourceFile":       "file_name",
        "SWA_PropertyVariable": "variable_name",
        "SWA_CallSequence":     "feature_id",
    }

    def _create_new_type_module_membership(self, parsed: dict):
        """Link new SWA node types to the MCALModule node."""
        module = self.module.upper()
        for node_type in self._NEW_SWA_TYPES:
            if node_type not in parsed:
                continue
            items = parsed[node_type]
            logger.info(
                "  Creating BELONGS_TO_MODULE for %s → %s …",
                node_type, module,
            )
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})
            self.stats[f"rel:BELONGS_TO_MODULE({node_type})"] = len(items)

    def _create_new_type_traces_to_prq(self, parsed: dict):
        """Create SWA_TRACES_TO edges from new types to ProductRequirement."""
        for node_type, uid_key in self._NEW_SWA_TYPES.items():
            nodes = parsed.get(node_type, [])
            if not nodes:
                continue

            edges = []
            for n in nodes:
                prqs_raw = n.get("prq_references")
                if not prqs_raw:
                    continue
                prqs = (
                    json.loads(prqs_raw)
                    if isinstance(prqs_raw, str)
                    else prqs_raw
                )
                for prq_id in prqs:
                    edges.append({"uid": n[uid_key], "requirement_id": prq_id})

            if not edges:
                continue

            logger.info(
                "  Creating SWA_TRACES_TO from %s (%d edges)…",
                node_type, len(edges),
            )
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (n:{node_type} {{{uid_key}: e.uid}}) "
                    f"MATCH (p:ProductRequirement {{requirement_id: e.requirement_id}}) "
                    f"MERGE (n)-[:SWA_TRACES_TO]->(p)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:SWA_TRACES_TO({node_type})"] = len(edges)

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, parsed: dict):
        """Print a summary of what would be created."""
        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – SWA Ingestion, Module: {self.module}")
        print("=" * 60)

        total_nodes = 0
        print(f"\n  Node types:")
        for node_type, items in sorted(parsed.items()):
            if node_type.startswith("_"):
                continue
            uid = self._UID_MAP.get(node_type, "?")
            print(f"    :{node_type:<35s}  {len(items):>5,d} nodes  [merge key: {uid}]")
            total_nodes += len(items)

            # Show a few sample titles/names
            sample_key = uid
            for item in items[:3]:
                val = item.get(sample_key, "?")[:60]
                print(f"      • {val}")
            if len(items) > 3:
                print(f"      … and {len(items) - 3} more")

        print(f"    {'TOTAL':<36s}  {total_nodes:>5,d}")

        # Edge previews
        edge_keys = [k for k in parsed if k.startswith("_edges_")]
        if edge_keys:
            print(f"\n  Relationship hierarchies:")
            for ek in edge_keys:
                rel_name = ek.replace("_edges_", "")
                print(f"    :{rel_name:<35s}  {len(parsed[ek]):>5,d} edges")

        # Traceability summary
        trace_count = 0
        for node_type, items in parsed.items():
            if node_type.startswith("_"):
                continue
            for item in items:
                prqs = item.get("prq_references")
                if prqs:
                    prq_list = json.loads(prqs) if isinstance(prqs, str) else prqs
                    trace_count += len(prq_list)
        if trace_count:
            print(f"\n  Traceability links to PRQ requirements: ~{trace_count}")

        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – SWA Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<35s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<36s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<35s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<36s}  {total_rels:>6,d}")

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# SWUD Knowledge Graph Builder
# ---------------------------------------------------------------------------

class SWUDKnowledgeGraphBuilder:
    """
    Builds SWUD (Software Unit Design) nodes and relationships in the
    MCAL Neo4j database from parsed SWUD markdown files.

    This builder is **document-agnostic** and **section-agnostic**:
    it auto-detects the module from filenames, infers content types
    from structural patterns in the markdown (headings, [req] tags,
    spec table layouts, keywords), and works for any MCAL module's
    SWUD document (ADC, SPI, CAN, GPT, …).

    Node types created (from the MCAL ontology):
        - SWUD_DesignDecision
        - SWUD_DerivedConfigParam
        - SWUD_ConfigStructure
        - SWUD_CodeGenMacro
        - SWUD_TypeDefinition
        - SWUD_Function
        - SWUD_DataVariable
        - SWUD_CriticalSection
        - SWUD_MemorySection

    Relationships created:
        - SWUD_TRACES_TO           (any SWUD node → ProductRequirement)
        - SWUD_BELONGS_TO_MODULE   (any SWUD node → MCALModule)
        - SWUD_ALLOCATED_IN        (Function/DataVariable → MemorySection)
        - SWUD_USES_TYPE           (Function → TypeDefinition)
        - SWUD_CALLS               (Function → Function)
        - SWUD_USES_CRITICAL_SECTION (Function → CriticalSection)
        - SWUD_CONFIG_GATES        (DerivedConfigParam → Function)
        - SWUD_PROTECTS            (CriticalSection → DataVariable)
        - SWUD_REALIZES_SWA        (Function → SWA_ArchitecturalDecision)

    Usage::

        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swud
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swud --dry-run
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-swud --swud-dir path/to/swud/
    """

    BATCH_SIZE = 500

    # Unique-key property for each SWUD node type
    _UID_MAP = {
        "SWUD_DesignDecision":    "decision_id",
        "SWUD_DerivedConfigParam": "param_name",
        "SWUD_ConfigStructure":   "structure_name",
        "SWUD_CodeGenMacro":      "macro_name",
        "SWUD_TypeDefinition":    "type_name",
        "SWUD_Function":          "function_name",
        "SWUD_DataVariable":      "variable_name",
        "SWUD_CriticalSection":   "critical_section_name",
        "SWUD_MemorySection":     "section_name",
    }

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        swud_dir: Path,
        dry_run: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.swud_dir = swud_dir
        self.dry_run = dry_run
        self.stats: dict = Counter()
        self._driver = None

    # -- Connection ---------------------------------------------------------

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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("SWUD write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a read query with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("SWUD read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # -- Public entry point -------------------------------------------------

    def build(self):
        """Run the full SWUD ingestion pipeline."""
        if parse_swud_directory is None:
            logger.error(
                "swud_parsers module not found. Ensure "
                "src/HybridRAG/code/KG/swud_parsers.py exists."
            )
            sys.exit(1)

        t0 = time.time()

        logger.info("=" * 60)
        logger.info("SWUD Knowledge Graph Builder – module: %s", self.module)
        logger.info("SWUD directory: %s", self.swud_dir)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.swud_dir.exists():
            logger.error("SWUD directory not found: %s", self.swud_dir)
            print(
                f"\n  ERROR: SWUD directory not found:\n"
                f"  {self.swud_dir}\n\n"
                f"  Convert SWUD PDF sections first using:\n"
                f"    python prepare_markdowns.py --input <SWUD_PDF> ...\n"
            )
            sys.exit(1)

        # Step 1: Parse SWUD markdown files
        logger.info("Step 1/4: Parsing SWUD markdown files…")
        parsed = parse_swud_directory(
            swud_dir=self.swud_dir,
            module=self.module,
        )

        if not parsed:
            logger.warning("No SWUD nodes parsed from %s", self.swud_dir)
            return

        if self.dry_run:
            self._preview(parsed)
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            # Step 3: Create constraints / indexes
            logger.info("Step 2/4: Creating SWUD constraints and indexes…")
            self._create_constraints(parsed)

            # Step 4: Create nodes
            logger.info("Step 3/4: Creating SWUD nodes…")
            self._create_nodes(parsed)

            # Step 5: Create relationships
            logger.info("Step 4/4: Creating SWUD relationships…")
            self._create_relationships(parsed)

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # -- Constraints --------------------------------------------------------

    def _create_constraints(self, parsed: dict):
        """Create uniqueness constraints for SWUD node types."""
        for node_type, uid_prop in self._UID_MAP.items():
            if node_type not in parsed:
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

    # -- Node Creation (batched by type) ------------------------------------

    def _create_nodes(self, parsed: dict):
        """Create nodes for each SWUD node type using UNWIND + MERGE."""
        for node_type, items in parsed.items():
            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                logger.warning("Unknown SWUD node type: %s – skipping", node_type)
                continue

            logger.info("  Creating :%s (%d nodes)…", node_type, len(items))

            # Prepare items for Neo4j: convert lists to JSON strings,
            # remove None values
            batch = []
            for item in items:
                clean = {}
                for k, v in item.items():
                    if v is None:
                        continue
                    if isinstance(v, (list, dict, set, frozenset)):
                        clean[k] = json.dumps(v, default=str)
                    else:
                        clean[k] = v
                batch.append(clean)

            for chunk in self._chunked(batch, self.BATCH_SIZE):
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
        """Create all SWUD relationships."""
        self._create_traces_to(parsed)
        self._create_belongs_to_module(parsed)
        self._create_allocated_in(parsed)
        self._create_uses_type(parsed)
        self._create_calls(parsed)
        self._create_uses_critical_section(parsed)
        self._create_config_gates(parsed)
        self._create_protects(parsed)
        self._create_realizes_swa(parsed)
        self._create_decision_applies_to(parsed)
        self._create_generates(parsed)

    def _create_traces_to(self, parsed: dict):
        """
        SWUD_TRACES_TO: any SWUD node → ProductRequirement
        via prq_references field on each node.
        """
        for node_type, items in parsed.items():
            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                continue

            edges = []
            for item in items:
                prqs_raw = item.get("prq_references")
                if not prqs_raw:
                    continue
                prqs = json.loads(prqs_raw) if isinstance(prqs_raw, str) else prqs_raw
                for prq_id in prqs:
                    edges.append({
                        "uid": item[uid_prop],
                        "requirement_id": prq_id,
                    })

            if not edges:
                continue

            logger.info("  Creating SWUD_TRACES_TO from %s (%d edges)…", node_type, len(edges))
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (n:{node_type} {{{uid_prop}: e.uid}}) "
                    f"MATCH (p:ProductRequirement {{requirement_id: e.requirement_id}}) "
                    f"MERGE (n)-[:SWUD_TRACES_TO]->(p)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:SWUD_TRACES_TO({node_type})"] = len(edges)

    def _create_belongs_to_module(self, parsed: dict):
        """SWUD_BELONGS_TO_MODULE: any SWUD node → MCALModule."""
        module = self.module.upper()
        for node_type in parsed:
            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                continue

            logger.info("  Creating SWUD_BELONGS_TO_MODULE for %s → %s …", node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:SWUD_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

            count_res = self._run(
                f"MATCH (:{node_type})-[r:SWUD_BELONGS_TO_MODULE]->(:MCALModule) "
                f"RETURN count(r) AS cnt"
            )
            cnt = count_res[0]["cnt"] if count_res else 0
            self.stats[f"rel:SWUD_BELONGS_TO_MODULE({node_type})"] = cnt

    def _create_allocated_in(self, parsed: dict):
        """
        SWUD_ALLOCATED_IN: Function/DataVariable → MemorySection
        via memory_section field on each node.
        """
        mem_sections = parsed.get("SWUD_MemorySection", [])
        if not mem_sections:
            return

        for node_type in ("SWUD_Function", "SWUD_DataVariable"):
            items = parsed.get(node_type, [])
            uid_prop = self._UID_MAP.get(node_type)
            if not items or not uid_prop:
                continue

            edges = []
            for item in items:
                ms = item.get("memory_section")
                if not ms:
                    continue
                edges.append({
                    "uid": item[uid_prop],
                    "section_name": ms,
                })

            if not edges:
                continue

            logger.info("  Creating SWUD_ALLOCATED_IN from %s (%d edges)…", node_type, len(edges))
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (n:{node_type} {{{uid_prop}: e.uid}}) "
                    f"MATCH (ms:SWUD_MemorySection {{section_name: e.section_name}}) "
                    f"MERGE (n)-[:SWUD_ALLOCATED_IN]->(ms)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:SWUD_ALLOCATED_IN({node_type})"] = len(edges)

    def _create_uses_type(self, parsed: dict):
        """
        SWUD_USES_TYPE: Function → TypeDefinition
        Inferred from parameter types, return types, and algorithm references.
        """
        functions = parsed.get("SWUD_Function", [])
        type_defs = parsed.get("SWUD_TypeDefinition", [])
        if not functions or not type_defs:
            return

        type_names = {td["type_name"] for td in type_defs}
        edges = []

        for func in functions:
            # Scan all text fields for type references
            text_fields = " ".join(str(func.get(f, "")) for f in [
                "syntax", "parameters_in", "parameters_out",
                "return_type", "algorithm", "description",
            ])
            for tname in type_names:
                if tname in text_fields:
                    edges.append({
                        "function_name": func["function_name"],
                        "type_name": tname,
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_USES_TYPE (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SWUD_Function {function_name: e.function_name}) "
                "MATCH (t:SWUD_TypeDefinition {type_name: e.type_name}) "
                "MERGE (f)-[:SWUD_USES_TYPE]->(t)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_USES_TYPE"] = len(edges)

    def _create_calls(self, parsed: dict):
        """
        SWUD_CALLS: Function → Function
        Inferred from algorithm text referencing other function names.
        """
        functions = parsed.get("SWUD_Function", [])
        if not functions or len(functions) < 2:
            return

        func_names = {f["function_name"] for f in functions}
        edges = []

        for func in functions:
            algo = str(func.get("algorithm", ""))
            desc = str(func.get("description", ""))
            text = algo + " " + desc

            for other_name in func_names:
                if other_name == func["function_name"]:
                    continue
                # Match as whole word to avoid false positives
                if re.search(rf"\b{re.escape(other_name)}\b", text):
                    edges.append({
                        "caller": func["function_name"],
                        "callee": other_name,
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_CALLS (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (a:SWUD_Function {function_name: e.caller}) "
                "MATCH (b:SWUD_Function {function_name: e.callee}) "
                "MERGE (a)-[:SWUD_CALLS]->(b)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_CALLS"] = len(edges)

    def _create_uses_critical_section(self, parsed: dict):
        """
        SWUD_USES_CRITICAL_SECTION: Function → CriticalSection
        Inferred from using_functions list on each CriticalSection.
        """
        functions = parsed.get("SWUD_Function", [])
        crit_sections = parsed.get("SWUD_CriticalSection", [])
        if not functions or not crit_sections:
            return

        func_name_set = {f["function_name"] for f in functions}
        edges = []

        for cs in crit_sections:
            api_raw = cs.get("using_functions")
            if not api_raw:
                continue
            apis = json.loads(api_raw) if isinstance(api_raw, str) else api_raw
            for api in apis:
                api = api.strip()
                if api in func_name_set:
                    edges.append({
                        "function_name": api,
                        "cs_name": cs["critical_section_name"],
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_USES_CRITICAL_SECTION (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SWUD_Function {function_name: e.function_name}) "
                "MATCH (cs:SWUD_CriticalSection {critical_section_name: e.cs_name}) "
                "MERGE (f)-[:SWUD_USES_CRITICAL_SECTION]->(cs)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_USES_CRITICAL_SECTION"] = len(edges)

    def _create_config_gates(self, parsed: dict):
        """
        SWUD_CONFIG_GATES: DerivedConfigParam → Function
        Inferred from configuration_dependencies on functions and from
        algorithm text of config params referencing function names.
        """
        params = parsed.get("SWUD_DerivedConfigParam", [])
        functions = parsed.get("SWUD_Function", [])
        if not params or not functions:
            return

        func_name_set = {f["function_name"] for f in functions}
        param_name_set = {p["param_name"] for p in params}
        edges = []

        # Direction 1: function.configuration_dependencies mentions a param
        for func in functions:
            deps = str(func.get("configuration_dependencies", ""))
            for pname in param_name_set:
                if pname in deps:
                    edges.append({
                        "param_name": pname,
                        "function_name": func["function_name"],
                    })

        # Direction 2: param.algorithm mentions a function
        for param in params:
            algo = str(param.get("algorithm", ""))
            for fname in func_name_set:
                if re.search(rf"\b{re.escape(fname)}\b", algo):
                    edges.append({
                        "param_name": param["param_name"],
                        "function_name": fname,
                    })

        # Deduplicate
        unique_edges = list({(e["param_name"], e["function_name"]): e for e in edges}.values())

        if not unique_edges:
            return

        logger.info("  Creating SWUD_CONFIG_GATES (%d edges)…", len(unique_edges))
        for chunk in self._chunked(unique_edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (p:SWUD_DerivedConfigParam {param_name: e.param_name}) "
                "MATCH (f:SWUD_Function {function_name: e.function_name}) "
                "MERGE (p)-[:SWUD_CONFIG_GATES]->(f)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_CONFIG_GATES"] = len(unique_edges)

    def _create_protects(self, parsed: dict):
        """
        SWUD_PROTECTS: CriticalSection → DataVariable
        Inferred from resource_name on CriticalSection matching variable names.
        """
        crit_sections = parsed.get("SWUD_CriticalSection", [])
        data_vars = parsed.get("SWUD_DataVariable", [])
        if not crit_sections or not data_vars:
            return

        var_names = {v["variable_name"] for v in data_vars}
        edges = []

        for cs in crit_sections:
            resource = cs.get("resource_name", "")
            if not resource:
                continue
            for vname in var_names:
                # Check if resource name pattern matches variable name
                if vname in resource or resource in vname:
                    edges.append({
                        "cs_name": cs["critical_section_name"],
                        "variable_name": vname,
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_PROTECTS (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (cs:SWUD_CriticalSection {critical_section_name: e.cs_name}) "
                "MATCH (v:SWUD_DataVariable {variable_name: e.variable_name}) "
                "MERGE (cs)-[:SWUD_PROTECTS]->(v)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_PROTECTS"] = len(edges)

    def _create_realizes_swa(self, parsed: dict):
        """
        SWUD_REALIZES_SWA: Function → SWA_ArchitecturalDecision
        Inferred from design_decisions field on functions that match
        existing SWA decision IDs.
        """
        functions = parsed.get("SWUD_Function", [])
        if not functions:
            return

        edges = []
        for func in functions:
            dd = func.get("design_decisions", "")
            if not dd:
                continue
            # Extract GUID-like references from design_decisions field
            import re as _re
            guids = _re.findall(
                r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
                str(dd),
            )
            for guid in guids:
                edges.append({
                    "function_name": func["function_name"],
                    "decision_id": guid,
                })

        if not edges:
            return

        logger.info("  Creating SWUD_REALIZES_SWA (%d edges)…", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SWUD_Function {function_name: e.function_name}) "
                "MATCH (d:SWA_ArchitecturalDecision {decision_id: e.decision_id}) "
                "MERGE (f)-[:SWUD_REALIZES_SWA]->(d)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_REALIZES_SWA"] = len(edges)

    def _create_decision_applies_to(self, parsed: dict):
        """
        SWUD_DECISION_APPLIES_TO: DesignDecision → Function/ConfigStructure/CodeGenMacro
        Inferred from references to node names inside design decision
        description and rationale text.
        """
        decisions = parsed.get("SWUD_DesignDecision", [])
        if not decisions:
            return

        # Build lookup sets for target node types
        target_map = {}  # name → (node_type, uid_prop)
        for node_type, uid_prop in [
            ("SWUD_Function", "function_name"),
            ("SWUD_ConfigStructure", "structure_name"),
            ("SWUD_CodeGenMacro", "macro_name"),
        ]:
            for item in parsed.get(node_type, []):
                name = item[uid_prop]
                if len(name) >= 4:  # skip very short names to avoid false matches
                    target_map[name] = (node_type, uid_prop)

        edges = []  # list of (decision_id, target_label, uid_prop, target_name)
        for dd in decisions:
            text = " ".join(str(dd.get(f, "")) for f in ("title", "description", "rationale"))
            if not text.strip():
                continue
            for target_name, (target_label, uid_prop) in target_map.items():
                if re.search(rf"\b{re.escape(target_name)}\b", text):
                    edges.append({
                        "decision_id": dd["decision_id"],
                        "target_label": target_label,
                        "uid_prop": uid_prop,
                        "target_name": target_name,
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_DECISION_APPLIES_TO (%d edges)…", len(edges))
        # Group by target label to generate appropriate Cypher
        from collections import defaultdict
        by_label = defaultdict(list)
        for e in edges:
            by_label[e["target_label"]].append(e)

        for target_label, group in by_label.items():
            uid_prop = group[0]["uid_prop"]
            batch = [{"decision_id": e["decision_id"], "target_name": e["target_name"]} for e in group]
            for chunk in self._chunked(batch, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (d:SWUD_DesignDecision {decision_id: e.decision_id}) "
                    f"MATCH (t:{target_label} {{{uid_prop}: e.target_name}}) "
                    "MERGE (d)-[:SWUD_DECISION_APPLIES_TO]->(t)"
                )
                self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_DECISION_APPLIES_TO"] = len(edges)

    def _create_generates(self, parsed: dict):
        """
        SWUD_GENERATES: CodeGenMacro → ConfigStructure/DerivedConfigParam
        Inferred from algorithm text and naming patterns.
        """
        macros = parsed.get("SWUD_CodeGenMacro", [])
        if not macros:
            return

        # Build lookup sets for target node types
        target_map = {}
        for node_type, uid_prop in [
            ("SWUD_ConfigStructure", "structure_name"),
            ("SWUD_DerivedConfigParam", "param_name"),
        ]:
            for item in parsed.get(node_type, []):
                name = item[uid_prop]
                if len(name) >= 4:
                    target_map[name] = (node_type, uid_prop)

        edges = []
        for macro in macros:
            text = " ".join(str(macro.get(f, "")) for f in ("description", "algorithm"))
            if not text.strip():
                continue
            for target_name, (target_label, uid_prop) in target_map.items():
                if re.search(rf"\b{re.escape(target_name)}\b", text):
                    edges.append({
                        "macro_name": macro["macro_name"],
                        "target_label": target_label,
                        "uid_prop": uid_prop,
                        "target_name": target_name,
                    })

        if not edges:
            return

        logger.info("  Creating SWUD_GENERATES (%d edges)…", len(edges))
        from collections import defaultdict
        by_label = defaultdict(list)
        for e in edges:
            by_label[e["target_label"]].append(e)

        for target_label, group in by_label.items():
            uid_prop = group[0]["uid_prop"]
            batch = [{"macro_name": e["macro_name"], "target_name": e["target_name"]} for e in group]
            for chunk in self._chunked(batch, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $edges AS e "
                    "MATCH (m:SWUD_CodeGenMacro {macro_name: e.macro_name}) "
                    f"MATCH (t:{target_label} {{{uid_prop}: e.target_name}}) "
                    "MERGE (m)-[:SWUD_GENERATES]->(t)"
                )
                self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SWUD_GENERATES"] = len(edges)

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, parsed: dict):
        """Print a summary of what would be created."""
        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – SWUD Ingestion, Module: {self.module}")
        print("=" * 60)

        total_nodes = 0
        print(f"\n  Node types:")
        for node_type, items in sorted(parsed.items()):
            uid = self._UID_MAP.get(node_type, "?")
            print(f"    :{node_type:<35s}  {len(items):>5,d} nodes  [merge key: {uid}]")
            total_nodes += len(items)

            # Show a few sample names
            sample_key = uid
            for item in items[:3]:
                val = str(item.get(sample_key, "?"))[:60]
                print(f"      • {val}")
            if len(items) > 3:
                print(f"      … and {len(items) - 3} more")

        print(f"    {'TOTAL':<36s}  {total_nodes:>5,d}")

        # Traceability summary
        trace_count = 0
        for node_type, items in parsed.items():
            for item in items:
                prqs = item.get("prq_references")
                if prqs:
                    prq_list = json.loads(prqs) if isinstance(prqs, str) else prqs
                    trace_count += len(prq_list)
        if trace_count:
            print(f"\n  Traceability links to PRQ requirements: ~{trace_count}")

        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – SWUD Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<35s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<36s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<35s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<36s}  {total_rels:>6,d}")

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Test Specification Knowledge Graph Builder
# ---------------------------------------------------------------------------

class TestSpecKnowledgeGraphBuilder:
    """
    Builds Test Specification (TS) nodes and relationships in the MCAL
    Neo4j database from a parsed test specification Excel workbook.

    This builder parses the test spec Excel file and creates nodes for each
    sheet type, then links them via traceability relationships to existing
    PRQ, SWA, and SWUD nodes in the graph.

    Node types created (from the MCAL ontology):
        - TS_FunctionalTestCase     (sheet "Test cases")
        - TS_ConfigTestCase         (sheet "Configuration tests")
        - TS_StaticInterfaceTestCase (sheet "Static source code IF tests")
        - TS_WCETTestCase           (sheet "WCET analysis")
        - TS_TestSpecDocument       (synthetic document-level node)

    Relationships created:
        - TS_VERIFIES               (test case → ProductRequirement via PRQ refs)
        - TS_VALIDATES_SWA          (test case → SWA node via SWA GUIDs)
        - TS_VALIDATES_SWUD         (test case → SWUD node via SWUD GUIDs)
        - TS_TESTS_CONFIG_ELEMENT   (config test → SWA_ConfigParam/Container)
        - TS_MEASURES_TIMING_OF     (WCET test → SWA_Function via api_name)
        - TS_BELONGS_TO_MODULE      (test case → MCALModule)
        - TS_CONTAINS_TESTCASE      (TS_TestSpecDocument → test cases)

    Usage::

        python build_knowledge_graph.py --profile mcal --module ADC --ingest-testspec
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-testspec --dry-run
        python build_knowledge_graph.py --profile mcal --module ADC --ingest-testspec --testspec-dir path/to/dir/
    """

    BATCH_SIZE = 500

    # Unique-key property for each TS node type
    _UID_MAP = {
        "TS_FunctionalTestCase":      "test_case_id",
        "TS_ConfigTestCase":          "test_case_id",
        "TS_StaticInterfaceTestCase": "test_case_id",
        "TS_WCETTestCase":           "test_case_id",
        "TS_TestSpecDocument":       "document_name",
    }

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        testspec_dir: Path,
        dry_run: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.testspec_dir = testspec_dir
        self.dry_run = dry_run
        self.stats: dict = Counter()
        self._driver = None

    # -- Connection ---------------------------------------------------------

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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

    def _close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def _write_tx(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("TestSpec write failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient write error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)

    def _run(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a read query with retry on transient errors."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    logger.error("TestSpec read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # -- Public entry point -------------------------------------------------

    def build(self):
        """Run the full TestSpec ingestion pipeline."""
        if parse_testspec_workbook is None:
            logger.error(
                "testspec_parsers module not found. Ensure "
                "src/HybridRAG/code/KG/testspec_parsers.py exists."
            )
            sys.exit(1)

        t0 = time.time()

        logger.info("=" * 60)
        logger.info("TestSpec Knowledge Graph Builder – module: %s", self.module)
        logger.info("TestSpec directory: %s", self.testspec_dir)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.testspec_dir.exists():
            logger.warning("TestSpec directory not found: %s — skipping.", self.testspec_dir)
            print(
                f"\n  SKIP: TestSpec directory not found:\n"
                f"  {self.testspec_dir}\n"
                f"  No test spec ingestion for module {self.module}.\n"
            )
            return

        # Find the Excel file for this module
        xlsx_path = self._find_workbook()
        if not xlsx_path:
            logger.warning("No test spec Excel file found for module %s in %s — skipping.",
                           self.module, self.testspec_dir)
            print(
                f"\n  SKIP: No test spec Excel file found for module {self.module}\n"
                f"  Expected pattern: TC4xx_SW_MCAL_TS_{self.module.capitalize()}.xlsx\n"
                f"  in directory: {self.testspec_dir}\n"
            )
            return

        logger.info("Using workbook: %s", xlsx_path.name)

        # Step 1: Parse Excel workbook
        logger.info("Step 1/4: Parsing test spec Excel workbook…")
        parsed = parse_testspec_workbook(str(xlsx_path), self.module)

        if not parsed:
            logger.warning("No test spec nodes parsed from %s", xlsx_path)
            return

        if self.dry_run:
            self._preview(parsed)
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            # Step 3: Create constraints / indexes
            logger.info("Step 2/4: Creating TestSpec constraints and indexes…")
            self._create_constraints(parsed)

            # Step 4: Create nodes
            logger.info("Step 3/4: Creating TestSpec nodes…")
            self._create_nodes(parsed)

            # Step 5: Create relationships
            logger.info("Step 4/4: Creating TestSpec relationships…")
            self._create_relationships(parsed)

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    def _find_workbook(self) -> Optional[Path]:
        """
        Find the test spec Excel workbook for the current module.

        Searches for patterns like:
            TC4xx_SW_MCAL_TS_Adc.xlsx
            TC4xx_SW_MCAL_TS_ADC.xlsx
            *_TS_<Module>.xlsx
        """
        # Try exact match first
        mod_cap = self.module.capitalize()  # e.g. "Adc"
        mod_upper = self.module.upper()     # e.g. "ADC"

        candidates = [
            self.testspec_dir / f"TC4xx_SW_MCAL_TS_{mod_cap}.xlsx",
            self.testspec_dir / f"TC4xx_SW_MCAL_TS_{mod_upper}.xlsx",
        ]

        for c in candidates:
            if c.exists():
                return c

        # Fallback: glob for any xlsx with the module name
        for f in self.testspec_dir.glob("*.xlsx"):
            if f.name.startswith("~$"):
                continue  # skip temporary files
            if mod_upper in f.stem.upper() and "TS" in f.stem.upper():
                return f

        return None

    # -- Constraints --------------------------------------------------------

    def _create_constraints(self, parsed: dict):
        """Create uniqueness constraints for TS node types."""
        for node_type, uid_prop in self._UID_MAP.items():
            if node_type not in parsed:
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

    # -- Node Creation (batched by type) ------------------------------------

    def _create_nodes(self, parsed: dict):
        """Create nodes for each TS node type using UNWIND + MERGE."""
        for node_type, items in parsed.items():
            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                logger.warning("Unknown TS node type: %s – skipping", node_type)
                continue

            logger.info("  Creating :%s (%d nodes)…", node_type, len(items))

            # Prepare items for Neo4j: convert lists to JSON strings,
            # remove None values
            batch = []
            for item in items:
                clean = {}
                for k, v in item.items():
                    if v is None:
                        continue
                    if isinstance(v, (list, dict, set, frozenset)):
                        clean[k] = json.dumps(v, default=str)
                    else:
                        clean[k] = v
                batch.append(clean)

            for chunk in self._chunked(batch, self.BATCH_SIZE):
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
        """Create all TestSpec relationships."""
        self._create_ts_verifies(parsed)
        self._create_ts_validates_swa(parsed)
        self._create_ts_validates_swud(parsed)
        self._create_ts_tests_config_element(parsed)
        self._create_ts_measures_timing(parsed)
        self._create_ts_belongs_to_module(parsed)
        self._create_ts_contains_testcase(parsed)
        self._create_vmodel_bridge_edges()

    def _create_vmodel_bridge_edges(self):
        """
        Create V-Model bridge relationships that the MCP traceability tools expect.

        The tools query: ProductRequirement -[:IMPLEMENTS]-> Code -[:TRACES_TO]-> Test
        but the existing graph has the reverse edges:
          - SWA -[:ARCHITECTURALLY_REALIZES/:SWA_ARCH_DECISION_TRACES_TO/:SWA_CONFIG_TRACES_TO]-> PRQ
          - SWUD -[:SWUD_TRACES_TO]-> PRQ
          - TS   -[:TS_VALIDATES_SWA/:TS_VALIDATES_SWUD]-> SWA/SWUD

        This method creates the forward-direction IMPLEMENTS/TRACES_TO edges.
        """
        module = self.module.upper()
        logger.info("  Creating V-Model bridge edges for module %s…", module)

        # ── IMPLEMENTS: ProductRequirement → SWA ─────────────────────
        # Reverse of ARCHITECTURALLY_REALIZES (SWA_Function/DataType → PRQ)
        self._write_tx(
            "MATCH (swa)-[:ARCHITECTURALLY_REALIZES]->(prq:ProductRequirement) "
            "WHERE swa.module = $module "
            "MERGE (prq)-[:IMPLEMENTS {source: 'bridge', derived_from: 'ARCHITECTURALLY_REALIZES'}]->(swa)",
            {"module": module},
        )
        # Reverse of SWA_ARCH_DECISION_TRACES_TO (SWA_ArchDecision → PRQ)
        self._write_tx(
            "MATCH (swa)-[:SWA_ARCH_DECISION_TRACES_TO]->(prq:ProductRequirement) "
            "MERGE (prq)-[:IMPLEMENTS {source: 'bridge', derived_from: 'SWA_ARCH_DECISION_TRACES_TO'}]->(swa)",
            {},
        )
        # Reverse of SWA_CONFIG_TRACES_TO (SWA_ConfigContainer/Param → PRQ)
        self._write_tx(
            "MATCH (swa)-[:SWA_CONFIG_TRACES_TO]->(prq:ProductRequirement) "
            "MERGE (prq)-[:IMPLEMENTS {source: 'bridge', derived_from: 'SWA_CONFIG_TRACES_TO'}]->(swa)",
            {},
        )

        swa_count = self._run(
            "MATCH (prq:ProductRequirement)-[r:IMPLEMENTS]->(swa) "
            "WHERE any(l IN labels(swa) WHERE l STARTS WITH 'SWA_') "
            "RETURN count(r) AS c",
        )
        n_swa = swa_count[0]["c"] if swa_count else 0
        logger.info("    IMPLEMENTS (PRQ → SWA): %d edges", n_swa)
        self.stats["rel:IMPLEMENTS(PRQ→SWA)"] = n_swa

        # ── IMPLEMENTS: ProductRequirement → SWUD ────────────────────
        # Reverse of SWUD_TRACES_TO
        self._write_tx(
            "MATCH (swud)-[:SWUD_TRACES_TO]->(prq:ProductRequirement) "
            "WHERE swud.module = $module "
            "MERGE (prq)-[:IMPLEMENTS {source: 'bridge', derived_from: 'SWUD_TRACES_TO'}]->(swud)",
            {"module": module},
        )

        swud_count = self._run(
            "MATCH (prq:ProductRequirement)-[r:IMPLEMENTS {derived_from: 'SWUD_TRACES_TO'}]->(swud) "
            "WHERE swud.module = $module RETURN count(r) AS c",
            {"module": module},
        )
        n_swud = swud_count[0]["c"] if swud_count else 0
        logger.info("    IMPLEMENTS (PRQ → SWUD): %d edges", n_swud)
        self.stats["rel:IMPLEMENTS(PRQ→SWUD)"] = n_swud

        # ── TRACES_TO: SWA/SWUD → TS test cases ─────────────────────
        # Reverse of TS_VALIDATES_SWA
        self._write_tx(
            "MATCH (ts)-[:TS_VALIDATES_SWA]->(swa) "
            "WHERE ts.module = $module "
            "MERGE (swa)-[:TRACES_TO {source: 'bridge', derived_from: 'TS_VALIDATES_SWA'}]->(ts)",
            {"module": module},
        )

        ts_swa_count = self._run(
            "MATCH (swa)-[r:TRACES_TO {derived_from: 'TS_VALIDATES_SWA'}]->(ts) "
            "WHERE ts.module = $module RETURN count(r) AS c",
            {"module": module},
        )
        n_ts_swa = ts_swa_count[0]["c"] if ts_swa_count else 0
        logger.info("    TRACES_TO (SWA → TS): %d edges", n_ts_swa)
        self.stats["rel:TRACES_TO(SWA→TS)"] = n_ts_swa

        # Reverse of TS_VALIDATES_SWUD
        self._write_tx(
            "MATCH (ts)-[:TS_VALIDATES_SWUD]->(swud) "
            "WHERE ts.module = $module "
            "MERGE (swud)-[:TRACES_TO {source: 'bridge', derived_from: 'TS_VALIDATES_SWUD'}]->(ts)",
            {"module": module},
        )

        ts_swud_count = self._run(
            "MATCH (swud)-[r:TRACES_TO {derived_from: 'TS_VALIDATES_SWUD'}]->(ts) "
            "WHERE ts.module = $module RETURN count(r) AS c",
            {"module": module},
        )
        n_ts_swud = ts_swud_count[0]["c"] if ts_swud_count else 0
        logger.info("    TRACES_TO (SWUD → TS): %d edges", n_ts_swud)
        self.stats["rel:TRACES_TO(SWUD→TS)"] = n_ts_swud

        total = n_swa + n_swud + n_ts_swa + n_ts_swud
        logger.info("  V-Model bridge edges total: %d", total)

    def _create_ts_verifies(self, parsed: dict):
        """
        TS_VERIFIES: TS test case → ProductRequirement via prq_references.
        The primary V-Model traceability link.
        """
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]
        for node_type in test_case_types:
            items = parsed.get(node_type, [])
            edges = []
            for item in items:
                prqs_raw = item.get("prq_references")
                if not prqs_raw:
                    continue
                prqs = json.loads(prqs_raw) if isinstance(prqs_raw, str) else prqs_raw
                for prq_id in prqs:
                    edges.append({
                        "test_case_id": item["test_case_id"],
                        "requirement_id": prq_id,
                    })

            if not edges:
                continue

            logger.info("  Creating TS_VERIFIES from %s (%d edges)…", node_type, len(edges))
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (tc:{node_type} {{test_case_id: e.test_case_id}}) "
                    f"MATCH (prq:ProductRequirement {{requirement_id: e.requirement_id}}) "
                    f"MERGE (tc)-[:TS_VERIFIES]->(prq)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:TS_VERIFIES({node_type})"] = len(edges)

    def _create_ts_validates_swa(self, parsed: dict):
        """
        TS_VALIDATES_SWA: TS test case → SWA node via SWA feature GUIDs.

        Matches GUIDs against the appropriate ID property of each SWA node
        type (decision_id for SWA_ArchitecturalDecision, feature_id for
        others).  Iterates over all SWA node types – mirrors the approach
        used by ``_create_ts_validates_swud``.
        """
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]
        # SWA node types to match against, with their GUID property.
        # SWA_ArchitecturalDecision stores the GUID in `decision_id`;
        # all other SWA types expose `feature_id`.
        swa_type_id_props = [
            ("SWA_ArchitecturalDecision", "decision_id"),
            ("SWA_Function",              "feature_id"),
            ("SWA_DataType",              "feature_id"),
            ("SWA_Macro",                 "feature_id"),
            ("SWA_ConfigContainer",       "feature_id"),
            ("SWA_ConfigParam",           "feature_id"),
            ("SWA_SourceFile",            "feature_id"),
            ("SWA_HwPeripheral",          "feature_id"),
        ]

        for node_type in test_case_types:
            items = parsed.get(node_type, [])
            edges = []
            for item in items:
                swa_raw = item.get("swa_references")
                if not swa_raw:
                    continue
                swa_guids = json.loads(swa_raw) if isinstance(swa_raw, str) else swa_raw
                for guid in swa_guids:
                    edges.append({
                        "test_case_id": item["test_case_id"],
                        "guid": guid,
                    })

            if not edges:
                continue

            logger.info("  Creating TS_VALIDATES_SWA from %s (%d edges)…",
                        node_type, len(edges))

            # Try to match each GUID against every SWA node type
            for swa_type, id_prop in swa_type_id_props:
                for chunk in self._chunked(edges, self.BATCH_SIZE):
                    cypher = (
                        f"UNWIND $edges AS e "
                        f"MATCH (tc:{node_type} {{test_case_id: e.test_case_id}}) "
                        f"MATCH (swa:{swa_type} {{{id_prop}: e.guid}}) "
                        f"MERGE (tc)-[:TS_VALIDATES_SWA]->(swa)"
                    )
                    self._write_tx(cypher, {"edges": chunk})

            self.stats[f"rel:TS_VALIDATES_SWA({node_type})"] = len(edges)

    def _create_ts_validates_swud(self, parsed: dict):
        """
        TS_VALIDATES_SWUD: TS test case → SWUD node via SWUD feature GUIDs.

        Matches GUIDs against the feature_id property of SWUD nodes
        (SWUD_DesignDecision, SWUD_Function, SWUD_TypeDefinition, etc.).
        """
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]
        # SWUD node types to match against
        swud_types = [
            "SWUD_DesignDecision",
            "SWUD_Function",
            "SWUD_TypeDefinition",
            "SWUD_DerivedConfigParam",
            "SWUD_ConfigStructure",
            "SWUD_DataVariable",
        ]

        for node_type in test_case_types:
            items = parsed.get(node_type, [])
            edges = []
            for item in items:
                swud_raw = item.get("swud_references")
                if not swud_raw:
                    continue
                swud_guids = json.loads(swud_raw) if isinstance(swud_raw, str) else swud_raw
                for guid in swud_guids:
                    edges.append({
                        "test_case_id": item["test_case_id"],
                        "guid": guid,
                    })

            if not edges:
                continue

            logger.info("  Creating TS_VALIDATES_SWUD from %s (%d edges)…",
                        node_type, len(edges))

            # Try to match each GUID against any SWUD node type
            for swud_type in swud_types:
                for chunk in self._chunked(edges, self.BATCH_SIZE):
                    cypher = (
                        f"UNWIND $edges AS e "
                        f"MATCH (tc:{node_type} {{test_case_id: e.test_case_id}}) "
                        f"MATCH (swud:{swud_type} {{feature_id: e.guid}}) "
                        f"MERGE (tc)-[:TS_VALIDATES_SWUD]->(swud)"
                    )
                    self._write_tx(cypher, {"edges": chunk})

            self.stats[f"rel:TS_VALIDATES_SWUD({node_type})"] = len(edges)

        # ── Text-mention expansion ────────────────────────────────
        # Create additional TS_VALIDATES_SWUD edges based on function name
        # mentions in test case text fields.  Only for names with a clear
        # identifier pattern (underscore-separated like Adc_Init) to avoid
        # false positives from short names like "Adc" or "Dio".
        self._create_ts_validates_swud_by_name(parsed)

    def _create_ts_validates_swud_by_name(self, parsed: dict):
        """
        Text-mention expansion: create TS_VALIDATES_SWUD edges when a test
        case's text fields mention a SWUD_Function by name.

        Only matches function names containing an underscore (e.g. Adc_Init)
        to avoid false positives from short names.  Skips edges that already
        exist via GUID matching.
        """
        module = self.module.upper()
        test_case_types = [
            "TS_FunctionalTestCase",
            "TS_ConfigTestCase",
            "TS_StaticInterfaceTestCase",
        ]
        text_fields = [
            "test_objective", "test_procedure", "expected_results",
            "traceability_tags",
        ]
        db = self.neo4j_cfg["database"]

        for tc_type in test_case_types:
            # Count existing edges before
            with self._driver.session(database=db) as session:
                before = session.run(
                    f"MATCH (:{tc_type})-[r:TS_VALIDATES_SWUD]->(f:SWUD_Function) "
                    f"WHERE f.module = $module RETURN count(r) AS c",
                    module=module,
                ).single()["c"]

            cypher = (
                f"MATCH (f:SWUD_Function) "
                f"WHERE f.module = $module AND f.function_name CONTAINS '_' "
                f"WITH f "
                f"MATCH (tc:{tc_type} {{module: $module}}) "
                f"WHERE NOT (tc)-[:TS_VALIDATES_SWUD]->(f) "
                f"  AND ("
                + " OR ".join(
                    f"tc.{field} CONTAINS f.function_name" for field in text_fields
                )
                + f") "
                f"MERGE (tc)-[:TS_VALIDATES_SWUD {{source: 'text_mention'}}]->(f)"
            )
            self._write_tx(cypher, {"module": module})

            # Count after to determine how many were created
            with self._driver.session(database=db) as session:
                after = session.run(
                    f"MATCH (:{tc_type})-[r:TS_VALIDATES_SWUD]->(f:SWUD_Function) "
                    f"WHERE f.module = $module RETURN count(r) AS c",
                    module=module,
                ).single()["c"]

            created = after - before
            if created:
                logger.info(
                    "  Text-mention TS_VALIDATES_SWUD from %s: %d new edges",
                    tc_type, created,
                )
                self.stats[f"rel:TS_VALIDATES_SWUD_textmention({tc_type})"] = created

    def _create_ts_tests_config_element(self, parsed: dict):
        """
        TS_TESTS_CONFIG_ELEMENT: TS_ConfigTestCase → SWA_ConfigParam/Container.

        Matches config_path on the test case against container_name or
        param_name on SWA nodes.
        """
        config_tests = parsed.get("TS_ConfigTestCase", [])
        if not config_tests:
            return

        edges = []
        for item in config_tests:
            cp = item.get("config_path")
            if not cp:
                continue
            # Extract the last segment as the param/container name
            # e.g. "/Adc/AdcConfigSet/AdcHwUnit/AdcChannel" → "AdcChannel"
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

        # Match against SWA_ConfigContainer
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (tc:TS_ConfigTestCase {test_case_id: e.test_case_id}) "
                "MATCH (c:SWA_ConfigContainer {container_name: e.leaf_name}) "
                "MERGE (tc)-[:TS_TESTS_CONFIG_ELEMENT]->(c)"
            )
            self._write_tx(cypher, {"edges": chunk})

        # Match against SWA_ConfigParam
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (tc:TS_ConfigTestCase {test_case_id: e.test_case_id}) "
                "MATCH (p:SWA_ConfigParam {param_name: e.leaf_name}) "
                "MERGE (tc)-[:TS_TESTS_CONFIG_ELEMENT]->(p)"
            )
            self._write_tx(cypher, {"edges": chunk})

        self.stats["rel:TS_TESTS_CONFIG_ELEMENT"] = len(edges)

    def _create_ts_measures_timing(self, parsed: dict):
        """
        TS_MEASURES_TIMING_OF: TS_WCETTestCase → SWA_Function.

        Matches api_name on the WCET test case against function_name
        on SWA_ArchitecturalDecision nodes (or SWUD_Function).
        """
        wcet_tests = parsed.get("TS_WCETTestCase", [])
        if not wcet_tests:
            return

        edges = []
        seen = set()
        for item in wcet_tests:
            api = item.get("api_name")
            if not api or api in seen:
                continue
            edges.append({
                "test_case_id": item["test_case_id"],
                "api_name": api,
                "data_point": item.get("data_point", ""),
            })

        if not edges:
            return

        logger.info("  Creating TS_MEASURES_TIMING_OF (%d edges)…", len(edges))

        # Match against SWUD_Function
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (tc:TS_WCETTestCase {test_case_id: e.test_case_id}) "
                "MATCH (f:SWUD_Function {function_name: e.api_name}) "
                "MERGE (tc)-[r:TS_MEASURES_TIMING_OF]->(f) "
                "SET r.data_point = e.data_point"
            )
            self._write_tx(cypher, {"edges": chunk})

        self.stats["rel:TS_MEASURES_TIMING_OF"] = len(edges)

    def _create_ts_belongs_to_module(self, parsed: dict):
        """TS_BELONGS_TO_MODULE: any TS node → MCALModule."""
        module = self.module.upper()
        for node_type in parsed:
            uid_prop = self._UID_MAP.get(node_type)
            if not uid_prop:
                continue

            logger.info("  Creating TS_BELONGS_TO_MODULE for %s → %s …", node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:TS_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

            count_res = self._run(
                f"MATCH (:{node_type})-[r:TS_BELONGS_TO_MODULE]->(:MCALModule) "
                f"RETURN count(r) AS cnt"
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
            "TS_WCETTestCase",
        ]

        for tc_type in test_types:
            items = parsed.get(tc_type, [])
            if not items:
                continue

            logger.info("  Creating TS_CONTAINS_TESTCASE: %s → %s (%d) …",
                        doc_name, tc_type, len(items))
            cypher = (
                f"MATCH (doc:TS_TestSpecDocument {{document_name: $doc_name}}) "
                f"MATCH (tc:{tc_type} {{source_document: $doc_stem}}) "
                f"MERGE (doc)-[:TS_CONTAINS_TESTCASE]->(tc)"
            )
            self._write_tx(cypher, {"doc_name": doc_name, "doc_stem": doc_name})

            count_res = self._run(
                f"MATCH (:TS_TestSpecDocument)-[r:TS_CONTAINS_TESTCASE]->(:{tc_type}) "
                f"RETURN count(r) AS cnt"
            )
            cnt = count_res[0]["cnt"] if count_res else 0
            self.stats[f"rel:TS_CONTAINS_TESTCASE({tc_type})"] = cnt

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self, parsed: dict):
        """Print a summary of what would be created."""
        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW – TestSpec Ingestion, Module: {self.module}")
        print("=" * 60)

        total_nodes = 0
        print(f"\n  Node types:")
        for node_type, items in sorted(parsed.items()):
            uid = self._UID_MAP.get(node_type, "?")
            print(f"    :{node_type:<35s}  {len(items):>5,d} nodes  [merge key: {uid}]")
            total_nodes += len(items)

            # Show a few samples
            for item in items[:3]:
                val = str(item.get(uid, "?"))[:60]
                print(f"      • {val}")
            if len(items) > 3:
                print(f"      … and {len(items) - 3} more")
        print(f"    {'TOTAL':<36s}  {total_nodes:>5,d}")

        # Traceability summary
        prq_count = 0
        swa_count = 0
        swud_count = 0
        hazop_count = 0
        for node_type, items in parsed.items():
            for item in items:
                for ref_key, counter_name in [
                    ("prq_references", "prq"),
                    ("swa_references", "swa"),
                    ("swud_references", "swud"),
                    ("hazop_references", "hazop"),
                ]:
                    refs = item.get(ref_key)
                    if refs:
                        ref_list = json.loads(refs) if isinstance(refs, str) else refs
                        if counter_name == "prq":
                            prq_count += len(ref_list)
                        elif counter_name == "swa":
                            swa_count += len(ref_list)
                        elif counter_name == "swud":
                            swud_count += len(ref_list)
                        elif counter_name == "hazop":
                            hazop_count += len(ref_list)

        print(f"\n  Traceability links (potential edges):")
        print(f"    TS_VERIFIES          → PRQ  : ~{prq_count}")
        print(f"    TS_VALIDATES_SWA     → SWA  : ~{swa_count}")
        print(f"    TS_VALIDATES_SWUD    → SWUD : ~{swud_count}")
        print(f"    HAZOP references            : ~{hazop_count}")

        # Config path matching
        config_tests = parsed.get("TS_ConfigTestCase", [])
        config_paths = sum(1 for t in config_tests if t.get("config_path"))
        if config_paths:
            print(f"    TS_TESTS_CONFIG_ELEMENT      : ~{config_paths}")

        # WCET API matching
        wcet_tests = parsed.get("TS_WCETTestCase", [])
        wcet_apis = len({t.get("api_name") for t in wcet_tests if t.get("api_name")})
        if wcet_apis:
            print(f"    TS_MEASURES_TIMING_OF        : ~{wcet_apis} unique APIs")

        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE – TestSpec Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<35s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<36s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<35s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<36s}  {total_rels:>6,d}")

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Interactive Profile Selection
# ---------------------------------------------------------------------------
def select_profile(ontology: dict) -> str:
    """Prompt the user to select a profile interactively."""
    profiles = list(ontology.get("profiles", {}).keys())
    if not profiles:
        raise ValueError("No profiles found in ontology.")

    print("\n" + "=" * 60)
    print("  Knowledge Graph Builder – Profile Selection")
    print("=" * 60)
    print("\n  Available profiles:\n")

    for i, pname in enumerate(profiles, 1):
        pcfg = ontology["profiles"][pname]
        meta = pcfg.get("metadata", {})
        desc = meta.get("description", "No description")
        node_count = len(pcfg.get("node_types", []))
        rel_count = len(pcfg.get("relationship_types", []))
        modules = meta.get("supported_modules", [])

        print(f"    [{i}] {pname}")
        print(f"        {meta.get('name', pname)}")
        print(f"        {desc[:100]}…" if len(desc) > 100 else f"        {desc}")
        print(f"        Node types: {node_count}  |  Relationship types: {rel_count}  |  Modules: {len(modules)}")
        print()

    while True:
        try:
            choice = input("  Select profile (enter number or name): ").strip()
            if choice.lower() in profiles:
                return choice.lower()
            idx = int(choice)
            if 1 <= idx <= len(profiles):
                return profiles[idx - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Invalid choice. Enter 1-{len(profiles)} or one of: {', '.join(profiles)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Build a Neo4j Knowledge Graph from the automotive ontology.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python build_knowledge_graph.py                                    # interactive\n"
            "  python build_knowledge_graph.py --profile mcal --module ADC        # MCAL ADC module\n"
            "  python build_knowledge_graph.py --profile mcal --module GPT --clear  # MCAL GPT, wipe first\n"
            "  python build_knowledge_graph.py --profile mcal --module ADC --dry-run # preview only\n"
            "  python build_knowledge_graph.py --profile illd --module CXPI          # ILLD profile\n"
            "  python build_knowledge_graph.py --profile illd --module CXPI --clear  # ILLD wipe & rebuild\n"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["mcal", "illd"],
        default=None,
        help="Ontology profile to use. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--module",
        type=str,
        default=None,
        help=(
            "Module name. For MCAL: selects jama-req/jama_<module>_*.json files "
            "(e.g. ADC, GPT, SPI). Default: ADC.  "
            "For ILLD: selects data/<module>/processed/ directory "
            "(e.g. CXPI, SPI). Default: CXPI."
        ),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help=(
            "Override path to data source. For MCAL: JSON file "
            "(default: jama-req/jama_<module>_combined_requirements.json). "
            "For ILLD: processed output directory (default: data/<MODULE>/processed)."
        ),
    )
    parser.add_argument(
        "--relationships",
        type=Path,
        default=None,
        help=(
            "Path to a cached Jama relationships JSON file (MCAL only). "
            "If omitted and no cache exists, relationships are "
            "fetched live from the Jama API."
        ),
    )
    parser.add_argument(
        "--refresh-relationships",
        action="store_true",
        help="Re-fetch relationships from Jama API even if a cached file exists (MCAL only).",
    )
    parser.add_argument(
        "--refresh-folders",
        action="store_true",
        help="Re-fetch folder hierarchy from Jama API even if a cached file exists (MCAL only).",
    )
    parser.add_argument(
        "--ingest-swa",
        action="store_true",
        help=(
            "Ingest SWA (Software Architecture) markdown sections into the "
            "MCAL database.  Parses section_*_raw.md files from the SWA "
            "directory and creates SWA_ArchitecturalDecision, SWA_HwPeripheral, "
            "SWA_SwDependency, SWA_ConfigContainer, SWA_ConfigParam nodes "
            "plus traceability relationships to existing PRQ requirements."
        ),
    )
    parser.add_argument(
        "--swa-dir",
        type=Path,
        default=None,
        help=(
            "Override path to SWA markdown directory. "
            "Default: swa/ under the HybridRAG root. "
            "Should contain section_*_raw.md files produced by prepare_markdowns.py."
        ),
    )
    parser.add_argument(
        "--ingest-swud",
        action="store_true",
        help=(
            "Ingest SWUD (Software Unit Design) markdown sections into the "
            "MCAL database.  Parses the SWUD markdown files and creates "
            "SWUD_Function, SWUD_TypeDefinition, SWUD_DerivedConfigParam, "
            "SWUD_DesignDecision, SWUD_DataVariable, SWUD_CriticalSection, "
            "SWUD_MemorySection, SWUD_ConfigStructure, SWUD_CodeGenMacro nodes "
            "plus traceability relationships to existing PRQ requirements."
        ),
    )
    parser.add_argument(
        "--swud-dir",
        type=Path,
        default=None,
        help=(
            "Override path to SWUD markdown directory. "
            "Default: swud/ under the HybridRAG root. "
            "Should contain the converted SWUD markdown file(s)."
        ),
    )
    parser.add_argument(
        "--ingest-testspec",
        action="store_true",
        help=(
            "Ingest Test Specification (TS) Excel workbook into the "
            "MCAL database.  Parses all sheets (Test cases, Configuration tests, "
            "Static source code IF tests, WCET analysis) and creates "
            "TS_FunctionalTestCase, TS_ConfigTestCase, TS_StaticInterfaceTestCase, "
            "TS_WCETTestCase, TS_TestSpecDocument nodes plus traceability "
            "relationships to existing PRQ, SWA, and SWUD nodes."
        ),
    )
    parser.add_argument(
        "--testspec-dir",
        type=Path,
        default=None,
        help=(
            "Override path to test spec directory containing Excel workbooks. "
            "Default: testspec/ under the HybridRAG root. "
            "Should contain TC4xx_SW_MCAL_TS_<Module>.xlsx files."
        ),
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all existing data in the target database before building.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without touching Neo4j.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of items per UNWIND batch (default: 500).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load configs
    ontology = load_ontology()
    storage_cfg = load_storage_config()

    # Profile selection
    profile = args.profile
    if not profile:
        profile = select_profile(ontology)

    logger.info("Selected profile: %s", profile)

    # Neo4j settings
    neo4j_cfg = get_neo4j_settings(profile, storage_cfg)

    # -----------------------------------------------------------------------
    # Resolve module defaults per profile
    # -----------------------------------------------------------------------
    if not args.module:
        parser.error("--module is required (e.g. --module ADC, --module SPI)")
    module = args.module.upper()

    logger.info("Module: %s", module)

    # -----------------------------------------------------------------------
    # Dispatch: ILLD profile → ILLDKnowledgeGraphBuilder
    # -----------------------------------------------------------------------
    if profile == "illd":
        data_path = args.data or (DATA_DIR / module / "processed")
        builder = ILLDKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            data_path=data_path,
            dry_run=args.dry_run,
            clear_db=args.clear,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        return

    # -----------------------------------------------------------------------
    # Dispatch: --ingest-swa and/or --ingest-swud
    # Both flags can be combined in a single invocation.
    # -----------------------------------------------------------------------
    ran_doc_ingest = False

    if args.ingest_swa:
        swa_dir = args.swa_dir or SWA_DIR
        builder = SWAKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            swa_dir=swa_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_swud:
        swud_dir = args.swud_dir or SWUD_DIR
        builder = SWUDKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            swud_dir=swud_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_testspec:
        testspec_dir = args.testspec_dir or TESTSPEC_DIR
        builder = TestSpecKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            testspec_dir=testspec_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if ran_doc_ingest:
        return

    # -----------------------------------------------------------------------
    # MCAL profile → existing KnowledgeGraphBuilder
    # -----------------------------------------------------------------------

    # Resolve module-specific paths
    module_paths = get_module_paths(module)
    data_path = args.data or module_paths["data"]
    relationships_path = args.relationships or module_paths["relationships"]
    folders_path = module_paths["folders"]

    logger.info("Data file        : %s", data_path)
    logger.info("Relationships    : %s", relationships_path)
    logger.info("Folders cache    : %s", folders_path)

    # Jama settings
    jama_cfg = storage_cfg.get("jama", {})

    # If --refresh-relationships is set, delete the cached file
    if args.refresh_relationships:
        if relationships_path.exists():
            relationships_path.unlink()
            logger.info("Deleted cached relationships file: %s", relationships_path.name)

    # If --refresh-folders is set, delete the cached file
    if args.refresh_folders:
        if folders_path.exists():
            folders_path.unlink()
            logger.info("Deleted cached folders file: %s", folders_path.name)

    # Build
    builder = KnowledgeGraphBuilder(
        profile=profile,
        ontology=ontology,
        neo4j_cfg=neo4j_cfg,
        data_path=data_path,
        dry_run=args.dry_run,
        clear_db=args.clear,
        relationships_path=relationships_path,
        folders_path=folders_path,
        jama_cfg=jama_cfg,
    )
    builder.BATCH_SIZE = args.batch_size
    builder.build()


# NOTE: The complete CLI entry point (main) is defined below after all
# builder classes, including SourceCode and SFR builders.

# ---------------------------------------------------------------------------
# Source Code Knowledge Graph Builder
# ---------------------------------------------------------------------------

class SourceCodeKnowledgeGraphBuilder:
    """
    Builds source-code nodes and relationships in the MCAL Neo4j database
    from C source files (.c / .h) in a module repository.

    This builder fills the **implementation** layer in the V-Model ΓÇö the gap
    between SWUD (detailed design) and Test Specification.  It parses every
    C file in the ``ssc/`` and ``Plugins/`` sub-trees of a module repo,
    extracts functions, data types, macros, call graphs, and register
    accesses, then populates the knowledge graph.

    Node types created (from the MCAL ontology):
        - SRC_SourceFile
        - SRC_Function
        - SRC_DataType
        - SRC_Macro

    Relationships created:
        - SRC_DEFINED_IN          (function/type/macro ΓåÆ file)
        - SRC_CALLS               (function ΓåÆ function)
        - SRC_BELONGS_TO_MODULE   (all ΓåÆ MCALModule)
        - SRC_IMPLEMENTS_SWA      (function ΓåÆ SWA_Function by name)
        - SRC_IMPLEMENTS_SWUD     (function ΓåÆ SWUD_Function by name)

    Usage::

        python build_knowledge_graph.py --profile mcal --module ADC \\
            --ingest-source --source-dir path/to/aurix3g_sw_mcal_tc4xx_adc_src

        python build_knowledge_graph.py --profile mcal --module ADC \\
            --ingest-source --dry-run
    """

    BATCH_SIZE = 500

    _UID_MAP = {
        "SRC_SourceFile":    "file_id",
        "SRC_Function":      "function_id",
        "SRC_DataType":      "type_id",
        "SRC_Macro":         "macro_id",
        "SRC_GlobalVariable": "variable_id",
        "SRC_LocalVariable":  "variable_id",
    }

    # ΓöÇΓöÇ Regex patterns for doc-block extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    # [cover parentID={GUID}] traceability tags
    _RE_COVER_GUID = re.compile(
        r'\[cover\s+parentID=\{([0-9A-Fa-f-]+)\}\]'
    )

    # File header fields
    _RE_HDR_VERSION  = re.compile(r'\*\*\s*VERSION\s*:\s*([\d.]+)', re.IGNORECASE)
    _RE_HDR_DATE     = re.compile(r'\*\*\s*DATE\s*:\s*([\d-]+)', re.IGNORECASE)

    # Function documentation block
    _RE_FUNC_DOC = re.compile(
        r'/\*{5,}.*?'                            # opening stars
        r'Traceability\s*:\s*(.*?)\n'            # traceability line(s)
        r'(.*?)'                                  # description block
        r'\*{5,}/',                              # closing stars
        re.DOTALL
    )

    # Documentation block fields
    _RE_DOC_SYNTAX    = re.compile(r'\*\*\s*Syntax\s*:\s*(.*?)(?:\*\*|\n\*\*)', re.DOTALL)
    _RE_DOC_DESC      = re.compile(r'\*\*\s*Description\s*:\s*(.*?)(?:\n\*\*\s*\n|\n\*\*\s*Service)', re.DOTALL)
    _RE_DOC_SERVICE   = re.compile(r'\*\*\s*Service\s+ID\s*:\s*(0x[0-9A-Fa-f]+)', re.IGNORECASE)
    _RE_DOC_SYNC      = re.compile(r'\*\*\s*Sync/Async\s*:\s*(\S+)', re.IGNORECASE)
    _RE_DOC_REENTRANT = re.compile(r'\*\*\s*Reentrancy\s*:\s*(.*?)(?:\*\*|\n)', re.IGNORECASE)

    # Function definition (with optional static/inline qualifiers)
    _RE_FUNC_DEF = re.compile(
        r'(?:^|\n)'
        r'((?:static\s+)?(?:inline\s+)?(?:LOCAL_INLINE\s+)?)'
        r'([\w\s\*]+?)\s+'
        r'([A-Za-z_]\w*)\s*'
        r'\(([^)]*)\)\s*\{',
        re.MULTILINE
    )

    # C type definitions
    _RE_TYPEDEF_STRUCT = re.compile(
        r'typedef\s+struct\s*(?:\w*\s*)?\{([^}]*)\}\s*(\w+)\s*;',
        re.DOTALL
    )
    _RE_TYPEDEF_UNION = re.compile(
        r'typedef\s+union\s*(?:\w*\s*)?\{([^}]*)\}\s*(\w+)\s*;',
        re.DOTALL
    )
    _RE_TYPEDEF_ENUM = re.compile(
        r'typedef\s+enum\s*(?:\w*\s*)?\{([^}]*)\}\s*(\w+)\s*;',
        re.DOTALL
    )
    _RE_TYPEDEF_SIMPLE = re.compile(
        r'typedef\s+([\w\s\*]+?)\s+(\w+)\s*;'
    )

    # Macro definitions
    _RE_MACRO = re.compile(
        r'^[ \t]*#define\s+([A-Za-z_]\w*)\s*(?:\(.*?\))?\s*(.*?)$',
        re.MULTILINE
    )

    # #include directives
    _RE_INCLUDE = re.compile(r'#include\s+[<"]([^>"]+)[>"]')

    # Conditional compilation (#if ... #endif tracking)
    _RE_IF_COND = re.compile(r'^[ \t]*#if\b\s*(.*)')

    # Global variable declarations at file scope
    # Matches: [static|extern] [const] [volatile] type [*] name [= init] [array] ;
    # Must NOT be a function declaration (which has parentheses)
    _RE_GLOBAL_VAR = re.compile(
        r'^[ \t]*'
        r'((?:(?:static|extern|const|volatile)\s+)*)'  # qualifiers
        r'([\w][\w\s]*?\*?)\s+'                         # type (e.g. uint32, const Adc_ConfigType *)
        r'([A-Za-z_]\w*)\s*'                            # name
        r'(\[[^\]]*\])?\s*'                              # optional array bounds
        r'(?:=\s*([^;]{0,200}))?\s*;',                  # optional initialiser
        re.MULTILINE,
    )

    # Local variable declarations (at start of block)
    _RE_LOCAL_VAR = re.compile(
        r'^[ \t]+'
        r'((?:(?:const|volatile|register)\s+)*)'        # qualifiers
        r'([\w][\w\s]*?\*?)\s+'                         # type
        r'([A-Za-z_]\w*)\s*'                            # name
        r'(\[[^\]]*\])?\s*'                              # optional array bounds
        r'(?:=\s*([^;]{0,200}))?\s*;',                  # optional initialiser
        re.MULTILINE,
    )

    # Memory section markers (AUTOSAR MemMap)
    _RE_MEMSEC_START = re.compile(
        r'#define\s+\w+_START_SEC_(VAR\w+)',
    )
    _RE_MEMSEC_STOP = re.compile(
        r'#define\s+\w+_STOP_SEC_(VAR\w+)',
    )

    # C keywords / control flow ΓÇö skip as "function calls"
    _C_KEYWORDS = {
        'if', 'else', 'while', 'for', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return', 'goto', 'sizeof', 'typeof',
        'void', 'int', 'float', 'double', 'char', 'struct', 'union',
        'typedef', 'const', 'static', 'extern', 'volatile', 'inline',
        'auto', 'register', 'restrict', 'defined', 'NULL_PTR',
    }

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        source_dir: Path,
        dry_run: bool = False,
        temp_dir: Optional[Path] = None,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.source_dir = Path(source_dir)
        self.dry_run = dry_run
        self.temp_dir = temp_dir or (HYBRIDRAG_DIR / "temp" / f"src_{self.module.lower()}")
        self.stats: dict = Counter()
        self._driver = None

        # Collected data
        self._files: list[dict] = []
        self._functions: list[dict] = []
        self._data_types: list[dict] = []
        self._macros: list[dict] = []
        self._global_variables: list[dict] = []
        self._local_variables: list[dict] = []
        self._call_edges: list[dict] = []
        self._register_accesses: list[dict] = []

    # -- Connection ---------------------------------------------------------

    def _connect(self):
        cfg = self.neo4j_cfg
        uri = cfg["uri"]
        logger.info("Connecting to Neo4j at %s ΓÇª", uri)
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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

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
                    logger.error("Source code write failed after %d attempts: %s", max_attempts, exc)
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
                    logger.error("Source code read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # ======================================================================
    # PUBLIC ENTRY POINT
    # ======================================================================

    def build(self):
        """Run the full source-code ingestion pipeline."""
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("Source Code KG Builder ΓÇô module: %s", self.module)
        logger.info("Source directory: %s", self.source_dir)
        logger.info("Temp directory : %s", self.temp_dir)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if not self.source_dir.exists():
            logger.error("Source directory not found: %s", self.source_dir)
            print(
                f"\n  ERROR: Source directory not found:\n"
                f"  {self.source_dir}\n\n"
                f"  Clone the module source repository first.\n"
            )
            sys.exit(1)

        # Step 1: Discover and parse all C files
        logger.info("Step 1/5: Discovering and parsing C filesΓÇª")
        c_files = self._discover_c_files()
        if not c_files:
            logger.warning("No .c/.h files found in %s", self.source_dir)
            return

        logger.info("  Found %d C/H files to parse", len(c_files))
        self._parse_all_files(c_files)

        # Step 2: Save intermediate data to temp/
        logger.info("Step 2/5: Saving intermediate data to %s ΓÇª", self.temp_dir)
        self._save_intermediate()

        if self.dry_run:
            self._preview()
            return

        # Step 3: Connect to Neo4j
        self._connect()

        try:
            # Step 4: Create constraints + nodes
            logger.info("Step 3/5: Creating constraints and indexesΓÇª")
            self._create_constraints()

            logger.info("Step 4/5: Creating nodesΓÇª")
            self._create_nodes()

            # Step 5: Create relationships
            logger.info("Step 5/5: Creating relationshipsΓÇª")
            self._create_relationships()

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # ======================================================================
    # STEP 1: DISCOVER + PARSE
    # ======================================================================

    def _discover_c_files(self) -> list[Path]:
        """Find all .c and .h files under ssc/ and Plugins/."""
        patterns = ["**/*.c", "**/*.h"]
        files = []
        for pat in patterns:
            files.extend(self.source_dir.glob(pat))
        # Exclude .git and irrelevant metadata files
        files = [
            f for f in files
            if ".git" not in f.parts
            and "META-INF" not in f.parts
        ]
        files.sort()
        return files

    def _parse_all_files(self, c_files: list[Path]):
        """Parse all C files and populate internal data structures."""
        # Import the C parser ΓÇö try from IngestionPipeline first
        c_parser = self._get_c_parser()

        for fpath in c_files:
            rel_path = fpath.relative_to(self.source_dir).as_posix()
            logger.info("  Parsing: %s", rel_path)

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Could not read %s: %s", rel_path, exc)
                continue

            # Determine subtree
            subtree = "ssc" if rel_path.lower().startswith("ssc") else "plugins"
            is_header = fpath.suffix.lower() == ".h"

            # ΓöÇΓöÇ File node ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            file_node = self._extract_file_node(rel_path, content, subtree, is_header, fpath)
            self._files.append(file_node)

            # ΓöÇΓöÇ Strip comments for structural parsing ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            clean = self._strip_comments(content)

            # ΓöÇΓöÇ Functions (using C parser for call graph + registers)
            self._extract_functions(rel_path, content, clean, c_parser, fpath)

            # ΓöÇΓöÇ Data types ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            self._extract_data_types(rel_path, content, clean)

            # ΓöÇΓöÇ Macros ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            self._extract_macros(rel_path, content, clean)

            # ΓöÇΓöÇ Global variables (file-scope) ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            self._extract_global_variables(rel_path, content, clean)

    def _get_c_parser(self):
        """Import the C parser module."""
        try:
            sys.path.insert(0, str(ROOT_DIR / "IngestionPipeline" / "Parsers"))
            import c_parser  # type: ignore[import-not-found]
            return c_parser
        except ImportError:
            logger.warning(
                "c_parser not found in IngestionPipeline ΓÇô "
                "falling back to regex-only extraction"
            )
            return None

    # ΓöÇΓöÇ File extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_file_node(
        self, rel_path: str, content: str, subtree: str,
        is_header: bool, fpath: Path,
    ) -> dict:
        """Create a SRC_SourceFile node dict."""
        lines = content.split("\n")
        # Header comments (first ~50 lines)
        header = "\n".join(lines[:50])

        version_m = self._RE_HDR_VERSION.search(header)
        date_m = self._RE_HDR_DATE.search(header)
        guids = self._RE_COVER_GUID.findall(header)
        includes = self._RE_INCLUDE.findall(content)

        # AUTOSAR release from version check
        ar_release = None
        for line in lines[:200]:
            if "AR_RELEASE_MAJOR_VERSION" in line and "!=" in line:
                major = re.search(r'!=\s*(\d+)', line)
                if major:
                    ar_release = f"{major.group(1)}"
                    break
            if "AUTOSAR Release" in line:
                ars = re.search(r'(\d+\.\d+\.\d+)', line)
                if ars:
                    ar_release = ars.group(1)
                    break

        return {
            "file_id": rel_path,
            "file_name": fpath.name,
            "relative_path": rel_path,
            "file_type": "header" if is_header else "source",
            "subtree": subtree,
            "line_count": len(lines),
            "size_bytes": fpath.stat().st_size,
            "version": version_m.group(1) if version_m else None,
            "date": date_m.group(1) if date_m else None,
            "autosar_release": ar_release,
            "includes": json.dumps(includes) if includes else None,
            "traceability_ids": json.dumps(guids) if guids else None,
            "module": self.module,
        }

    # ΓöÇΓöÇ Function extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_functions(
        self, rel_path: str, raw_content: str,
        clean_content: str, c_parser, fpath: Path,
    ):
        """Extract function definitions, call graphs, and register accesses."""
        lines = raw_content.split("\n")

        # Use C parser (regex backend) for call graph + register analysis
        parser_result = None
        if c_parser:
            try:
                parser_result = c_parser.parse(str(fpath), method="regex")
            except Exception as exc:
                logger.debug("C parser failed for %s: %s", rel_path, exc)

        # Build doc-block index: map function names to doc metadata
        doc_blocks = self._extract_doc_blocks(raw_content)

        # Track active #if conditions for compile_condition
        active_conditions = self._build_condition_map(lines)

        # Find all function definitions
        for m in self._RE_FUNC_DEF.finditer(clean_content):
            qualifiers = m.group(1).strip()
            return_type = m.group(2).strip()
            func_name = m.group(3)
            params_raw = m.group(4).strip()

            # Skip C keywords falsely matched
            if func_name in self._C_KEYWORDS:
                continue

            is_static = "static" in qualifiers
            is_inline = "inline" in qualifiers or "LOCAL_INLINE" in qualifiers

            # Find line number
            char_pos = m.start()
            start_line = clean_content[:char_pos].count("\n") + 1

            # Find end line (match braces)
            end_line = self._find_function_end(clean_content, m.end() - 1)

            # Build parameter list
            parameters = []
            if params_raw and params_raw != "void":
                for p in params_raw.split(","):
                    parameters.append(p.strip())

            # Get compile condition at this line
            compile_cond = active_conditions.get(start_line)

            # Get doc block info
            doc = doc_blocks.get(func_name, {})

            func_id = f"{rel_path}::{func_name}"

            func_node = {
                "function_id": func_id,
                "name": func_name,
                "return_type": return_type,
                "parameters": json.dumps(parameters) if parameters else None,
                "signature": f"{return_type} {func_name}({params_raw})",
                "description": doc.get("description"),
                "service_id": doc.get("service_id"),
                "sync_async": doc.get("sync_async"),
                "reentrancy": doc.get("reentrancy"),
                "is_static": is_static,
                "is_inline": is_inline,
                "start_line": start_line,
                "end_line": end_line,
                "compile_condition": compile_cond,
                "traceability_ids": json.dumps(doc["guids"]) if doc.get("guids") else None,
                "module": self.module,
                "_file_id": rel_path,
            }
            self._functions.append(func_node)

            # ΓöÇΓöÇ Local variables ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            # Extract function body text for local variable parsing
            brace_start = m.end() - 1  # position of opening '{'
            body_end_char = self._find_function_end_pos(clean_content, brace_start)
            func_body = clean_content[brace_start + 1 : body_end_char - 1]
            self._extract_local_variables(rel_path, func_name, func_body, start_line)

            # ΓöÇΓöÇ Call graph from C parser result ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
            if parser_result and func_name in parser_result.get("functions", {}):
                func_data = parser_result["functions"][func_name]
                calls = func_data.get("internal_calls", [])
                if isinstance(calls, list):
                    for call in calls:
                        if isinstance(call, dict) and "function" in call:
                            self._call_edges.append({
                                "caller_id": func_id,
                                "callee_name": call["function"],
                                "call_order": call.get("order", 0),
                                "case_label": call.get("case"),
                            })
                        elif isinstance(call, dict) and "calls" in call:
                            # switch-case grouped calls
                            for sc in call.get("calls", []):
                                self._call_edges.append({
                                    "caller_id": func_id,
                                    "callee_name": sc["function"],
                                    "call_order": sc.get("order", 0),
                                    "case_label": sc.get("case"),
                                })

                # Register accesses
                reg_accesses = func_data.get("register_accesses", [])
                for ra in reg_accesses:
                    self._register_accesses.append({
                        "function_id": func_id,
                        "register_name": ra.get("register", ""),
                        "field": ra.get("field", ""),
                        "access_type": ra.get("access_type", ""),
                        "line": ra.get("line", 0),
                    })

    def _extract_doc_blocks(self, content: str) -> dict:
        """Extract Doxygen-like documentation blocks and map to function names."""
        blocks = {}

        # Split by the large star-bordered blocks
        parts = re.split(r'/\*{5,}', content)
        for part in parts:
            end = part.find("*" * 5 + "/")
            if end == -1:
                continue
            block = part[:end]

            # Try to find a Syntax line to identify the function
            syntax_m = self._RE_DOC_SYNTAX.search(block)
            if not syntax_m:
                continue

            syntax_text = syntax_m.group(1).strip().replace("**", "").strip()
            # Extract function name from syntax
            name_m = re.search(r'([A-Za-z_]\w*)\s*\(', syntax_text)
            if not name_m:
                continue

            func_name = name_m.group(1)

            # Extract fields
            desc_m = self._RE_DOC_DESC.search(block)
            service_m = self._RE_DOC_SERVICE.search(block)
            sync_m = self._RE_DOC_SYNC.search(block)
            reentrant_m = self._RE_DOC_REENTRANT.search(block)
            guids = self._RE_COVER_GUID.findall(block)

            description = None
            if desc_m:
                description = desc_m.group(1).replace("**", "").strip()
                description = re.sub(r'\s+', ' ', description)

            blocks[func_name] = {
                "description": description,
                "service_id": service_m.group(1) if service_m else None,
                "sync_async": sync_m.group(1).strip() if sync_m else None,
                "reentrancy": reentrant_m.group(1).strip().replace("**", "").strip() if reentrant_m else None,
                "guids": guids if guids else None,
            }

        return blocks

    def _build_condition_map(self, lines: list[str]) -> dict:
        """Build a mapping from line number to enclosing #if condition."""
        cond_stack: list[str] = []
        cond_map: dict[int, str] = {}

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if_m = self._RE_IF_COND.match(stripped)
            if if_m:
                cond_stack.append(if_m.group(1).strip())
            elif stripped.startswith("#endif"):
                if cond_stack:
                    cond_stack.pop()
            elif stripped.startswith("#else"):
                if cond_stack:
                    top = cond_stack.pop()
                    cond_stack.append(f"!({top})")

            if cond_stack:
                cond_map[i] = cond_stack[-1]

        return cond_map

    def _find_function_end(self, content: str, brace_start: int) -> int:
        """Find the line number of the closing brace."""
        pos = self._find_function_end_pos(content, brace_start)
        return content[:pos].count("\n") + 1

    def _find_function_end_pos(self, content: str, brace_start: int) -> int:
        """Find the character position just past the closing brace."""
        depth = 1
        pos = brace_start + 1
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        return pos

    # ΓöÇΓöÇ Data type extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_data_types(self, rel_path: str, raw_content: str, clean_content: str):
        """Extract struct, union, enum, and typedef definitions."""

        # --- Structs ---
        for m in self._RE_TYPEDEF_STRUCT.finditer(clean_content):
            body, name = m.group(1), m.group(2)
            members = [
                line.strip().rstrip(";")
                for line in body.split("\n")
                if line.strip() and not line.strip().startswith("/*")
            ]
            guids = self._find_preceding_guids(raw_content, name)
            desc = self._find_preceding_comment(raw_content, name)
            start_line = clean_content[:m.start()].count("\n") + 1
            self._data_types.append({
                "type_id": f"{rel_path}::{name}",
                "name": name,
                "kind": "struct",
                "members": json.dumps(members) if members else None,
                "base_type": None,
                "description": desc,
                "traceability_ids": json.dumps(guids) if guids else None,
                "start_line": start_line,
                "module": self.module,
                "_file_id": rel_path,
            })

        # --- Unions ---
        for m in self._RE_TYPEDEF_UNION.finditer(clean_content):
            body, name = m.group(1), m.group(2)
            members = [
                line.strip().rstrip(";")
                for line in body.split("\n")
                if line.strip() and not line.strip().startswith("/*")
            ]
            guids = self._find_preceding_guids(raw_content, name)
            desc = self._find_preceding_comment(raw_content, name)
            start_line = clean_content[:m.start()].count("\n") + 1
            self._data_types.append({
                "type_id": f"{rel_path}::{name}",
                "name": name,
                "kind": "union",
                "members": json.dumps(members) if members else None,
                "base_type": None,
                "description": desc,
                "traceability_ids": json.dumps(guids) if guids else None,
                "start_line": start_line,
                "module": self.module,
                "_file_id": rel_path,
            })

        # --- Enums ---
        for m in self._RE_TYPEDEF_ENUM.finditer(clean_content):
            body, name = m.group(1), m.group(2)
            # Extract enumerator names
            members = re.findall(r'([A-Za-z_]\w*)\s*(?:=|,|\n)', body)
            guids = self._find_preceding_guids(raw_content, name)
            desc = self._find_preceding_comment(raw_content, name)
            start_line = clean_content[:m.start()].count("\n") + 1
            self._data_types.append({
                "type_id": f"{rel_path}::{name}",
                "name": name,
                "kind": "enum",
                "members": json.dumps(members) if members else None,
                "base_type": None,
                "description": desc,
                "traceability_ids": json.dumps(guids) if guids else None,
                "start_line": start_line,
                "module": self.module,
                "_file_id": rel_path,
            })

        # --- Simple typedefs (not already captured) ---
        existing_names = {dt["name"] for dt in self._data_types}
        for m in self._RE_TYPEDEF_SIMPLE.finditer(clean_content):
            base_type, name = m.group(1).strip(), m.group(2).strip()
            if name in existing_names:
                continue
            # Skip struct/union/enum typedefs already found
            if any(kw in base_type for kw in ("struct", "union", "enum")):
                continue
            start_line = clean_content[:m.start()].count("\n") + 1
            self._data_types.append({
                "type_id": f"{rel_path}::{name}",
                "name": name,
                "kind": "typedef",
                "members": None,
                "base_type": base_type,
                "description": self._find_preceding_comment(raw_content, name),
                "traceability_ids": None,
                "start_line": start_line,
                "module": self.module,
                "_file_id": rel_path,
            })

    # ΓöÇΓöÇ Macro extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_macros(self, rel_path: str, raw_content: str, clean_content: str):
        """Extract #define macros."""
        for m in self._RE_MACRO.finditer(raw_content):
            name = m.group(1)
            value = m.group(2).strip().rstrip("\\").strip()

            # Categorise
            category = "general"
            name_upper = name.upper()
            if "_SID" in name_upper or "SERVICE_ID" in name_upper:
                category = "service_id"
            elif name_upper.startswith("E_") or re.match(r'^[A-Z]+_E_', name_upper):
                category = "error_code"
            elif "VERSION" in name_upper:
                category = "version"
            elif "_RESET" in name_upper:
                category = "register_value"
            elif "_OFFSET" in name_upper or "_POS" in name_upper or "_MSK" in name_upper or "_MASK" in name_upper:
                category = "bit_offset"
            elif name_upper.endswith(("_ON", "_OFF")) or value in ("STD_ON", "STD_OFF", "0U", "1U"):
                category = "config_switch"

            start_line = raw_content[:m.start()].count("\n") + 1
            desc = self._find_preceding_comment(raw_content, name)
            guids = self._find_preceding_guids(raw_content, name)

            self._macros.append({
                "macro_id": f"{rel_path}::{name}",
                "name": name,
                "value": value[:500] if value else None,  # truncate very long values
                "macro_category": category,
                "description": desc,
                "traceability_ids": json.dumps(guids) if guids else None,
                "start_line": start_line,
                "module": self.module,
                "_file_id": rel_path,
            })

    # ΓöÇΓöÇ Global variable extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_global_variables(
        self, rel_path: str, raw_content: str, clean_content: str,
    ):
        """Extract file-scope variable declarations (static / extern / plain)."""
        lines = raw_content.split("\n")

        # Build set of known function names so we can skip forward-declarations
        known_func_names = {f["name"] for f in self._functions}
        known_type_names = {dt["name"] for dt in self._data_types}

        # Identify ranges of function bodies to exclude
        func_ranges: list[tuple[int, int]] = []
        for f in self._functions:
            if f["_file_id"] == rel_path:
                func_ranges.append((f.get("start_line", 0), f.get("end_line", 0)))

        # Track current memory section
        active_memsec: Optional[str] = None
        active_conditions = self._build_condition_map(lines)

        for m in self._RE_GLOBAL_VAR.finditer(clean_content):
            qualifiers = m.group(1).strip()
            var_type = m.group(2).strip()
            var_name = m.group(3)
            array_bounds = m.group(4)  # e.g. "[4]"
            initialiser = m.group(5)

            # Skip if this is a known function name (forward declaration matched)
            if var_name in known_func_names:
                continue
            # Skip if this is a typedef name we already captured
            if var_name in known_type_names:
                continue
            # Skip if type is a preprocessor keyword
            if var_type in ("define", "include", "ifdef", "ifndef", "endif", "undef"):
                continue
            # Skip if text ends up matching a function proto (heuristic: if
            # there's a '(' on the same logical line, skip)
            line_start = m.start()
            line_end = clean_content.find("\n", m.end())
            if line_end == -1:
                line_end = len(clean_content)
            # If there's a ( before the ; on this match, it's a function decl
            match_text = clean_content[m.start():m.end()]
            if "(" in match_text:
                continue

            start_line = clean_content[:m.start()].count("\n") + 1

            # Skip variables that fall inside a function body
            inside_func = False
            for (fs, fe) in func_ranges:
                if fs <= start_line <= fe:
                    inside_func = True
                    break
            if inside_func:
                continue

            # Determine memory section from preceding lines
            preceding_raw = raw_content[:raw_content.find(var_name)]
            ms_start = list(self._RE_MEMSEC_START.finditer(preceding_raw))
            ms_stop = list(self._RE_MEMSEC_STOP.finditer(preceding_raw))
            if ms_start:
                last_start = ms_start[-1]
                last_stop_pos = ms_stop[-1].start() if ms_stop else -1
                if last_start.start() > last_stop_pos:
                    active_memsec = last_start.group(1)
                else:
                    active_memsec = None

            is_static = "static" in qualifiers
            is_extern = "extern" in qualifiers
            is_const = "const" in qualifiers or "const" in var_type

            compile_cond = active_conditions.get(start_line)
            desc = self._find_preceding_comment(raw_content, var_name)
            guids = self._find_preceding_guids(raw_content, var_name)

            full_type = f"{qualifiers} {var_type}".strip() if qualifiers else var_type
            if array_bounds:
                full_type += array_bounds

            self._global_variables.append({
                "variable_id": f"{rel_path}::{var_name}",
                "name": var_name,
                "data_type": full_type,
                "is_static": is_static,
                "is_extern": is_extern,
                "is_const": is_const,
                "array_bounds": array_bounds,
                "initial_value": initialiser.strip()[:200] if initialiser else None,
                "memory_section": active_memsec,
                "compile_condition": compile_cond,
                "start_line": start_line,
                "description": desc,
                "traceability_ids": json.dumps(guids) if guids else None,
                "module": self.module,
                "_file_id": rel_path,
            })

    # ΓöÇΓöÇ Local variable extraction ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _extract_local_variables(
        self, rel_path: str, func_name: str, func_body: str,
        func_start_line: int,
    ):
        """Extract local variable declarations from a function body."""
        lines = func_body.split("\n")
        func_id = f"{rel_path}::{func_name}"

        # Build condition map relative to function body
        cond_stack: list[str] = []

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Track #if conditions
            if_m = self._RE_IF_COND.match(stripped)
            if if_m:
                cond_stack.append(if_m.group(1).strip())
                continue
            if stripped.startswith("#endif"):
                if cond_stack:
                    cond_stack.pop()
                continue
            if stripped.startswith("#else"):
                if cond_stack:
                    top = cond_stack.pop()
                    cond_stack.append(f"!({top})")
                continue

            # Skip preprocessor, empty, comments, control flow
            if stripped.startswith("#") or not stripped or stripped.startswith("/*") or stripped.startswith("//"):
                continue
            # Skip lines that are clearly statements (assignments, function calls, returns)
            if stripped.startswith(("return ", "if ", "if(", "else", "for ", "for(",
                                   "while ", "while(", "switch", "do ", "do{",
                                   "break", "continue", "goto ", "}", "{")):
                continue

            m = self._RE_LOCAL_VAR.match(line)
            if not m:
                continue

            qualifiers = m.group(1).strip()
            var_type = m.group(2).strip()
            var_name = m.group(3)
            array_bounds = m.group(4)
            initialiser = m.group(5)

            # Skip C keywords mistakenly matched
            if var_name in self._C_KEYWORDS or var_type in self._C_KEYWORDS:
                continue
            # Skip if the type looks like a control keyword
            if var_type in ("if", "else", "for", "while", "switch", "return", "case"):
                continue

            is_const = "const" in qualifiers or "const" in var_type

            compile_cond = cond_stack[-1] if cond_stack else None
            abs_line = func_start_line + i

            full_type = f"{qualifiers} {var_type}".strip() if qualifiers else var_type
            if array_bounds:
                full_type += array_bounds

            self._local_variables.append({
                "variable_id": f"{func_id}::{var_name}",
                "name": var_name,
                "data_type": full_type,
                "is_const": is_const,
                "array_bounds": array_bounds,
                "initial_value": initialiser.strip()[:200] if initialiser else None,
                "compile_condition": compile_cond,
                "start_line": abs_line,
                "module": self.module,
                "_file_id": rel_path,
                "_function_id": func_id,
            })

    # ΓöÇΓöÇ Helper: find preceding comment / GUIDs ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

    def _find_preceding_comment(self, content: str, name: str) -> Optional[str]:
        """Find the comment text immediately before a given name."""
        idx = content.find(name)
        if idx == -1:
            return None
        # Look backward for a comment block within 500 chars
        search_start = max(0, idx - 500)
        preceding = content[search_start:idx]
        # Multi-line comment
        cm = re.search(r'/\*\s*(.*?)\s*\*/', preceding, re.DOTALL)
        if cm:
            return re.sub(r'\s+', ' ', cm.group(1).replace("*", "").strip())[:500]
        # Single-line comment
        cm = re.search(r'//\s*(.*?)$', preceding, re.MULTILINE)
        if cm:
            return cm.group(1).strip()[:500]
        return None

    def _find_preceding_guids(self, content: str, name: str) -> list[str]:
        """Find [cover parentID={GUID}] tags before a given name."""
        idx = content.find(name)
        if idx == -1:
            return []
        search_start = max(0, idx - 1000)
        preceding = content[search_start:idx]
        return self._RE_COVER_GUID.findall(preceding)

    @staticmethod
    def _strip_comments(code: str) -> str:
        """Remove C comments (block + line)."""
        code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
        code = re.sub(r'//.*?$', '', code, flags=re.MULTILINE)
        return code

    # ======================================================================
    # STEP 2: SAVE INTERMEDIATE DATA
    # ======================================================================

    def _save_intermediate(self):
        """Save parsed data to temp/ folder as JSON files."""
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        datasets = {
            "source_files.json": self._files,
            "functions.json": self._functions,
            "data_types.json": self._data_types,
            "macros.json": self._macros,
            "global_variables.json": self._global_variables,
            "local_variables.json": self._local_variables,
            "call_edges.json": self._call_edges,
            "register_accesses.json": self._register_accesses,
        }

        for filename, data in datasets.items():
            path = self.temp_dir / filename
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            logger.info("    Saved %s (%d items)", filename, len(data))

        # Summary
        summary = {
            "module": self.module,
            "source_dir": str(self.source_dir),
            "total_files": len(self._files),
            "total_functions": len(self._functions),
            "total_data_types": len(self._data_types),
            "total_macros": len(self._macros),
            "total_global_variables": len(self._global_variables),
            "total_local_variables": len(self._local_variables),
            "total_call_edges": len(self._call_edges),
            "total_register_accesses": len(self._register_accesses),
        }
        (self.temp_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        logger.info("    Saved summary.json")

    # ======================================================================
    # STEP 3: CREATE CONSTRAINTS
    # ======================================================================

    def _create_constraints(self):
        """Create uniqueness constraints and indexes for SRC_ node types."""
        for node_type, uid_prop in self._UID_MAP.items():
            constraint_name = f"unique_{node_type}_{uid_prop}"
            cypher = (
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{node_type}) REQUIRE n.{uid_prop} IS UNIQUE"
            )
            try:
                self._write_tx(cypher)
                logger.info("    Constraint: %s", constraint_name)
            except Exception as exc:
                logger.debug("Constraint %s: %s", constraint_name, exc)

        # Name index for function lookup (for call-graph linking)
        try:
            self._write_tx(
                "CREATE INDEX idx_SRC_Function_name IF NOT EXISTS "
                "FOR (n:SRC_Function) ON (n.name)"
            )
        except Exception:
            pass

    # ======================================================================
    # STEP 4: CREATE NODES
    # ======================================================================

    def _create_nodes(self):
        """Create all SRC_ nodes using UNWIND + MERGE."""
        node_groups = {
            "SRC_SourceFile":    self._files,
            "SRC_Function":      self._functions,
            "SRC_DataType":      self._data_types,
            "SRC_Macro":         self._macros,
            "SRC_GlobalVariable": self._global_variables,
            "SRC_LocalVariable":  self._local_variables,
        }

        for node_type, items in node_groups.items():
            if not items:
                continue

            uid_prop = self._UID_MAP[node_type]
            logger.info("  Creating :%s (%d nodes)ΓÇª", node_type, len(items))

            # Clean items for Neo4j ΓÇö remove internal keys, convert None
            batch = []
            for item in items:
                clean = {}
                for k, v in item.items():
                    if k.startswith("_"):  # skip internal keys
                        continue
                    if v is None:
                        continue
                    clean[k] = v
                batch.append(clean)

            for chunk in self._chunked(batch, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{node_type} {{{uid_prop}: props.{uid_prop}}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})

            self.stats[f"nodes:{node_type}"] = len(items)
            logger.info("    ΓåÆ created/merged %d :%s nodes", len(items), node_type)

    # ======================================================================
    # STEP 5: CREATE RELATIONSHIPS
    # ======================================================================

    def _create_relationships(self):
        """Create all source code relationships."""
        self._create_defined_in_rels()
        self._create_call_graph_rels()
        self._create_belongs_to_module_rels()
        self._create_has_global_var_rels()
        self._create_has_local_var_rels()
        self._create_uses_global_rels()
        self._create_includes_rels()
        self._create_implements_swa_rels()
        self._create_implements_swud_rels()
        self._create_traceability_rels()
        self._create_register_access_rels()

    def _create_defined_in_rels(self):
        """SRC_Function / SRC_DataType / SRC_Macro / SRC_*Variable ΓåÆ SRC_SourceFile."""
        for node_type, items, uid_prop in [
            ("SRC_Function", self._functions, "function_id"),
            ("SRC_DataType", self._data_types, "type_id"),
            ("SRC_Macro", self._macros, "macro_id"),
            ("SRC_GlobalVariable", self._global_variables, "variable_id"),
            ("SRC_LocalVariable", self._local_variables, "variable_id"),
        ]:
            edges = [
                {"uid": item[uid_prop], "file_id": item["_file_id"]}
                for item in items
            ]
            if not edges:
                continue

            logger.info("  Creating SRC_DEFINED_IN for %s (%d edges)ΓÇª", node_type, len(edges))
            for chunk in self._chunked(edges, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $edges AS e "
                    f"MATCH (n:{node_type} {{{uid_prop}: e.uid}}) "
                    f"MATCH (f:SRC_SourceFile {{file_id: e.file_id}}) "
                    f"MERGE (n)-[:SRC_DEFINED_IN]->(f)"
                )
                self._write_tx(cypher, {"edges": chunk})
            self.stats[f"rel:SRC_DEFINED_IN({node_type})"] = len(edges)

    def _create_call_graph_rels(self):
        """SRC_Function -[SRC_CALLS]ΓåÆ SRC_Function (by function name)."""
        if not self._call_edges:
            return

        logger.info("  Creating SRC_CALLS (%d edges)ΓÇª", len(self._call_edges))

        # Build lookup: function name ΓåÆ list of function_ids
        # (a function name can appear in multiple files, prefer same-module match)
        func_name_set = {f["name"] for f in self._functions}

        # Filter edges where we know both caller and callee
        valid_edges = []
        for edge in self._call_edges:
            if edge["callee_name"] in func_name_set:
                clean = {
                    "caller_id": edge["caller_id"],
                    "callee_name": edge["callee_name"],
                    "call_order": edge.get("call_order", 0),
                }
                if edge.get("case_label"):
                    clean["case_label"] = edge["case_label"]
                valid_edges.append(clean)

        if not valid_edges:
            logger.info("    No resolvable call edges (callees outside module)")
            return

        logger.info("    %d edges resolve to known functions", len(valid_edges))
        for chunk in self._chunked(valid_edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (caller:SRC_Function {function_id: e.caller_id}) "
                "MATCH (callee:SRC_Function {name: e.callee_name, module: $module}) "
                "MERGE (caller)-[r:SRC_CALLS]->(callee) "
                "SET r.call_order = e.call_order "
                "SET r.case_label = e.case_label"
            )
            self._write_tx(cypher, {"edges": chunk, "module": self.module})
        self.stats["rel:SRC_CALLS"] = len(valid_edges)

    def _create_belongs_to_module_rels(self):
        """SRC_* ΓåÆ MCALModule."""
        module = self.module
        for node_type in ("SRC_SourceFile", "SRC_Function", "SRC_DataType", "SRC_Macro",
                        "SRC_GlobalVariable", "SRC_LocalVariable"):
            logger.info("  Creating SRC_BELONGS_TO_MODULE for %s ΓåÆ %s ΓÇª", node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:SRC_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

        count_res = self._run(
            "MATCH ()-[r:SRC_BELONGS_TO_MODULE]->(:MCALModule {module_name: $module}) "
            "RETURN count(r) AS cnt",
            {"module": module},
        )
        self.stats["rel:SRC_BELONGS_TO_MODULE"] = count_res[0]["cnt"] if count_res else 0

    def _create_has_global_var_rels(self):
        """SRC_SourceFile -[SRC_HAS_GLOBAL_VAR]ΓåÆ SRC_GlobalVariable."""
        if not self._global_variables:
            return

        edges = [
            {"file_id": gv["_file_id"], "var_id": gv["variable_id"]}
            for gv in self._global_variables
        ]
        logger.info("  Creating SRC_HAS_GLOBAL_VAR (%d edges)ΓÇª", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SRC_SourceFile {file_id: e.file_id}) "
                "MATCH (g:SRC_GlobalVariable {variable_id: e.var_id}) "
                "MERGE (f)-[:SRC_HAS_GLOBAL_VAR]->(g)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SRC_HAS_GLOBAL_VAR"] = len(edges)

    def _create_has_local_var_rels(self):
        """SRC_Function -[SRC_HAS_LOCAL_VAR]ΓåÆ SRC_LocalVariable."""
        if not self._local_variables:
            return

        edges = [
            {"func_id": lv["_function_id"], "var_id": lv["variable_id"]}
            for lv in self._local_variables
        ]
        logger.info("  Creating SRC_HAS_LOCAL_VAR (%d edges)ΓÇª", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SRC_Function {function_id: e.func_id}) "
                "MATCH (lv:SRC_LocalVariable {variable_id: e.var_id}) "
                "MERGE (f)-[:SRC_HAS_LOCAL_VAR]->(lv)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SRC_HAS_LOCAL_VAR"] = len(edges)

    def _create_uses_global_rels(self):
        """SRC_Function -[SRC_USES_GLOBAL]ΓåÆ SRC_GlobalVariable.

        Infers usage by checking if a global variable's name appears in
        the function body text (between start_line and end_line).
        Uses a Cypher-only approach: match functions whose line range
        overlaps with global variables in the same file, then use
        apoc-free string matching as a filter.
        """
        if not self._global_variables or not self._functions:
            return

        # Build a mapping of file ΓåÆ global variable names
        file_globals: dict[str, list[str]] = {}
        for gv in self._global_variables:
            fid = gv.get("_file_id", "")
            if fid:
                file_globals.setdefault(fid, []).append(gv["name"])

        if not file_globals:
            return

        # For each file's functions, check which globals they reference
        # by re-reading the source file and scanning function bodies
        edges: list[dict] = []
        global_names_set = {gv["name"] for gv in self._global_variables}

        for func in self._functions:
            func_id = func["function_id"]
            file_id = func.get("_file_id", "")
            # Only check globals from the same module (not just same file)
            func_name = func["name"]

            # Get function body from temp data if available
            start = func.get("start_line", 0)
            end = func.get("end_line", 0)
            if start == 0 or end == 0:
                continue

            # Read source file to get function body
            src_path = self.source_dir / file_id
            if not src_path.exists():
                continue

            try:
                all_lines = src_path.read_text(encoding="utf-8", errors="replace").split("\n")
                body_lines = all_lines[start - 1 : end]
                body_text = "\n".join(body_lines)
            except OSError:
                continue

            # Check which global variable names appear in the function body
            import re as _re
            for gv_name in global_names_set:
                if len(gv_name) < 3:
                    continue  # skip very short names to avoid false matches
                # Word-boundary match to avoid partial name collisions
                if _re.search(r'\b' + _re.escape(gv_name) + r'\b', body_text):
                    gv_id = f"{file_id}::{gv_name}"
                    # Check if this variable_id actually exists
                    if any(gv["variable_id"] == gv_id for gv in self._global_variables):
                        edges.append({"func_id": func_id, "var_id": gv_id})
                    else:
                        # Variable might be from a different file ΓÇö search all
                        for gv in self._global_variables:
                            if gv["name"] == gv_name:
                                edges.append({"func_id": func_id, "var_id": gv["variable_id"]})
                                break

        if not edges:
            logger.info("  Skipping SRC_USES_GLOBAL (no usage edges detected)")
            return

        # Deduplicate
        seen = set()
        unique_edges = []
        for e in edges:
            key = (e["func_id"], e["var_id"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(e)
        edges = unique_edges

        logger.info("  Creating SRC_USES_GLOBAL (%d edges)ΓÇª", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (f:SRC_Function {function_id: e.func_id}) "
                "MATCH (g:SRC_GlobalVariable {variable_id: e.var_id}) "
                "MERGE (f)-[:SRC_USES_GLOBAL]->(g)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SRC_USES_GLOBAL"] = len(edges)

    def _create_includes_rels(self):
        """SRC_SourceFile -[SRC_INCLUDES]ΓåÆ SRC_SourceFile (via #include)."""
        # Build lookup of all known file names ΓåÆ file_id
        fname_to_id: dict[str, str] = {}
        for f in self._files:
            fname_to_id[f["file_name"]] = f["file_id"]

        edges = []
        for f in self._files:
            includes_json = f.get("includes")
            if not includes_json:
                continue
            try:
                includes = json.loads(includes_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for inc in includes:
                # inc could be "Adc_MemMap.h" or "Adc.h" ΓÇö match by filename
                inc_basename = inc.rsplit("/", 1)[-1] if "/" in inc else inc
                target_id = fname_to_id.get(inc_basename)
                if target_id and target_id != f["file_id"]:
                    edges.append({"src_id": f["file_id"], "tgt_id": target_id})

        if not edges:
            logger.info("  Skipping SRC_INCLUDES (no resolvable include edges)")
            return

        logger.info("  Creating SRC_INCLUDES (%d edges)ΓÇª", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (src:SRC_SourceFile {file_id: e.src_id}) "
                "MATCH (tgt:SRC_SourceFile {file_id: e.tgt_id}) "
                "MERGE (src)-[:SRC_INCLUDES]->(tgt)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SRC_INCLUDES"] = len(edges)

    def _create_implements_swa_rels(self):
        """SRC_Function ΓåÆ SWA_Function (by matching function name)."""
        # Check if any SWA_Function nodes exist
        check = self._run(
            "MATCH (s:SWA_Function {module: $module}) RETURN count(s) AS cnt",
            {"module": self.module},
        )
        swa_count = check[0]["cnt"] if check else 0
        if swa_count == 0:
            logger.info("  Skipping SRC_IMPLEMENTS_SWA (no SWA_Function nodes for %s)", self.module)
            return

        logger.info("  Creating SRC_IMPLEMENTS_SWA (matching against %d SWA functions)ΓÇª", swa_count)
        cypher = (
            "MATCH (src:SRC_Function {module: $module}) "
            "MATCH (swa:SWA_Function {function_name: src.name, module: $module}) "
            "MERGE (src)-[:SRC_IMPLEMENTS_SWA]->(swa)"
        )
        self._write_tx(cypher, {"module": self.module})

        count_res = self._run(
            "MATCH (:SRC_Function)-[r:SRC_IMPLEMENTS_SWA]->(:SWA_Function) "
            "RETURN count(r) AS cnt"
        )
        self.stats["rel:SRC_IMPLEMENTS_SWA"] = count_res[0]["cnt"] if count_res else 0

    def _create_implements_swud_rels(self):
        """SRC_Function ΓåÆ SWUD_Function (by matching function name)."""
        check = self._run(
            "MATCH (s:SWUD_Function {module: $module}) RETURN count(s) AS cnt",
            {"module": self.module},
        )
        swud_count = check[0]["cnt"] if check else 0
        if swud_count == 0:
            logger.info("  Skipping SRC_IMPLEMENTS_SWUD (no SWUD_Function nodes for %s)", self.module)
            return

        logger.info("  Creating SRC_IMPLEMENTS_SWUD (matching against %d SWUD functions)ΓÇª", swud_count)
        cypher = (
            "MATCH (src:SRC_Function {module: $module}) "
            "MATCH (swud:SWUD_Function {function_name: src.name, module: $module}) "
            "MERGE (src)-[:SRC_IMPLEMENTS_SWUD]->(swud)"
        )
        self._write_tx(cypher, {"module": self.module})

        count_res = self._run(
            "MATCH (:SRC_Function)-[r:SRC_IMPLEMENTS_SWUD]->(:SWUD_Function) "
            "RETURN count(r) AS cnt"
        )
        self.stats["rel:SRC_IMPLEMENTS_SWUD"] = count_res[0]["cnt"] if count_res else 0

    def _create_traceability_rels(self):
        """SRC_* -[SRC_TRACES_TO]ΓåÆ SWUD_* / SWA_* / ProductRequirement.

        Source code ``[cover parentID={GUID}]`` tags reference design items
        by their ``feature_id`` (UUID) stored on SWUD_* and SWA_* nodes.
        As a fallback, also tries ``global_id`` on requirement nodes.
        """
        # Collect all (node_type, uid_prop, item) with non-empty traceability_ids
        trace_edges: list[dict] = []
        for node_type, items, uid_prop in [
            ("SRC_Function", self._functions, "function_id"),
            ("SRC_DataType", self._data_types, "type_id"),
            ("SRC_Macro", self._macros, "macro_id"),
            ("SRC_GlobalVariable", self._global_variables, "variable_id"),
            ("SRC_SourceFile", self._files, "file_id"),
        ]:
            for item in items:
                guids_json = item.get("traceability_ids")
                if not guids_json:
                    continue
                try:
                    guids = json.loads(guids_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                for guid in guids:
                    if guid:
                        trace_edges.append({
                            "node_type": node_type,
                            "uid_prop": uid_prop,
                            "uid_val": item[uid_prop],
                            "guid": guid,
                        })

        if not trace_edges:
            logger.info("  Skipping SRC_TRACES_TO (no traceability GUIDs found)")
            return

        logger.info("  Creating SRC_TRACES_TO (%d potential edges)ΓÇª", len(trace_edges))

        # Group by node type for efficient batched Cypher
        from collections import defaultdict
        by_type: dict[str, list[dict]] = defaultdict(list)
        for te in trace_edges:
            by_type[te["node_type"]].append(te)

        total_created = 0
        for node_type, edges_for_type in by_type.items():
            uid_prop = edges_for_type[0]["uid_prop"]
            batch = [
                {"uid": e["uid_val"], "guid": e["guid"]}
                for e in edges_for_type
            ]
            for chunk in self._chunked(batch, self.BATCH_SIZE):
                # Match against feature_id on SWUD_*/SWA_* nodes (case-insensitive)
                cypher_design = (
                    "UNWIND $edges AS e "
                    f"MATCH (src:{node_type} {{{uid_prop}: e.uid}}) "
                    "MATCH (tgt) "
                    "WHERE (any(lbl IN labels(tgt) WHERE lbl STARTS WITH 'SWUD_') "
                    "   OR any(lbl IN labels(tgt) WHERE lbl STARTS WITH 'SWA_')) "
                    "AND toUpper(tgt.feature_id) = toUpper(e.guid) "
                    "MERGE (src)-[r:SRC_TRACES_TO]->(tgt) "
                    "SET r.guid = e.guid"
                )
                self._write_tx(cypher_design, {"edges": chunk})

                # Fallback: match against global_id on requirement nodes
                cypher_req = (
                    "UNWIND $edges AS e "
                    f"MATCH (src:{node_type} {{{uid_prop}: e.uid}}) "
                    "MATCH (req) "
                    "WHERE (req:ProductRequirement OR req:StakeholderRequirement) "
                    "AND req.global_id = e.guid "
                    "MERGE (src)-[r:SRC_TRACES_TO]->(req) "
                    "SET r.guid = e.guid"
                )
                self._write_tx(cypher_req, {"edges": chunk})

            # Count created edges
            count_res = self._run(
                f"MATCH (:{node_type})-[r:SRC_TRACES_TO]->() RETURN count(r) AS cnt"
            )
            cnt = count_res[0]["cnt"] if count_res else 0
            total_created += cnt
            logger.info("    %s: %d traceability edges", node_type, cnt)

        self.stats["rel:SRC_TRACES_TO"] = total_created

    def _create_register_access_rels(self):
        """Store register access data as properties on SRC_Function nodes.

        Since register definitions are not separate nodes in the current
        graph, we store the register access summary as a JSON property
        on each SRC_Function node that accesses hardware registers.
        """
        if not self._register_accesses:
            logger.info("  Skipping register access storage (no register accesses extracted)")
            return

        # Group by function_id
        from collections import defaultdict
        by_func: dict[str, list[dict]] = defaultdict(list)
        for ra in self._register_accesses:
            by_func[ra["function_id"]].append({
                "register": ra["register_name"],
                "field": ra.get("field", ""),
                "access_type": ra["access_type"],
                "line": ra.get("line", 0),
            })

        logger.info("  Storing register accesses on %d functionsΓÇª", len(by_func))

        batch = []
        for func_id, accesses in by_func.items():
            # Summarise: unique registers + access types
            registers_read = sorted(set(
                a["register"] for a in accesses if a["access_type"] == "READ"
            ))
            registers_written = sorted(set(
                a["register"] for a in accesses if a["access_type"] == "WRITE"
            ))
            batch.append({
                "func_id": func_id,
                "register_accesses": json.dumps(accesses),
                "registers_read": json.dumps(registers_read),
                "registers_written": json.dumps(registers_written),
                "register_access_count": len(accesses),
            })

        for chunk in self._chunked(batch, self.BATCH_SIZE):
            cypher = (
                "UNWIND $items AS item "
                "MATCH (f:SRC_Function {function_id: item.func_id}) "
                "SET f.register_accesses = item.register_accesses, "
                "    f.registers_read = item.registers_read, "
                "    f.registers_written = item.registers_written, "
                "    f.register_access_count = item.register_access_count"
            )
            self._write_tx(cypher, {"items": chunk})

        self.stats["register_access_functions"] = len(by_func)
        self.stats["register_access_total"] = len(self._register_accesses)
        logger.info("    ΓåÆ %d functions with %d register accesses",
                     len(by_func), len(self._register_accesses))

    # -- Preview (dry-run) --------------------------------------------------

    def _preview(self):
        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW ΓÇô Source Code Ingestion, Module: {self.module}")
        print("=" * 60)

        print(f"\n  Source directory: {self.source_dir}")
        print(f"  Intermediate data saved to: {self.temp_dir}")

        print(f"\n  Node types:")
        for label, items in [
            ("SRC_SourceFile", self._files),
            ("SRC_Function", self._functions),
            ("SRC_DataType", self._data_types),
            ("SRC_Macro", self._macros),
            ("SRC_GlobalVariable", self._global_variables),
            ("SRC_LocalVariable", self._local_variables),
        ]:
            uid = self._UID_MAP.get(label, "?")
            print(f"    :{label:<25s}  {len(items):>6,d} nodes  [merge key: {uid}]")
            for item in items[:3]:
                name = item.get("name", item.get("file_name", item.get(uid, "?")))
                print(f"      ΓÇó {name}")
            if len(items) > 3:
                print(f"      ΓÇª and {len(items) - 3} more")

        total = (len(self._files) + len(self._functions) + len(self._data_types)
                 + len(self._macros) + len(self._global_variables) + len(self._local_variables))
        print(f"    {'TOTAL':<26s}  {total:>6,d}")

        # Count include edges
        include_count = 0
        fname_set = {f["file_name"] for f in self._files}
        for f in self._files:
            inc_json = f.get("includes")
            if inc_json:
                try:
                    for inc in json.loads(inc_json):
                        inc_base = inc.rsplit("/", 1)[-1] if "/" in inc else inc
                        if inc_base in fname_set:
                            include_count += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        defined_in_count = (len(self._functions) + len(self._data_types) + len(self._macros)
                            + len(self._global_variables) + len(self._local_variables))
        print(f"\n  Relationships:")
        print(f"    SRC_DEFINED_IN         : {defined_in_count:>6,d}")
        print(f"    SRC_CALLS              : {len(self._call_edges):>6,d}")
        print(f"    SRC_INCLUDES           : {include_count:>6,d}")
        print(f"    SRC_HAS_GLOBAL_VAR     : {len(self._global_variables):>6,d}")
        print(f"    SRC_HAS_LOCAL_VAR      : {len(self._local_variables):>6,d}")
        print(f"    SRC_BELONGS_TO_MODULE  :  (all nodes)")

        # Traceability count
        trace_count = 0
        for items in (self._functions, self._data_types, self._macros, self._global_variables):
            for item in items:
                guids = item.get("traceability_ids")
                if guids:
                    try:
                        trace_count += len(json.loads(guids))
                    except (json.JSONDecodeError, TypeError):
                        pass
        print(f"\n  Traceability GUIDs found: {trace_count}")
        print(f"  Register accesses found : {len(self._register_accesses)}")

        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE ΓÇô Source Code Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<25s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<26s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<35s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<36s}  {total_rels:>6,d}")

        print(f"\n  Intermediate data: {self.temp_dir}")
        print(f"  (You may delete the temp/ folder once satisfied with the results)")

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ===========================================================================
# SFR (Special Function Register) Knowledge Graph Builder
# ===========================================================================

class SFRKnowledgeGraphBuilder:
    """
    Ingests SFR (Special Function Register) header files into the MCAL
    Neo4j knowledge graph.

    Parses ``_regdef.h``, ``_bf.h``, and ``_reg.h`` files from the cloned
    ``aurix3g_sw_mcal_tc4xx_infra_sfr`` repository for a given peripheral
    module across all (or selected) device variants.

    Node types created:
        - SFR_File          ΓÇô one per header file per device
        - SFR_Register      ΓÇô one per typedef struct per device
        - SFR_BitField      ΓÇô one per bitfield member per device
        - SFR_BaseAddress   ΓÇô one per base-address macro per device

    Relationships created:
        - SFR_DEFINED_IN        (register/base_addr ΓåÆ file)
        - SFR_HAS_BITFIELD      (register ΓåÆ bitfield)
        - SFR_BELONGS_TO_MODULE (all ΓåÆ MCALModule)
        - SRC_ACCESSES_SFR      (SRC_Function ΓåÆ SFR_Register, cross-link)

    Usage::

        python build_knowledge_graph.py --profile mcal --module ADC \\
            --ingest-sfr

        python build_knowledge_graph.py --profile mcal --module ADC \\
            --ingest-sfr --sfr-device TC49xN --dry-run
    """

    BATCH_SIZE = 500

    _UID_MAP = {
        "SFR_File":        "file_id",
        "SFR_Register":    "register_id",
        "SFR_BitField":    "bitfield_id",
        "SFR_BaseAddress": "base_address_id",
    }

    def __init__(
        self,
        neo4j_cfg: dict,
        module: str,
        sfr_dir: Path,
        dry_run: bool = False,
        devices: Optional[list[str]] = None,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.module = module.upper()
        self.sfr_dir = Path(sfr_dir)
        self.dry_run = dry_run
        self.devices = devices          # None ΓåÆ all devices
        self.stats: dict = Counter()
        self._driver = None

        # Parsed data (populated by _parse)
        self._files: list[dict] = []
        self._registers: list[dict] = []
        self._bitfields: list[dict] = []
        self._base_addresses: list[dict] = []

    # -- Connection ---------------------------------------------------------

    def _connect(self):
        cfg = self.neo4j_cfg
        uri = cfg["uri"]
        logger.info("Connecting to Neo4j at %s ΓÇª", uri)
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
            print(
                f"\n  ERROR: Neo4j is not reachable at {uri}.\n"
                f"  Please ensure Neo4j is running and the URI/credentials in\n"
                f"  {STORAGE_CONFIG_PATH} are correct.\n"
            )
            sys.exit(1)
        logger.info("Connected to Neo4j at %s (database: %s)", uri, cfg["database"])

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
                    logger.error("SFR write failed after %d attempts: %s", max_attempts, exc)
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
                    logger.error("SFR read failed after %d attempts: %s", max_attempts, exc)
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient read error (attempt %d/%d), retrying in %ds: %s",
                               attempt, max_attempts, wait, exc)
                time.sleep(wait)
        return []

    # ======================================================================
    # PUBLIC ENTRY POINT
    # ======================================================================

    def build(self):
        """Run the full SFR ingestion pipeline."""
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("SFR KG Builder ΓÇô module: %s", self.module)
        logger.info("SFR directory: %s", self.sfr_dir)
        logger.info("Devices: %s", self.devices or "ALL")
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        if parse_sfr_repo is None:
            logger.error("sfr_parsers module not found. Cannot ingest SFR data.")
            print("\n  ERROR: sfr_parsers.py not found in KG/ directory.\n")
            sys.exit(1)

        if not self.sfr_dir.exists():
            logger.error("SFR directory not found: %s", self.sfr_dir)
            print(
                f"\n  ERROR: SFR directory not found:\n"
                f"  {self.sfr_dir}\n\n"
                f"  Clone the SFR repository first:\n"
                f"  git clone <url> temp/temporary_data/aurix3g_sw_mcal_tc4xx_infra_sfr\n"
            )
            sys.exit(1)

        # Step 1: Parse SFR files
        logger.info("Step 1/4: Parsing SFR header filesΓÇª")
        self._files, self._registers, self._bitfields, self._base_addresses = (
            parse_sfr_repo(
                repo_dir=self.sfr_dir,
                module=self.module,
                devices=self.devices,
            )
        )

        if self.dry_run:
            self._preview()
            return

        # Step 2: Connect to Neo4j
        self._connect()

        try:
            # Step 3: Create constraints + nodes
            logger.info("Step 2/4: Creating constraints and indexesΓÇª")
            self._create_constraints()

            logger.info("Step 3/4: Creating nodesΓÇª")
            self._create_nodes()

            # Step 4: Create relationships
            logger.info("Step 4/4: Creating relationshipsΓÇª")
            self._create_relationships()

            self._print_summary(time.time() - t0)
        finally:
            self._close()

    # ======================================================================
    # CONSTRAINTS
    # ======================================================================

    def _create_constraints(self):
        for node_type, uid_prop in self._UID_MAP.items():
            constraint_name = f"unique_{node_type}_{uid_prop}"
            cypher = (
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{node_type}) REQUIRE n.{uid_prop} IS UNIQUE"
            )
            try:
                self._write_tx(cypher)
                logger.info("    Constraint: %s", constraint_name)
            except Exception as exc:
                logger.debug("Constraint %s: %s", constraint_name, exc)

        # Name indexes for lookup
        for node_type in ("SFR_Register", "SFR_BitField"):
            try:
                self._write_tx(
                    f"CREATE INDEX idx_{node_type}_name IF NOT EXISTS "
                    f"FOR (n:{node_type}) ON (n.name)"
                )
            except Exception:
                pass

    # ======================================================================
    # CREATE NODES
    # ======================================================================

    def _create_nodes(self):
        node_groups = {
            "SFR_File":        self._files,
            "SFR_Register":    self._registers,
            "SFR_BitField":    self._bitfields,
            "SFR_BaseAddress": self._base_addresses,
        }

        for node_type, items in node_groups.items():
            if not items:
                continue

            uid_prop = self._UID_MAP[node_type]
            logger.info("  Creating :%s (%d nodes)ΓÇª", node_type, len(items))

            batch = []
            for item in items:
                clean = {k: v for k, v in item.items()
                         if not k.startswith("_") and v is not None}
                batch.append(clean)

            for chunk in self._chunked(batch, self.BATCH_SIZE):
                cypher = (
                    f"UNWIND $items AS props "
                    f"MERGE (n:{node_type} {{{uid_prop}: props.{uid_prop}}}) "
                    f"ON CREATE SET n.global_id = randomUUID() "
                    f"SET n += props"
                )
                self._write_tx(cypher, {"items": chunk})

            self.stats[f"nodes:{node_type}"] = len(items)
            logger.info("    ΓåÆ created/merged %d :%s nodes", len(items), node_type)

    # ======================================================================
    # CREATE RELATIONSHIPS
    # ======================================================================

    def _create_relationships(self):
        self._create_defined_in_rels()
        self._create_has_bitfield_rels()
        self._create_belongs_to_module_rels()
        self._create_src_accesses_sfr_rels()

    def _create_defined_in_rels(self):
        """SFR_Register ΓåÆ SFR_File (via device + module match on regdef file)."""
        # Registers ΓåÆ regdef file
        for device in {r["device"] for r in self._registers}:
            file_id = next(
                (f["file_id"] for f in self._files
                 if f["device"] == device and f["file_type"] == "regdef"),
                None,
            )
            if not file_id:
                continue

            reg_ids = [r["register_id"] for r in self._registers if r["device"] == device]
            logger.info("  SFR_DEFINED_IN for %d registers ΓåÆ %s", len(reg_ids), file_id)
            for chunk in self._chunked(reg_ids, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $ids AS rid "
                    "MATCH (r:SFR_Register {register_id: rid}) "
                    "MATCH (f:SFR_File {file_id: $file_id}) "
                    "MERGE (r)-[:SFR_DEFINED_IN]->(f)"
                )
                self._write_tx(cypher, {"ids": chunk, "file_id": file_id})
            self.stats["rel:SFR_DEFINED_IN(Register)"] = (
                self.stats.get("rel:SFR_DEFINED_IN(Register)", 0) + len(reg_ids)
            )

        # Base addresses ΓåÆ reg file
        for device in {b["device"] for b in self._base_addresses}:
            file_id = next(
                (f["file_id"] for f in self._files
                 if f["device"] == device and f["file_type"] == "reg"),
                None,
            )
            if not file_id:
                continue

            ba_ids = [b["base_address_id"] for b in self._base_addresses if b["device"] == device]
            logger.info("  SFR_DEFINED_IN for %d base addresses ΓåÆ %s", len(ba_ids), file_id)
            for chunk in self._chunked(ba_ids, self.BATCH_SIZE):
                cypher = (
                    "UNWIND $ids AS bid "
                    "MATCH (b:SFR_BaseAddress {base_address_id: bid}) "
                    "MATCH (f:SFR_File {file_id: $file_id}) "
                    "MERGE (b)-[:SFR_DEFINED_IN]->(f)"
                )
                self._write_tx(cypher, {"ids": chunk, "file_id": file_id})
            self.stats["rel:SFR_DEFINED_IN(BaseAddress)"] = (
                self.stats.get("rel:SFR_DEFINED_IN(BaseAddress)", 0) + len(ba_ids)
            )

    def _create_has_bitfield_rels(self):
        """SFR_Register -[SFR_HAS_BITFIELD]ΓåÆ SFR_BitField."""
        if not self._bitfields:
            return

        edges = [
            {"reg_id": bf["_register_id"], "bf_id": bf["bitfield_id"]}
            for bf in self._bitfields
        ]

        logger.info("  Creating SFR_HAS_BITFIELD (%d edges)ΓÇª", len(edges))
        for chunk in self._chunked(edges, self.BATCH_SIZE):
            cypher = (
                "UNWIND $edges AS e "
                "MATCH (r:SFR_Register {register_id: e.reg_id}) "
                "MATCH (b:SFR_BitField {bitfield_id: e.bf_id}) "
                "MERGE (r)-[:SFR_HAS_BITFIELD]->(b)"
            )
            self._write_tx(cypher, {"edges": chunk})
        self.stats["rel:SFR_HAS_BITFIELD"] = len(edges)

    def _create_belongs_to_module_rels(self):
        """SFR_* ΓåÆ MCALModule."""
        module = self.module
        for node_type in ("SFR_File", "SFR_Register", "SFR_BitField", "SFR_BaseAddress"):
            logger.info("  Creating SFR_BELONGS_TO_MODULE for %s ΓåÆ %s ΓÇª", node_type, module)
            cypher = (
                f"MATCH (n:{node_type} {{module: $module}}) "
                f"MATCH (m:MCALModule {{module_name: $module}}) "
                f"MERGE (n)-[:SFR_BELONGS_TO_MODULE]->(m)"
            )
            self._write_tx(cypher, {"module": module})

        count_res = self._run(
            "MATCH ()-[r:SFR_BELONGS_TO_MODULE]->(:MCALModule {module_name: $module}) "
            "RETURN count(r) AS cnt",
            {"module": module},
        )
        self.stats["rel:SFR_BELONGS_TO_MODULE"] = count_res[0]["cnt"] if count_res else 0

    def _create_src_accesses_sfr_rels(self):
        """SRC_Function -[SRC_ACCESSES_SFR]ΓåÆ SFR_Register.

        Cross-links source code functions to SFR registers by matching
        register names found in the existing SRC_Function nodes'
        ``register_accesses`` data against ingested SFR_Register names.
        """
        # Check if there are any SRC_Function nodes to link
        src_check = self._run(
            "MATCH (f:SRC_Function {module: $module}) RETURN count(f) AS cnt",
            {"module": self.module},
        )
        if not src_check or src_check[0]["cnt"] == 0:
            logger.info("  No SRC_Function nodes for module %s ΓÇö skipping SRC_ACCESSES_SFR", self.module)
            return

        # Build a set of register names we've ingested (without device prefix)
        reg_names = {r["name"] for r in self._registers}

        # For each device, try to link SRC functions ΓåÆ SFR registers
        # The SRC_Function nodes have a `register_accesses` JSON property
        # that lists register names accessed by the function.
        # We match those against our SFR_Register.name property.
        devices = sorted({r["device"] for r in self._registers})
        total = 0

        for device in devices:
            # Get device-specific register names
            device_regs = {r["name"] for r in self._registers if r["device"] == device}

            cypher = (
                "MATCH (f:SRC_Function {module: $module}) "
                "WHERE f.register_accesses IS NOT NULL "
                "WITH f "
                "UNWIND $reg_names AS rname "
                "WITH f, rname "
                "WHERE f.register_accesses CONTAINS rname "
                "MATCH (r:SFR_Register {name: rname, device: $device}) "
                "MERGE (f)-[:SRC_ACCESSES_SFR]->(r)"
            )
            self._write_tx(cypher, {
                "module": self.module,
                "device": device,
                "reg_names": list(device_regs),
            })

        count_res = self._run(
            "MATCH ()-[r:SRC_ACCESSES_SFR]->() RETURN count(r) AS cnt"
        )
        total = count_res[0]["cnt"] if count_res else 0
        self.stats["rel:SRC_ACCESSES_SFR"] = total
        if total:
            logger.info("    ΓåÆ %d SRC_ACCESSES_SFR edges created", total)
        else:
            logger.info("    No SRC_ACCESSES_SFR cross-links found (source code may not store register names)")

    # ======================================================================
    # DRY-RUN PREVIEW
    # ======================================================================

    def _preview(self):
        print("\n" + "=" * 60)
        print(f"  DRY-RUN PREVIEW ΓÇô SFR Ingestion, Module: {self.module}")
        print("=" * 60)

        devices = sorted({f["device"] for f in self._files})
        print(f"\n  SFR directory: {self.sfr_dir}")
        print(f"  Devices ({len(devices)}): {', '.join(devices)}")

        print(f"\n  Node types:")
        for label, items in [
            ("SFR_File", self._files),
            ("SFR_Register", self._registers),
            ("SFR_BitField", self._bitfields),
            ("SFR_BaseAddress", self._base_addresses),
        ]:
            uid = self._UID_MAP.get(label, "?")
            print(f"    :{label:<20s}  {len(items):>6,d} nodes  [merge key: {uid}]")
            for item in items[:3]:
                name = item.get("name", item.get("file_name", item.get(uid, "?")))
                dev = item.get("device", "")
                print(f"      ΓÇó {dev}/{name}")
            if len(items) > 3:
                print(f"      ΓÇª and {len(items) - 3} more")

        total = (len(self._files) + len(self._registers)
                 + len(self._bitfields) + len(self._base_addresses))
        print(f"    {'TOTAL':<21s}  {total:>6,d}")

        print(f"\n  Relationships:")
        print(f"    SFR_DEFINED_IN         : {len(self._registers) + len(self._base_addresses):>6,d}")
        print(f"    SFR_HAS_BITFIELD       : {len(self._bitfields):>6,d}")
        print(f"    SFR_BELONGS_TO_MODULE  :  (all nodes)")
        print(f"    SRC_ACCESSES_SFR       :  (cross-link, computed at ingestion time)")

        # Per-device breakdown
        print(f"\n  Per-device breakdown:")
        for dev in devices:
            regs = sum(1 for r in self._registers if r["device"] == dev)
            bfs = sum(1 for b in self._bitfields if b["device"] == dev)
            bas = sum(1 for b in self._base_addresses if b["device"] == dev)
            print(f"    {dev}: {regs} registers, {bfs} bitfields, {bas} base addresses")

        print("=" * 60 + "\n")

    # -- Summary ------------------------------------------------------------

    def _print_summary(self, elapsed: float):
        print("\n" + "=" * 60)
        print(f"  BUILD COMPLETE ΓÇô SFR Ingestion, Module: {self.module}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<20s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<21s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<30s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<31s}  {total_rels:>6,d}")

        try:
            db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
            rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
            labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
            print(f"\n  Database totals:")
            print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
            print(f"    Relationships: {rel_count[0]['rels']:,d}")
            print(f"    Labels       : {', '.join(labels[0]['labels'])}")
        except Exception:
            pass

        print("=" * 60 + "\n")

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _chunked(lst: list, size: int):
        for i in range(0, len(lst), size):
            yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Interactive Profile Selection
# ---------------------------------------------------------------------------
def select_profile(ontology: dict) -> str:
    """Prompt the user to select a profile interactively."""
    profiles = list(ontology.get("profiles", {}).keys())
    if not profiles:
        raise ValueError("No profiles found in ontology.")

    print("\n" + "=" * 60)
    print("  Knowledge Graph Builder ΓÇô Profile Selection")
    print("=" * 60)
    print("\n  Available profiles:\n")

    for i, pname in enumerate(profiles, 1):
        pcfg = ontology["profiles"][pname]
        meta = pcfg.get("metadata", {})
        desc = meta.get("description", "No description")
        node_count = len(pcfg.get("node_types", []))
        rel_count = len(pcfg.get("relationship_types", []))
        modules = meta.get("supported_modules", [])

        print(f"    [{i}] {pname}")
        print(f"        {meta.get('name', pname)}")
        print(f"        {desc[:100]}ΓÇª" if len(desc) > 100 else f"        {desc}")
        print(f"        Node types: {node_count}  |  Relationship types: {rel_count}  |  Modules: {len(modules)}")
        print()

    while True:
        try:
            choice = input("  Select profile (enter number or name): ").strip()
            if choice.lower() in profiles:
                return choice.lower()
            idx = int(choice)
            if 1 <= idx <= len(profiles):
                return profiles[idx - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Invalid choice. Enter 1-{len(profiles)} or one of: {', '.join(profiles)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Build a Neo4j Knowledge Graph from the automotive ontology.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python build_knowledge_graph.py                                    # interactive\n"
            "  python build_knowledge_graph.py --profile mcal --module ADC        # MCAL ADC module\n"
            "  python build_knowledge_graph.py --profile mcal --module GPT --clear  # MCAL GPT, wipe first\n"
            "  python build_knowledge_graph.py --profile mcal --module ADC --dry-run # preview only\n"
            "  python build_knowledge_graph.py --profile illd --module CXPI          # ILLD profile\n"
            "  python build_knowledge_graph.py --profile illd --module CXPI --clear  # ILLD wipe & rebuild\n"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["mcal", "illd"],
        default=None,
        help="Ontology profile to use. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--module",
        type=str,
        default=None,
        help=(
            "Module name. For MCAL: selects jama-req/jama_<module>_*.json files "
            "(e.g. ADC, GPT, SPI). Default: ADC.  "
            "For ILLD: selects data/<module>/processed/ directory "
            "(e.g. CXPI, SPI). Default: CXPI."
        ),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help=(
            "Override path to data source. For MCAL: JSON file "
            "(default: jama-req/jama_<module>_combined_requirements.json). "
            "For ILLD: processed output directory (default: data/<MODULE>/processed)."
        ),
    )
    parser.add_argument(
        "--relationships",
        type=Path,
        default=None,
        help=(
            "Path to a cached Jama relationships JSON file (MCAL only). "
            "If omitted and no cache exists, relationships are "
            "fetched live from the Jama API."
        ),
    )
    parser.add_argument(
        "--refresh-relationships",
        action="store_true",
        help="Re-fetch relationships from Jama API even if a cached file exists (MCAL only).",
    )
    parser.add_argument(
        "--refresh-folders",
        action="store_true",
        help="Re-fetch folder hierarchy from Jama API even if a cached file exists (MCAL only).",
    )
    parser.add_argument(
        "--ingest-swa",
        action="store_true",
        help=(
            "Ingest SWA (Software Architecture) markdown sections into the "
            "MCAL database.  Parses section_*_raw.md files from the SWA "
            "directory and creates SWA_ArchitecturalDecision, SWA_HwPeripheral, "
            "SWA_SwDependency, SWA_ConfigContainer, SWA_ConfigParam nodes "
            "plus traceability relationships to existing PRQ requirements."
        ),
    )
    parser.add_argument(
        "--swa-dir",
        type=Path,
        default=None,
        help=(
            "Override path to SWA markdown directory. "
            "Default: swa/ under the HybridRAG root. "
            "Should contain section_*_raw.md files produced by prepare_markdowns.py."
        ),
    )
    parser.add_argument(
        "--ingest-swud",
        action="store_true",
        help=(
            "Ingest SWUD (Software Unit Design) markdown sections into the "
            "MCAL database.  Parses the SWUD markdown files and creates "
            "SWUD_Function, SWUD_TypeDefinition, SWUD_DerivedConfigParam, "
            "SWUD_DesignDecision, SWUD_DataVariable, SWUD_CriticalSection, "
            "SWUD_MemorySection, SWUD_ConfigStructure, SWUD_CodeGenMacro nodes "
            "plus traceability relationships to existing PRQ requirements."
        ),
    )
    parser.add_argument(
        "--swud-dir",
        type=Path,
        default=None,
        help=(
            "Override path to SWUD markdown directory. "
            "Default: swud/ under the HybridRAG root. "
            "Should contain the converted SWUD markdown file(s)."
        ),
    )
    parser.add_argument(
        "--ingest-testspec",
        action="store_true",
        help=(
            "Ingest Test Specification (TS) Excel workbook into the "
            "MCAL database.  Parses all sheets (Test cases, Configuration tests, "
            "Static source code IF tests, WCET analysis) and creates "
            "TS_FunctionalTestCase, TS_ConfigTestCase, TS_StaticInterfaceTestCase, "
            "TS_WCETTestCase, TS_TestSpecDocument nodes plus traceability "
            "relationships to existing PRQ, SWA, and SWUD nodes."
        ),
    )
    parser.add_argument(
        "--testspec-dir",
        type=Path,
        default=None,
        help=(
            "Override path to test spec directory containing Excel workbooks. "
            "Default: testspec/ under the HybridRAG root. "
            "Should contain TC4xx_SW_MCAL_TS_<Module>.xlsx files."
        ),
    )
    parser.add_argument(
        "--ingest-source",
        action="store_true",
        help=(
            "Ingest C source code from a module repository into the "
            "MCAL database.  Parses all .c and .h files under ssc/ and "
            "Plugins/ sub-trees and creates SRC_SourceFile, SRC_Function, "
            "SRC_DataType, SRC_Macro nodes plus call-graph and traceability "
            "relationships to existing SWA, SWUD, and PRQ nodes."
        ),
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help=(
            "Path to the cloned module source repository. "
            "Should contain ssc/ and Plugins/ directories. "
            "Example: path/to/aurix3g_sw_mcal_tc4xx_adc_src"
        ),
    )
    parser.add_argument(
        "--ingest-sfr",
        action="store_true",
        help=(
            "Ingest SFR (Special Function Register) header files from the "
            "cloned aurix3g_sw_mcal_tc4xx_infra_sfr repository into the "
            "MCAL database.  Parses _regdef.h, _bf.h, and _reg.h files "
            "for the selected module across all (or filtered) device "
            "variants and creates SFR_File, SFR_Register, SFR_BitField, "
            "SFR_BaseAddress nodes plus SFR_HAS_BITFIELD, SFR_DEFINED_IN, "
            "SFR_BELONGS_TO_MODULE, and SRC_ACCESSES_SFR relationships."
        ),
    )
    parser.add_argument(
        "--sfr-dir",
        type=Path,
        default=None,
        help=(
            "Path to the cloned SFR repository. "
            "Default: temp/temporary_data/aurix3g_sw_mcal_tc4xx_infra_sfr"
        ),
    )
    parser.add_argument(
        "--sfr-device",
        type=str,
        nargs="*",
        default=None,
        help=(
            "Filter SFR ingestion to specific device variant(s). "
            "Example: --sfr-device TC49xN TC4ExA. "
            "Default: all devices."
        ),
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all existing data in the target database before building.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without touching Neo4j.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of items per UNWIND batch (default: 500).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load configs
    ontology = load_ontology()
    storage_cfg = load_storage_config()

    # Profile selection
    profile = args.profile
    if not profile:
        profile = select_profile(ontology)

    logger.info("Selected profile: %s", profile)

    # Neo4j settings
    neo4j_cfg = get_neo4j_settings(profile, storage_cfg)

    # -----------------------------------------------------------------------
    # Resolve module defaults per profile
    # -----------------------------------------------------------------------
    if not args.module:
        parser.error("--module is required (e.g. --module ADC, --module SPI)")
    module = args.module.upper()

    logger.info("Module: %s", module)

    # -----------------------------------------------------------------------
    # Dispatch: ILLD profile → ILLDKnowledgeGraphBuilder
    # -----------------------------------------------------------------------
    if profile == "illd":
        data_path = args.data or (DATA_DIR / module / "processed")
        builder = ILLDKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            data_path=data_path,
            dry_run=args.dry_run,
            clear_db=args.clear,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        return

    # -----------------------------------------------------------------------
    # Dispatch: --ingest-swa and/or --ingest-swud
    # Both flags can be combined in a single invocation.
    # -----------------------------------------------------------------------
    ran_doc_ingest = False

    if args.ingest_swa:
        swa_dir = args.swa_dir or SWA_DIR
        builder = SWAKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            swa_dir=swa_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_swud:
        swud_dir = args.swud_dir or SWUD_DIR
        builder = SWUDKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            swud_dir=swud_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_testspec:
        testspec_dir = args.testspec_dir or TESTSPEC_DIR
        builder = TestSpecKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            testspec_dir=testspec_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_source:
        source_dir = args.source_dir
        if source_dir is None:
            # Default: look under temp/temporary_data/
            source_dir = HYBRIDRAG_DIR / "temp" / "temporary_data" / f"aurix3g_sw_mcal_tc4xx_{module.lower()}_src"
        builder = SourceCodeKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            source_dir=source_dir,
            dry_run=args.dry_run,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if args.ingest_sfr:
        sfr_dir = args.sfr_dir
        if sfr_dir is None:
            sfr_dir = HYBRIDRAG_DIR / "temp" / "temporary_data" / "aurix3g_sw_mcal_tc4xx_infra_sfr"
        builder = SFRKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            module=module,
            sfr_dir=sfr_dir,
            dry_run=args.dry_run,
            devices=args.sfr_device,
        )
        builder.BATCH_SIZE = args.batch_size
        builder.build()
        ran_doc_ingest = True

    if ran_doc_ingest:
        return

    # -----------------------------------------------------------------------
    # MCAL profile ΓåÆ existing KnowledgeGraphBuilder
    # -----------------------------------------------------------------------

    # Resolve module-specific paths
    module_paths = get_module_paths(module)
    data_path = args.data or module_paths["data"]
    relationships_path = args.relationships or module_paths["relationships"]
    folders_path = module_paths["folders"]

    logger.info("Data file        : %s", data_path)
    logger.info("Relationships    : %s", relationships_path)
    logger.info("Folders cache    : %s", folders_path)

    # Jama settings
    jama_cfg = storage_cfg.get("jama", {})

    # If --refresh-relationships is set, delete the cached file
    if args.refresh_relationships:
        if relationships_path.exists():
            relationships_path.unlink()
            logger.info("Deleted cached relationships file: %s", relationships_path.name)

    # If --refresh-folders is set, delete the cached file
    if args.refresh_folders:
        if folders_path.exists():
            folders_path.unlink()
            logger.info("Deleted cached folders file: %s", folders_path.name)

    # Build
    builder = KnowledgeGraphBuilder(
        profile=profile,
        ontology=ontology,
        neo4j_cfg=neo4j_cfg,
        data_path=data_path,
        dry_run=args.dry_run,
        clear_db=args.clear,
        relationships_path=relationships_path,
        folders_path=folders_path,
        jama_cfg=jama_cfg,
    )
    builder.BATCH_SIZE = args.batch_size
    builder.build()


if __name__ == "__main__":
    main()
