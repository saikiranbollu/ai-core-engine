"""
ReqIF Knowledge Graph Builder
==============================

Ingests parsed ReqIF hardware user manual data into Neo4j.

Node Types Created:
    - HW_Module       — Top-level hardware module (GPT12, ADC, DMA, etc.)
    - HW_Section      — Content section (heading/information/table)
    - HW_Register     — Hardware register with address/reset info
    - HW_BitField     — Individual bitfield within a register
    - HW_Image        — Diagram/formula image with LLM-generated description

Relationships Created:
    - HAS_SECTION       (HW_Module → HW_Section)
    - HAS_REGISTER      (HW_Module → HW_Register)
    - DESCRIBES_REGISTER (HW_Section → HW_Register)
    - HAS_BITFIELD      (HW_Register → HW_BitField)
    - HAS_IMAGE         (HW_Section → HW_Image)
    - NEXT_SECTION      (HW_Section → HW_Section) — document order

Usage::

    from reqif_kg_builder import ReqIFKnowledgeGraphBuilder

    builder = ReqIFKnowledgeGraphBuilder(
        neo4j_cfg={"uri": "...", "username": "...", "password": "...", "database": "neo4j"},
        reqifz_path="path/to/file.reqifz",
        module="GPT12",
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
    from .reqif_parser import ReqIFParser, ReqIFModule, ReqIFSection, ReqIFImage
    from .reqif_image_descriptor import ReqIFImageDescriptor
except ImportError:
    from reqif_parser import ReqIFParser, ReqIFModule, ReqIFSection, ReqIFImage
    from reqif_image_descriptor import ReqIFImageDescriptor

logger = logging.getLogger(__name__)


class ReqIFKnowledgeGraphBuilder:
    """Builds a Neo4j knowledge graph from ReqIF hardware user manual data."""

    BATCH_SIZE = 200

    def __init__(
        self,
        neo4j_cfg: dict,
        reqifz_path: str | Path,
        module: str,
        *,
        describe_images: bool = True,
        image_cache_dir: Optional[Path] = None,
        dry_run: bool = False,
        clear_module: bool = False,
        device_variant: str = "TC44x",
        um_version: str = "unknown",
        project: str = "A3G",
    ):
        """
        Args:
            neo4j_cfg: Dict with uri, username, password, database keys.
            reqifz_path: Path to .reqifz file.
            module: Module name to extract (e.g. "GPT12", "ADC").
            describe_images: Whether to use LLM vision for image descriptions.
            image_cache_dir: Directory for caching image descriptions.
            dry_run: If True, parse but don't write to Neo4j.
            clear_module: If True, delete existing HW nodes for this module first.
            device_variant: Device variant (e.g. "TC44x").
            um_version: User Manual version string (e.g. "v00.90").
        """
        self.neo4j_cfg = neo4j_cfg
        self.reqifz_path = Path(reqifz_path)
        self.module_name = module.upper()
        self.describe_images = describe_images
        self.dry_run = dry_run
        self.clear_module = clear_module
        self.device_variant = device_variant
        self.um_version = um_version
        self.project = project
        self.stats: Counter = Counter()
        self._driver = None

        # Image descriptor
        cache_dir = image_cache_dir or Path("temp/reqif_image_cache")
        self._image_descriptor = ReqIFImageDescriptor(cache_dir=cache_dir) if describe_images else None

        # Parser
        self._parser = ReqIFParser(self.reqifz_path)

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
        """Execute a read query with retry logic."""
        max_attempts = 3
        db = self.neo4j_cfg["database"]
        for attempt in range(1, max_attempts + 1):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= max_attempts:
                    raise
                time.sleep(min(2 ** attempt, 8))
        return []

    # -- Schema Constraints -------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for HW node types."""
        constraints = [
            ("hw_module_uid", "HW_Module", "module_id"),
            ("hw_section_uid", "HW_Section", "section_id"),
            ("hw_register_uid", "HW_Register", "register_id"),
            ("hw_bitfield_uid", "HW_BitField", "bitfield_id"),
            ("hw_image_uid", "HW_Image", "image_id"),
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
        """Delete all existing HW nodes for this module + device combination."""
        logger.info("Clearing existing HW nodes for module=%s, device=%s …",
                   self.module_name, self.device_variant)
        cypher = """
        MATCH (n)
        WHERE (n:HW_Module OR n:HW_Section OR n:HW_Register OR n:HW_BitField OR n:HW_Image)
          AND n.module = $module
          AND n.device = $device
        DETACH DELETE n
        """
        self._write_tx(cypher, {"module": self.module_name, "device": self.device_variant})
        self.stats["cleared"] += 1

    # ======================================================================
    # PUBLIC ENTRY POINT
    # ======================================================================

    def build(self):
        """Run the full ReqIF → Neo4j ingestion pipeline."""
        t0 = time.time()

        print("=" * 60)
        print(f"  ReqIF KG Builder — module: {self.module_name}")
        print(f"  ReqIF file: {self.reqifz_path.name}")
        print(f"  Device: {self.device_variant}  |  UM Version: {self.um_version}")
        print(f"  Describe images: {self.describe_images}")
        print(f"  Dry run: {self.dry_run}")
        print("=" * 60)

        # 1. Parse ReqIF
        logger.info("Step 1: Parsing ReqIF…")
        self._parser.load()
        module_data = self._parser.extract_module(self.module_name)
        if not module_data:
            logger.error("Module '%s' not found in ReqIF!", self.module_name)
            available = self._parser.get_module_names()[:20]
            print(f"\n  ERROR: Module not found. Available modules: {available}\n")
            sys.exit(1)

        print(f"  Parsed: {len(module_data.sections)} sections")
        self.stats["sections_parsed"] = len(module_data.sections)

        # 2. Process images with LLM vision
        if self.describe_images and self._image_descriptor:
            logger.info("Step 2: Describing images with LLM vision…")
            self._describe_all_images(module_data)
        else:
            logger.info("Step 2: Skipping image description (disabled)")

        # 3. Connect to Neo4j and ingest
        if not self.dry_run:
            logger.info("Step 3: Ingesting into Neo4j…")
            self._connect()
            try:
                self._ensure_constraints()
                if self.clear_module:
                    self._clear_existing()
                self._ingest_module(module_data)
            finally:
                self._close()
        else:
            logger.info("Step 3: Dry run — skipping Neo4j ingestion")

        # 4. Report
        elapsed = time.time() - t0
        self._print_report(elapsed)

        # Save image cache
        if self._image_descriptor:
            self._image_descriptor.save_cache()

    # -- Image Description --------------------------------------------------

    def _describe_all_images(self, module_data: ReqIFModule):
        """Describe all images in the module using LLM vision."""
        all_images: list[tuple[bytes, str, str]] = []

        for section in module_data.sections:
            for img in section.images:
                img_bytes = self._parser.extract_image_bytes(img.path)
                if img_bytes:
                    all_images.append((img_bytes, img.image_type, img.path))

        if not all_images:
            logger.info("No images to describe")
            return

        logger.info("Describing %d images…", len(all_images))

        def progress(done, total):
            pct = done / total * 100
            print(f"\r  Images: {done}/{total} ({pct:.0f}%)", end="", flush=True)

        descriptions = self._image_descriptor.describe_batch(all_images, progress_callback=progress)
        print()  # newline after progress

        # Map descriptions back to ReqIFImage objects
        for section in module_data.sections:
            for img in section.images:
                if img.path in descriptions:
                    img.description = descriptions[img.path]

        stats = self._image_descriptor.stats
        logger.info("Image stats: %s", stats)
        self.stats["images_described"] = stats.get("described", 0)
        self.stats["images_cached"] = stats.get("cached", 0)
        self.stats["images_failed"] = stats.get("failed", 0)

    # -- Neo4j Ingestion (Batched) ------------------------------------------

    def _ingest_module(self, module_data: ReqIFModule):
        """Create all HW nodes and relationships for a module using batched writes.

        Uses UNWIND to batch all node/relationship creation into a small number
        of transactions instead of one per entity (reduces network round-trips
        from ~1200 to ~10).
        """
        module_id = f"HW_{self.device_variant}_{module_data.name}"

        # 1. Create HW_Module node (single write)
        cypher = """
        MERGE (m:HW_Module {module_id: $module_id})
        SET m.name = $name,
            m.prefix = $prefix,
            m.device = $device_variant,
            m.um_version = $um_version,
            m.reqif_obj_id = $obj_id,
            m.source = 'reqif',
            m.module = $module,
            m.project = $project
        """
        self._write_tx(cypher, {
            "module_id": module_id,
            "name": module_data.name,
            "prefix": module_data.prefix,
            "device_variant": self.device_variant,
            "um_version": self.um_version,
            "obj_id": module_data.chapter_obj_id,
            "module": self.module_name,
            "project": self.project,
        })
        self.stats["nodes_HW_Module"] += 1

        # 2. Collect all data into batch lists
        sections_batch = []
        registers_batch = []
        bitfields_batch = []
        images_batch = []
        next_section_pairs = []

        prev_section_id = None
        for idx, section in enumerate(module_data.sections):
            section_id = f"{module_id}_sec_{idx:04d}"

            sections_batch.append({
                "section_id": section_id,
                "title": section.title[:500],
                "kind": section.kind,
                "text_content": section.text_content[:50000],
                "has_table": section.has_table,
                "obj_id": section.obj_id,
                "order": idx,
                "module": self.module_name,
                "device": self.device_variant,
                "um_version": self.um_version,
                "module_id": module_id,
                "project": self.project,
            })

            if prev_section_id:
                next_section_pairs.append({
                    "prev_id": prev_section_id,
                    "curr_id": section_id,
                })
            prev_section_id = section_id

            # Collect registers
            if section.register:
                register = section.register
                register_id = f"{module_id}_reg_{register.name}"
                registers_batch.append({
                    "register_id": register_id,
                    "name": register.name,
                    "long_name": register.long_name,
                    "offset": register.offset,
                    "size": register.size,
                    "reset_value": register.reset_value,
                    "access": register.access,
                    "module": self.module_name,
                    "device": self.device_variant,
                    "um_version": self.um_version,
                    "module_id": module_id,
                    "section_id": section_id,
                    "project": self.project,
                })

                for bf in register.bitfields:
                    bitfield_id = f"{register_id}_bf_{bf.name}"
                    bitfields_batch.append({
                        "bitfield_id": bitfield_id,
                        "name": bf.name,
                        "bits": bf.bits,
                        "field_type": bf.field_type,
                        "reset_value": bf.reset_value,
                        "description": bf.description[:2000],
                        "module": self.module_name,
                        "device": self.device_variant,
                        "um_version": self.um_version,
                        "register_id": register_id,
                        "project": self.project,
                    })

            # Collect images
            for img_idx, img in enumerate(section.images):
                image_id = f"{section_id}_img_{img_idx:03d}"
                images_batch.append({
                    "image_id": image_id,
                    "path": img.path,
                    "image_type": img.image_type,
                    "alt_text": img.alt_text[:500],
                    "description": img.description[:8000],
                    "module": self.module_name,
                    "device": self.device_variant,
                    "um_version": self.um_version,
                    "section_id": section_id,
                    "project": self.project,
                })

        # 3. Execute batched writes
        logger.info("Batch write: %d sections, %d registers, %d bitfields, %d images",
                    len(sections_batch), len(registers_batch),
                    len(bitfields_batch), len(images_batch))

        # Batch: Sections + HAS_SECTION
        if sections_batch:
            self._write_tx("""
            UNWIND $batch AS sec
            MERGE (s:HW_Section {section_id: sec.section_id})
            SET s.title = sec.title,
                s.kind = sec.kind,
                s.text_content = sec.text_content,
                s.has_table = sec.has_table,
                s.reqif_obj_id = sec.obj_id,
                s.order_index = sec.order,
                s.module = sec.module,
                s.device = sec.device,
                s.um_version = sec.um_version,
                s.project = sec.project
            WITH s, sec
            MATCH (m:HW_Module {module_id: sec.module_id})
            MERGE (m)-[:HAS_SECTION]->(s)
            """, {"batch": sections_batch})
            self.stats["nodes_HW_Section"] += len(sections_batch)
            self.stats["rels_HAS_SECTION"] += len(sections_batch)

        # Batch: NEXT_SECTION chain
        if next_section_pairs:
            self._write_tx("""
            UNWIND $batch AS pair
            MATCH (a:HW_Section {section_id: pair.prev_id})
            MATCH (b:HW_Section {section_id: pair.curr_id})
            MERGE (a)-[:NEXT_SECTION]->(b)
            """, {"batch": next_section_pairs})
            self.stats["rels_NEXT_SECTION"] = len(next_section_pairs)

        # Batch: Registers + HAS_REGISTER + DESCRIBES_REGISTER
        if registers_batch:
            self._write_tx("""
            UNWIND $batch AS reg
            MERGE (r:HW_Register {register_id: reg.register_id})
            SET r.name = reg.name,
                r.long_name = reg.long_name,
                r.offset = reg.offset,
                r.size = reg.size,
                r.reset_value = reg.reset_value,
                r.access = reg.access,
                r.module = reg.module,
                r.device = reg.device,
                r.um_version = reg.um_version,
                r.project = reg.project
            WITH r, reg
            MATCH (m:HW_Module {module_id: reg.module_id})
            MERGE (m)-[:HAS_REGISTER]->(r)
            WITH r, reg
            MATCH (s:HW_Section {section_id: reg.section_id})
            MERGE (s)-[:DESCRIBES_REGISTER]->(r)
            """, {"batch": registers_batch})
            self.stats["nodes_HW_Register"] += len(registers_batch)
            self.stats["rels_HAS_REGISTER"] += len(registers_batch)

        # Batch: BitFields + HAS_BITFIELD
        if bitfields_batch:
            self._write_tx("""
            UNWIND $batch AS bf
            MERGE (b:HW_BitField {bitfield_id: bf.bitfield_id})
            SET b.name = bf.name,
                b.bits = bf.bits,
                b.field_type = bf.field_type,
                b.reset_value = bf.reset_value,
                b.description = bf.description,
                b.module = bf.module,
                b.device = bf.device,
                b.um_version = bf.um_version,
                b.project = bf.project
            WITH b, bf
            MATCH (r:HW_Register {register_id: bf.register_id})
            MERGE (r)-[:HAS_BITFIELD]->(b)
            """, {"batch": bitfields_batch})
            self.stats["nodes_HW_BitField"] += len(bitfields_batch)
            self.stats["rels_HAS_BITFIELD"] += len(bitfields_batch)

        # Batch: Images + HAS_IMAGE
        if images_batch:
            self._write_tx("""
            UNWIND $batch AS img
            MERGE (i:HW_Image {image_id: img.image_id})
            SET i.path = img.path,
                i.image_type = img.image_type,
                i.alt_text = img.alt_text,
                i.description = img.description,
                i.module = img.module,
                i.device = img.device,
                i.um_version = img.um_version,
                i.project = img.project
            WITH i, img
            MATCH (s:HW_Section {section_id: img.section_id})
            MERGE (s)-[:HAS_IMAGE]->(i)
            """, {"batch": images_batch})
            self.stats["nodes_HW_Image"] += len(images_batch)
            self.stats["rels_HAS_IMAGE"] += len(images_batch)

    # -- Reporting ----------------------------------------------------------

    def _print_report(self, elapsed: float):
        """Print summary report."""
        print("\n" + "=" * 60)
        print(f"  ReqIF KG Builder — COMPLETE ({elapsed:.1f}s)")
        print("=" * 60)
        print(f"\n  Module: {self.module_name}")
        print(f"  Sections parsed: {self.stats['sections_parsed']}")
        print(f"\n  Nodes created:")

        node_types = [k for k in sorted(self.stats) if k.startswith("nodes_")]
        for k in node_types:
            label = k.replace("nodes_", "")
            print(f"    {label:<20s} {self.stats[k]:>6,d}")

        print(f"\n  Relationships created:")
        rel_types = [k for k in sorted(self.stats) if k.startswith("rels_")]
        for k in rel_types:
            label = k.replace("rels_", "")
            print(f"    {label:<20s} {self.stats[k]:>6,d}")

        if self.describe_images:
            print(f"\n  Images:")
            print(f"    Described (LLM)  : {self.stats.get('images_described', 0)}")
            print(f"    From cache       : {self.stats.get('images_cached', 0)}")
            print(f"    Failed           : {self.stats.get('images_failed', 0)}")

        print("=" * 60 + "\n")
