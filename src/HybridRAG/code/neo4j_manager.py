"""
Storage Manager for GraphRAG

Manages separate Neo4j and Qdrant connections for ILLD (reference software)
and MCAL (productive software) instances. Configuration is loaded from
config/storage_config.yaml.

Usage:
    # Use the active instance from config
    python neo4j_manager.py --action status

    # Override with a specific instance
    python neo4j_manager.py --instance mcal --action status

    # Verify connectivity (Neo4j + Qdrant)
    python neo4j_manager.py --instance illd --action verify

    # Show current configuration
    python neo4j_manager.py --action config
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from neo4j import GraphDatabase, Driver
from neo4j.exceptions import ServiceUnavailable, AuthError

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code
HYBRIDRAG_DIR = SCRIPT_DIR.parent                     # .../HybridRAG
CONFIG_DIR = HYBRIDRAG_DIR / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "storage_config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("neo4j_manager")


@dataclass
class GraphSettings:
    """Graph-specific tunables stored alongside each instance."""
    node_label_prefix: str = ""
    embedding_dimension: int = 1536
    similarity_threshold: float = 0.85


@dataclass
class Neo4jInstanceConfig:
    """Typed representation of a single Neo4j instance configuration."""
    name: str
    description: str
    uri: str
    username: str
    password: str
    database: str
    encrypted: bool = False
    max_connection_lifetime: int = 3600
    max_connection_pool_size: int = 50
    connection_acquisition_timeout: int = 60
    graph_settings: GraphSettings = field(default_factory=GraphSettings)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Neo4jInstanceConfig":
        gs_raw = data.pop("graph_settings", {})
        graph_settings = GraphSettings(**gs_raw)
        return cls(name=name, graph_settings=graph_settings, **data)


@dataclass
class QdrantInstanceConfig:
    """Typed representation of a Qdrant instance configuration."""
    name: str
    description: str
    url: str
    api_key: str
    collection_name: str
    embedding_dimension: int = 384
    distance_metric: str = "cosine"
    grpc: bool = False
    timeout: int = 30
    port: int = 443
    https: bool = True
    verify_ssl: bool = False

    @classmethod
    def from_dict(cls, name: str, shared: dict, instance: dict) -> "QdrantInstanceConfig":
        return cls(
            name=name,
            description=instance.get("description", ""),
            url=shared.get("url", ""),
            api_key=shared.get("api_key", ""),
            collection_name=instance.get("collection_name", f"{name}_graphrag"),
            embedding_dimension=shared.get("embedding_dimension", 384),
            distance_metric=shared.get("distance_metric", "cosine"),
            grpc=shared.get("grpc", False),
            timeout=shared.get("timeout", 30),
            port=shared.get("port", 443),
            https=shared.get("https", True),
            verify_ssl=shared.get("verify_ssl", False),
        )


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load and return the YAML configuration with env-var resolution."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    from env_config import load_yaml_with_env
    return load_yaml_with_env(config_path)


def _resolve_instance_name(
    raw: dict, instance_name: Optional[str] = None
) -> str:
    """Return the effective instance key, falling back to active_instance."""
    return instance_name or raw.get("active_instance")


def get_instance_config(
    instance_name: Optional[str] = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> Neo4jInstanceConfig:
    """
    Resolve and return the Neo4jInstanceConfig for the requested instance.

    Parameters
    ----------
    instance_name : str, optional
        Explicit instance key ("illd" or "mcal").  When *None*, the
        ``active_instance`` value from the config file is used.
    config_path : Path
        Path to the YAML configuration file.
    """
    raw = load_config(config_path)
    active = _resolve_instance_name(raw, instance_name)
    instances = raw.get("neo4j", {})

    if active not in instances:
        available = ", ".join(instances.keys())
        raise ValueError(
            f"Unknown Neo4j instance '{active}'. Available: {available}"
        )

    logger.info("Using Neo4j instance: %s", active)
    return Neo4jInstanceConfig.from_dict(active, dict(instances[active]))


def get_qdrant_config(
    instance_name: Optional[str] = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> QdrantInstanceConfig:
    """
    Resolve and return the QdrantInstanceConfig for the requested instance.

    The qdrant section has shared fields (url, api_key, embedding_dimension)
    and per-instance sub-keys (illd, mcal) with collection_name/description.
    """
    raw = load_config(config_path)
    active = _resolve_instance_name(raw, instance_name)
    qdrant_cfg = raw.get("qdrant", {})

    if active not in qdrant_cfg:
        available = [k for k in qdrant_cfg if isinstance(qdrant_cfg[k], dict)]
        raise ValueError(
            f"Unknown Qdrant instance '{active}'. Available: {', '.join(available)}"
        )

    logger.info("Using Qdrant instance: %s", active)
    return QdrantInstanceConfig.from_dict(active, qdrant_cfg, dict(qdrant_cfg[active]))


def get_vector_backend(config_path: Path = DEFAULT_CONFIG_PATH) -> str:
    """Return the configured vector backend (qdrant)."""
    raw = load_config(config_path)
    return raw.get("vector_backend", "qdrant").lower().strip()


# ---------------------------------------------------------------------------
# Neo4j Connection Manager
# ---------------------------------------------------------------------------

class Neo4jConnection:
    """
    Context-managed Neo4j driver wrapper.

    Usage::

        with Neo4jConnection(config) as conn:
            result = conn.query("MATCH (n) RETURN count(n) AS cnt")
            print(result)
    """

    def __init__(self, config: Neo4jInstanceConfig):
        self.config = config
        self._driver: Optional[Driver] = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> "Neo4jConnection":
        """Create the driver and verify connectivity."""
        logger.info(
            "Connecting to %s (%s) at %s …",
            self.config.name,
            self.config.description,
            self.config.uri,
        )
        driver_kwargs = dict(
            auth=(self.config.username, self.config.password),
            max_connection_lifetime=self.config.max_connection_lifetime,
            max_connection_pool_size=self.config.max_connection_pool_size,
            connection_acquisition_timeout=self.config.connection_acquisition_timeout,
        )
        # bolt+ssc / bolt+s / neo4j+ssc / neo4j+s carry encryption in the
        # URI scheme — passing 'encrypted' would clash with the driver.
        if "+s" not in self.config.uri.split("://")[0]:
            driver_kwargs["encrypted"] = self.config.encrypted
        self._driver = GraphDatabase.driver(self.config.uri, **driver_kwargs)
        return self

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None
            logger.info("Connection to %s closed.", self.config.name)

    def __enter__(self) -> "Neo4jConnection":
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # -- helpers -------------------------------------------------------------

    @property
    def driver(self) -> Driver:
        if self._driver is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._driver

    def query(self, cypher: str, parameters: Optional[dict] = None, **kwargs):
        """Run a Cypher query and return a list of record dicts."""
        with self.driver.session(database=self.config.database, **kwargs) as session:
            result = session.run(cypher, parameters or {})
            return [record.data() for record in result]

    def verify_connectivity(self) -> bool:
        """Return True when we can reach the database."""
        try:
            self.driver.verify_connectivity()
            logger.info("Connectivity verified for %s.", self.config.name)
            return True
        except (ServiceUnavailable, AuthError) as exc:
            logger.error("Connectivity check failed: %s", exc)
            return False

    def get_database_stats(self) -> dict:
        """Return basic node/relationship counts."""
        counts = self.query(
            "MATCH (n) RETURN count(n) AS node_count"
        )
        rels = self.query(
            "MATCH ()-[r]->() RETURN count(r) AS relationship_count"
        )
        labels = self.query(
            "CALL db.labels() YIELD label RETURN collect(label) AS labels"
        )
        return {
            "instance": self.config.name,
            "description": self.config.description,
            "database": self.config.database,
            "node_count": counts[0]["node_count"] if counts else 0,
            "relationship_count": rels[0]["relationship_count"] if rels else 0,
            "labels": labels[0]["labels"] if labels else [],
        }

    def ensure_fulltext_index(self) -> bool:
        """Create the AICE full-text search index if it does not already exist.

        Index covers 13 searchable properties across all node types used by
        the graph search in SearchService._graph_search(). Uses Lucene-backed
        full-text indexing which is orders of magnitude faster than
        toLower(CONTAINS) property scans.

        Returns True if the index exists (created or already present).
        """
        # Get all labels in the database to build the index over them
        try:
            stats = self.get_database_stats()
            all_labels = stats.get("labels", [])
            if not all_labels:
                logger.warning("No labels found — skipping fulltext index creation")
                return False

            label_list = "|".join(all_labels)
            properties = [
                "name", "function_name", "description", "param_name",
                "requirement_id", "title", "api_name", "type_name",
                "macro_name", "module_name", "file_name", "register_name",
                "struct_name",
            ]
            prop_list = ", ".join(f"n.{p}" for p in properties)

            cypher = (
                f"CREATE FULLTEXT INDEX aice_search_idx IF NOT EXISTS "
                f"FOR (n:{label_list}) ON EACH [{prop_list}]"
            )
            self.query(cypher)
            logger.info("Fulltext index 'aice_search_idx' ensured on %d labels", len(all_labels))
            return True
        except Exception as e:
            logger.warning("Could not create fulltext index: %s", e)
            return False


# ---------------------------------------------------------------------------
# Qdrant Connection Manager
# ---------------------------------------------------------------------------

class QdrantConnection:
    """
    Context-managed Qdrant client wrapper.

    Usage::

        with QdrantConnection(config) as q:
            collection = q.get_or_create_collection()
            print(collection.count())
    """

    def __init__(self, config: QdrantInstanceConfig):
        self.config = config
        self._client = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> "QdrantConnection":
        """Create the Qdrant client (remote HTTP/gRPC)."""
        if not QDRANT_AVAILABLE:
            raise ImportError(
                "qdrant-client is required but not installed. "
                "Install with: pip install qdrant-client>=1.7.0"
            )
        logger.info(
            "Connecting to Qdrant %s (%s) at %s",
            self.config.name,
            self.config.description,
            self.config.url,
        )
        self._client = QdrantClient(
            url=self.config.url,
            port=self.config.port,
            api_key=self.config.api_key,
            https=self.config.https,
            prefer_grpc=self.config.grpc,
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        return self

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            logger.info("Qdrant connection to %s closed.", self.config.name)

    def __enter__(self) -> "QdrantConnection":
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # -- helpers -------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    def _distance(self) -> "Distance":
        """Map config distance_metric to Qdrant Distance enum."""
        mapping = {"cosine": Distance.COSINE, "euclid": Distance.EUCLID, "dot": Distance.DOT}
        return mapping.get(self.config.distance_metric, Distance.COSINE)

    def get_or_create_collection(self, name: Optional[str] = None):
        """Return the configured collection, creating it if needed."""
        collection_name = name or self.config.collection_name
        existing = [c.name for c in self.client.get_collections().collections]
        if collection_name not in existing:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=self.config.embedding_dimension,
                    distance=self._distance(),
                ),
            )
            logger.info(
                "Created Qdrant collection '%s' (dim=%d, %s)",
                collection_name, self.config.embedding_dimension,
                self.config.distance_metric,
            )
        return collection_name

    def verify_connectivity(self) -> bool:
        """Return True when the Qdrant server is reachable."""
        try:
            self.client.get_collections()
            logger.info("Qdrant connectivity verified for %s.", self.config.name)
            return True
        except Exception as exc:
            logger.error("Qdrant connectivity check failed: %s", exc)
            return False

    def get_collection_stats(self) -> dict:
        """Return basic collection statistics."""
        collection_name = self.config.collection_name
        try:
            info = self.client.get_collection(collection_name)
            count = info.points_count or 0
        except Exception:
            count = "N/A"

        return {
            "instance": self.config.name,
            "description": self.config.description,
            "collection_name": collection_name,
            "url": self.config.url,
            "distance_metric": self.config.distance_metric,
            "document_count": count,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_neo4j_config(config: Neo4jInstanceConfig) -> None:
    print(f"\n{'=' * 50}")
    print(f" [Neo4j]")
    print(f" Instance   : {config.name}")
    print(f" Description: {config.description}")
    print(f" URI        : {config.uri}")
    print(f" Database   : {config.database}")
    print(f" Username   : {config.username}")
    print(f" Encrypted  : {config.encrypted}")
    print(f" Prefix     : {config.graph_settings.node_label_prefix}")
    print(f" Embedding  : {config.graph_settings.embedding_dimension}d")
    print(f" Similarity : {config.graph_settings.similarity_threshold}")
    print(f"{'=' * 50}")


def _print_qdrant_config(config: QdrantInstanceConfig) -> None:
    print(f"\n{'=' * 50}")
    print(f" [Qdrant]")
    print(f" Instance   : {config.name}")
    print(f" Description: {config.description}")
    print(f" URL        : {config.url}")
    print(f" Collection : {config.collection_name}")
    print(f" Metric     : {config.distance_metric}")
    print(f" Emb. Dim.  : {config.embedding_dimension}")
    print(f" gRPC       : {config.grpc}")
    print(f"{'=' * 50}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Storage Manager for ILLD / MCAL GraphRAG instances"
    )
    parser.add_argument(
        "--instance",
        choices=["illd", "mcal", "test", "local"],
        default=None,
        help="Instance to use (defaults to active_instance in config)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--store",
        choices=["neo4j", "qdrant", "vector", "all"],
        default="all",
        help="Which store to target. 'vector' uses the configured vector_backend. (default: all)",
    )
    parser.add_argument(
        "--action",
        choices=["config", "verify", "status"],
        default="config",
        help=(
            "config  – display resolved configuration\n"
            "verify  – test database connectivity\n"
            "status  – show database statistics"
        ),
    )
    args = parser.parse_args()

    use_neo4j = args.store in ("neo4j", "all")
    use_qdrant = args.store in ("qdrant", "vector", "all")

    # -- resolve configs ----------------------------------------------------
    neo4j_cfg = None
    qdrant_cfg = None

    try:
        if use_neo4j:
            neo4j_cfg = get_instance_config(args.instance, args.config)
        if use_qdrant:
            qdrant_cfg = get_qdrant_config(args.instance, args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    # -- action: config -----------------------------------------------------
    if args.action == "config":
        if neo4j_cfg:
            _print_neo4j_config(neo4j_cfg)
        if qdrant_cfg:
            _print_qdrant_config(qdrant_cfg)
        return

    # -- action: verify / status --------------------------------------------
    if neo4j_cfg:
        try:
            with Neo4jConnection(neo4j_cfg) as conn:
                if args.action == "verify":
                    ok = conn.verify_connectivity()
                    print(f"Neo4j   : {'OK' if ok else 'FAILED'}")
                elif args.action == "status":
                    stats = conn.get_database_stats()
                    print(f"\n--- Neo4j: {stats['instance']} ({stats['description']}) ---")
                    print(f"Database           : {stats['database']}")
                    print(f"Total nodes        : {stats['node_count']}")
                    print(f"Total relationships: {stats['relationship_count']}")
                    print(f"Labels             : {', '.join(stats['labels']) or '(none)'}")
        except (ServiceUnavailable, AuthError, Exception) as exc:
            logger.error("Neo4j (%s) is not reachable: %s", neo4j_cfg.name, exc)
            print(f"Neo4j   : FAILED – {exc}")

    if qdrant_cfg:
        try:
            with QdrantConnection(qdrant_cfg) as qconn:
                if args.action == "verify":
                    ok = qconn.verify_connectivity()
                    print(f"Qdrant  : {'OK' if ok else 'FAILED'}")
                elif args.action == "status":
                    stats = qconn.get_collection_stats()
                    print(f"\n--- Qdrant: {stats['instance']} ({stats['description']}) ---")
                    print(f"Collection  : {stats['collection_name']}")
                    print(f"URL         : {stats['url']}")
                    print(f"Metric      : {stats['distance_metric']}")
                    print(f"Documents   : {stats['document_count']}")
        except Exception as exc:
            logger.error("Qdrant (%s) is not reachable: %s", qdrant_cfg.name, exc)
            print(f"Qdrant  : FAILED – {exc}")


if __name__ == "__main__":
    main()
