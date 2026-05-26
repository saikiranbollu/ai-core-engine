#!/usr/bin/env python3
"""
Datasheet Pin Mux Knowledge Graph Builder
==========================================

Ingests parsed pin mux CSV data (from table_parser.py / Camelot) into Neo4j.

Node Types Created:
    - DS_Device        — BGA package variant (e.g., BGA436_COM)
    - DS_Port          — Port group within a device (e.g., port 00)
    - DS_Pin           — Physical ball on the BGA package
    - DS_PinFunction   — One mux mode of a pin (signal + ctrl)

Relationships Created:
    - DS_HAS_FUNCTION       (DS_Pin → DS_PinFunction)
    - DS_BELONGS_TO_PORT    (DS_Pin → DS_Port)
    - DS_BELONGS_TO_DEVICE  (DS_Port → DS_Device)
    - DS_USES_PERIPHERAL    (DS_PinFunction → MCALModule)
    - DS_VARIANT_OF         (DS_Device → CM_DeviceVariant)

Usage::

    # CLI (standalone)
    python datasheet_kg_builder.py temp/datasheet/BGA436_COM_pin_mux.csv \\
        --device BGA436_COM --module PORT --profile mcal

    # From pipeline (run_pipeline.py step 10)
    python datasheet_kg_builder.py <csv_path> \\
        --device <device_name> --module PORT --profile test --project A3G
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

logger = logging.getLogger("datasheet_kg_builder")

# ── Peripheral mapping: symbol prefix → MCAL module name ─────────────────────
# Order matters: longer prefixes checked first via sorted(key=len, reverse=True)
PERIPHERAL_MAP: list[tuple[str, str]] = [
    ("ADC",     "ADC"),
    ("CAN",     "CAN"),
    ("QSPI",    "SPI"),
    ("ASCLIN",  "LIN"),
    ("SENT",    "SENT"),
    ("EGTM",    "GTM"),
    ("GTM",     "GTM"),
    ("I2C",     "I2C"),
    ("MSC",     "MSC"),
    ("PSI5",    "PSI5"),
    ("GETH",    "ETH"),
    ("CCU",     "CCU"),
    ("GPT",     "GPT"),
    ("STM",     "STM"),
    ("DMA",     "DMA"),
    ("HSCT",    "HSCT"),
    ("HSSL",    "HSSL"),
    ("SMU",     "SMU"),
    ("SCR",     "SCR"),
    ("EVADC",   "ADC"),
    ("EDSADC",  "ADC"),
    ("DSADC",   "ADC"),
    ("HSM",     "HSM"),
    ("FLEXRAY", "FR"),
    ("FR",      "FR"),
    ("SDMMC",   "SDMMC"),
    ("EMEM",    "EMEM"),
    ("SCI",     "SCI"),
]
# Sort longest prefix first for greedy matching
PERIPHERAL_MAP.sort(key=lambda x: len(x[0]), reverse=True)


def infer_peripheral(symbol: str) -> Optional[str]:
    """Infer MCAL peripheral module from pin function symbol name.

    Returns None for generic port pins (P00.0) and unmapped signals.
    """
    sym_upper = symbol.upper().strip()
    # Skip generic port pin symbols (Pxx.y)
    if re.match(r"^P\d{2}\.\d+$", sym_upper):
        return None
    for prefix, module in PERIPHERAL_MAP:
        if sym_upper.startswith(prefix):
            return module
    return None


def infer_direction(ctrl: str) -> str:
    """Derive signal direction from the Ctrl column value."""
    c = ctrl.strip().upper()
    if c == "I":
        return "input"
    if c == "AI":
        return "analog"
    if c.startswith("O"):
        return "output"
    return "bidirectional"


# ── CSV Parser ────────────────────────────────────────────────────────────────

# Title row pattern: "Page N - Table M - Title: 3.2.x DEVICE port NN ..."
_TITLE_RE = re.compile(
    r"Page\s+\d+\s*-\s*Table\s+\d+\s*-\s*Title:\s*"
    r"[\d.]+\s+\S+\s+(port\s+\d+|analog|special|system|supply)"
    r"(?:\s+\(continued\))?"
    r"\s*\(",
    re.IGNORECASE,
)

_PORT_RE = re.compile(r"(port\s+\d+|analog|special|system|supply)", re.IGNORECASE)

# Fallback: detect title rows that don't match the standard pattern
# e.g. "Page 28 - Table 1 - Title: 3.2.1 BGA224_RDR Port 00 (...)"
_TITLE_FALLBACK_RE = re.compile(
    r"Page\s+\d+\s*-\s*Table\s+\d+\s*-\s*Title:",
    re.IGNORECASE,
)


def parse_pin_mux_csv(csv_path: Path, device: str) -> dict:
    """Parse a pin mux CSV into structured data for KG ingestion.

    Handles multiple CSV formats gracefully:
      - Standard format: 5-column rows (Ball, Symbol, Ctrl, Buffer type, Function)
      - Missing columns: falls back to available data
      - Unknown title formats: uses "unknown" port as fallback
      - Empty/malformed rows: silently skipped

    Returns::
        {
            "device": "BGA436_COM",
            "ports": [{"name": "port 00", "port_number": "00"}, ...],
            "pins": [{"ball": "P4", "port": "port 00", "buffer_type": "..."}, ...],
            "functions": [{"ball": "P4", "symbol": "P00.0", "ctrl": "I", ...}, ...],
            "warnings": ["list of any parse warnings"],
        }
    """
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        logger.warning("Empty CSV: %s", csv_path)
        return {"device": device, "ports": [], "pins": [], "functions": [], "warnings": ["Empty CSV"]}

    ports_seen: dict[str, dict] = {}   # port_name → port info
    pins_seen: dict[str, dict] = {}    # ball → pin info
    functions: list[dict] = []
    warnings: list[str] = []
    current_port: str = "unknown"
    skipped_rows = 0
    header_detected = False

    for row_idx, row in enumerate(rows):
        # Single-cell metadata/title rows
        if len(row) == 1:
            cell = row[0]
            # Try standard port regex
            m = _PORT_RE.search(cell)
            if m and "continued" not in cell.lower():
                port_raw = m.group(1).strip().lower()
                current_port = port_raw
                # Extract port number
                num_m = re.search(r"\d+", port_raw)
                port_number = num_m.group(0) if num_m else port_raw
                if current_port not in ports_seen:
                    ports_seen[current_port] = {
                        "name": current_port,
                        "port_number": port_number,
                    }
            elif _TITLE_FALLBACK_RE.match(cell) and "continued" not in cell.lower():
                # Title row that doesn't match known port pattern
                # Try to extract anything useful
                port_match = re.search(r"[Pp]ort\s*(\d+)", cell)
                if port_match:
                    port_number = port_match.group(1)
                    current_port = f"port {port_number}"
                    if current_port not in ports_seen:
                        ports_seen[current_port] = {
                            "name": current_port,
                            "port_number": port_number,
                        }
                # else: keep current_port as-is (may be "unknown")
            continue

        # Detect header rows (Ball, Symbol, Ctrl, ...)
        if len(row) >= 3 and row[0].strip().lower() in ("ball", "pin"):
            header_detected = True
            continue

        # Skip rows with fewer than 3 columns (minimum: ball, symbol, ctrl)
        if len(row) < 3:
            skipped_rows += 1
            continue

        # Extract fields — handle both 5-col and shorter formats
        ball = row[0].strip()
        symbol = row[1].strip()
        ctrl = row[2].strip() if len(row) > 2 else ""
        buffer_type = row[3].strip().replace("\n", " / ") if len(row) > 3 else ""
        function_desc = row[4].strip().replace("\n", " ") if len(row) > 4 else ""

        # Skip empty data rows
        if not symbol or not ctrl:
            skipped_rows += 1
            continue

        # Track pin (by ball)
        if ball and ball not in pins_seen:
            pins_seen[ball] = {
                "ball": ball,
                "port": current_port,
                "buffer_type": buffer_type,
                "port_pin": symbol if re.match(r"^P\d{2}\.\d+$", symbol.upper()) else None,
            }

        # Use last-known ball for forward-filled rows
        effective_ball = ball if ball else None
        if not effective_ball:
            # This shouldn't happen after forward-fill, but be defensive
            skipped_rows += 1
            continue

        # Track function
        peripheral = infer_peripheral(symbol)
        functions.append({
            "ball": effective_ball,
            "symbol": symbol,
            "ctrl": ctrl,
            "direction": infer_direction(ctrl),
            "function_desc": function_desc,
            "peripheral": peripheral,
        })

    # Post-parse validation and warnings
    if not header_detected:
        warnings.append("No header row detected — CSV may have unexpected format")
    if not ports_seen:
        # If no port sections detected, create a single "unknown" port
        ports_seen["unknown"] = {"name": "unknown", "port_number": "unknown"}
        warnings.append("No port section titles found — all pins assigned to 'unknown' port")
    if skipped_rows > 0:
        warnings.append(f"Skipped {skipped_rows} malformed/empty rows")
    if current_port == "unknown" and functions:
        warnings.append("Some or all pins assigned to 'unknown' port (title parsing issue)")

    return {
        "device": device,
        "ports": list(ports_seen.values()),
        "pins": list(pins_seen.values()),
        "functions": functions,
        "warnings": warnings,
    }


# ── KG Builder ────────────────────────────────────────────────────────────────

class DatasheetKnowledgeGraphBuilder:
    """Builds Neo4j knowledge graph from parsed datasheet pin mux CSV."""

    BATCH_SIZE = 500

    def __init__(
        self,
        neo4j_cfg: dict,
        csv_path: str | Path,
        device: str,
        module: str = "PORT",
        *,
        project: str = "A3G",
        dry_run: bool = False,
        clear_device: bool = False,
    ):
        self.neo4j_cfg = neo4j_cfg
        self.csv_path = Path(csv_path)
        self.device = device.upper()
        self.module = module.upper()
        self.project = project
        self.dry_run = dry_run
        self.clear_device = clear_device
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

    # -- Build Pipeline -----------------------------------------------------

    def build(self):
        """Main entry point — parse CSV and ingest into Neo4j."""
        logger.info("=" * 60)
        logger.info("Datasheet Pin Mux KG Builder")
        logger.info("  Device: %s | Module: %s | Project: %s",
                    self.device, self.module, self.project)
        logger.info("  Source: %s", self.csv_path)
        logger.info("  Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        start = time.perf_counter()

        # Parse CSV
        data = parse_pin_mux_csv(self.csv_path, self.device)
        logger.info("Parsed: %d ports, %d pins, %d functions",
                    len(data["ports"]), len(data["pins"]), len(data["functions"]))

        # Log any parse warnings
        for warn in data.get("warnings", []):
            logger.warning("  PARSE WARNING: %s", warn)

        if not data["pins"]:
            logger.warning("No pin data found in %s — skipping ingestion.", self.csv_path)
            return

        # Connect
        if not self.dry_run:
            self._connect()

        try:
            if self.clear_device and not self.dry_run:
                self._clear_device_data()

            self._ensure_constraints()

            # 1. DS_Device node
            self._ingest_device(data)

            # 2. DS_Port nodes
            self._ingest_ports(data)

            # 3. DS_Pin nodes + DS_BELONGS_TO_PORT relationships
            self._ingest_pins(data)

            # 4. DS_PinFunction nodes + DS_HAS_FUNCTION relationships
            self._ingest_functions(data)

            # 5. DS_BELONGS_TO_DEVICE relationships (Port → Device)
            self._create_port_device_rels(data)

            # 6. DS_USES_PERIPHERAL relationships (PinFunction → MCALModule)
            self._create_peripheral_rels(data)

            # 7. DS_VARIANT_OF relationship (Device → CM_DeviceVariant)
            self._create_variant_of_rel()

            # Report
            elapsed = time.perf_counter() - start
            self._report_stats(elapsed)

        finally:
            self._close()

    # -- Clear existing data ------------------------------------------------

    def _clear_device_data(self):
        """Remove existing DS nodes for this device."""
        logger.info("Clearing existing DS data for device=%s …", self.device)

        # Delete relationships from DS_PinFunction
        self._write_tx("""
            MATCH (f:DS_PinFunction {device: $device})
            DETACH DELETE f
        """, {"device": self.device})

        # Delete DS_Pin nodes
        self._write_tx("""
            MATCH (p:DS_Pin {device: $device})
            DETACH DELETE p
        """, {"device": self.device})

        # Delete DS_Port nodes
        self._write_tx("""
            MATCH (pt:DS_Port {device: $device})
            DETACH DELETE pt
        """, {"device": self.device})

        # Delete DS_Device node
        self._write_tx("""
            MATCH (d:DS_Device {name: $device})
            DETACH DELETE d
        """, {"device": self.device})

        logger.info("Cleared existing DS data for %s.", self.device)

    # -- Constraints --------------------------------------------------------

    def _ensure_constraints(self):
        """Create uniqueness constraints for DS nodes."""
        constraints = [
            ("ds_device_uid", "DS_Device", "uid"),
            ("ds_port_uid", "DS_Port", "uid"),
            ("ds_pin_uid", "DS_Pin", "uid"),
            ("ds_pinfunc_uid", "DS_PinFunction", "uid"),
        ]
        for name, label, prop in constraints:
            cypher = (
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            self._write_tx(cypher)

    # -- DS_Device ----------------------------------------------------------

    def _ingest_device(self, data: dict):
        """Create the DS_Device node."""
        uid = f"DS_DEV_{self.device}"
        cypher = """
        MERGE (d:DS_Device {uid: $uid})
        SET d.name = $name,
            d.datasheet = $datasheet,
            d.pin_count = $pin_count,
            d.project = $project
        """
        params = {
            "uid": uid,
            "name": self.device,
            "datasheet": self.csv_path.name,
            "pin_count": len(data["pins"]),
            "project": self.project,
        }
        self._write_tx(cypher, params)
        self.stats["DS_Device"] += 1
        logger.info("  DS_Device: %s (%d pins)", self.device, len(data["pins"]))

    # -- DS_Port ------------------------------------------------------------

    def _ingest_ports(self, data: dict):
        """Create DS_Port nodes."""
        ports = data["ports"]
        if not ports:
            return

        cypher = """
        UNWIND $batch AS p
        MERGE (pt:DS_Port {uid: p.uid})
        SET pt.name = p.name,
            pt.port_number = p.port_number,
            pt.device = $device
        """
        batch = []
        for port in ports:
            uid = f"DS_PORT_{self.device}_{port['port_number']}"
            batch.append({
                "uid": uid,
                "name": port["name"],
                "port_number": port["port_number"],
            })

        self._write_tx(cypher, {"batch": batch, "device": self.device})
        self.stats["DS_Port"] += len(batch)
        logger.info("  DS_Port nodes: %d", len(batch))

    # -- DS_Pin + DS_BELONGS_TO_PORT ----------------------------------------

    def _ingest_pins(self, data: dict):
        """Create DS_Pin nodes and DS_BELONGS_TO_PORT relationships."""
        pins = data["pins"]
        if not pins:
            return

        cypher = """
        UNWIND $batch AS pin
        MERGE (p:DS_Pin {uid: pin.uid})
        SET p.ball = pin.ball,
            p.port_pin = pin.port_pin,
            p.device = $device,
            p.buffer_type = pin.buffer_type
        WITH p, pin
        MATCH (pt:DS_Port {uid: pin.port_uid})
        MERGE (p)-[:DS_BELONGS_TO_PORT]->(pt)
        """

        for i in range(0, len(pins), self.BATCH_SIZE):
            batch = []
            for pin in pins[i:i + self.BATCH_SIZE]:
                port_number = "unknown"
                # Look up port number from port name
                for port in data["ports"]:
                    if port["name"] == pin["port"]:
                        port_number = port["port_number"]
                        break
                uid = f"DS_PIN_{self.device}_{pin['ball']}"
                port_uid = f"DS_PORT_{self.device}_{port_number}"
                batch.append({
                    "uid": uid,
                    "ball": pin["ball"],
                    "port_pin": pin.get("port_pin"),
                    "buffer_type": pin.get("buffer_type", ""),
                    "port_uid": port_uid,
                })
            self._write_tx(cypher, {"batch": batch, "device": self.device})
            self.stats["DS_Pin"] += len(batch)

        self.stats["DS_BELONGS_TO_PORT"] += len(pins)
        logger.info("  DS_Pin nodes: %d (+ DS_BELONGS_TO_PORT rels)", len(pins))

    # -- DS_PinFunction + DS_HAS_FUNCTION -----------------------------------

    def _ingest_functions(self, data: dict):
        """Create DS_PinFunction nodes and DS_HAS_FUNCTION relationships."""
        functions = data["functions"]
        if not functions:
            return

        cypher = """
        UNWIND $batch AS fn
        MERGE (f:DS_PinFunction {uid: fn.uid})
        SET f.symbol = fn.symbol,
            f.ctrl = fn.ctrl,
            f.direction = fn.direction,
            f.function_desc = fn.function_desc,
            f.device = $device,
            f.ball = fn.ball,
            f.peripheral = fn.peripheral
        WITH f, fn
        MATCH (p:DS_Pin {uid: fn.pin_uid})
        MERGE (p)-[:DS_HAS_FUNCTION]->(f)
        """

        for i in range(0, len(functions), self.BATCH_SIZE):
            batch = []
            for fn in functions[i:i + self.BATCH_SIZE]:
                # UID: device + ball + ctrl + symbol (unique mux entry)
                safe_symbol = re.sub(r"[^A-Za-z0-9_]", "_", fn["symbol"])
                uid = f"DS_FUNC_{self.device}_{fn['ball']}_{fn['ctrl']}_{safe_symbol}"
                pin_uid = f"DS_PIN_{self.device}_{fn['ball']}"
                batch.append({
                    "uid": uid,
                    "symbol": fn["symbol"],
                    "ctrl": fn["ctrl"],
                    "direction": fn["direction"],
                    "function_desc": fn.get("function_desc", ""),
                    "ball": fn["ball"],
                    "peripheral": fn.get("peripheral"),
                    "pin_uid": pin_uid,
                })
            self._write_tx(cypher, {"batch": batch, "device": self.device})
            self.stats["DS_PinFunction"] += len(batch)

        self.stats["DS_HAS_FUNCTION"] += len(functions)
        logger.info("  DS_PinFunction nodes: %d (+ DS_HAS_FUNCTION rels)", len(functions))

    # -- DS_BELONGS_TO_DEVICE (Port → Device) --------------------------------

    def _create_port_device_rels(self, data: dict):
        """Create DS_BELONGS_TO_DEVICE relationships from ports to device."""
        device_uid = f"DS_DEV_{self.device}"
        cypher = """
        MATCH (pt:DS_Port {device: $device})
        MATCH (d:DS_Device {uid: $device_uid})
        MERGE (pt)-[:DS_BELONGS_TO_DEVICE]->(d)
        """
        self._write_tx(cypher, {"device": self.device, "device_uid": device_uid})
        self.stats["DS_BELONGS_TO_DEVICE"] += len(data["ports"])
        logger.info("  DS_BELONGS_TO_DEVICE rels: %d", len(data["ports"]))

    # -- DS_USES_PERIPHERAL (PinFunction → MCALModule) ----------------------

    def _create_peripheral_rels(self, data: dict):
        """Create DS_USES_PERIPHERAL relationships from functions to MCALModule."""
        # Collect unique peripherals that have functions
        peripheral_symbols: dict[str, list[str]] = {}  # peripheral → list of function UIDs
        for fn in data["functions"]:
            if fn.get("peripheral"):
                peripheral_symbols.setdefault(fn["peripheral"], [])

        if not peripheral_symbols:
            logger.info("  No peripheral mappings found — skipping DS_USES_PERIPHERAL.")
            return

        # Link all DS_PinFunction nodes with a peripheral to MCALModule
        cypher = """
        MATCH (f:DS_PinFunction {device: $device})
        WHERE f.peripheral = $peripheral
        MATCH (m:MCALModule {name: $peripheral})
        MERGE (f)-[:DS_USES_PERIPHERAL]->(m)
        RETURN count(*) AS cnt
        """

        total = 0
        for peripheral in sorted(peripheral_symbols.keys()):
            if self.dry_run:
                logger.info("  [DRY] DS_USES_PERIPHERAL: %s", peripheral)
                continue
            # Execute and check if MCALModule exists
            db = self.neo4j_cfg["database"]
            with self._driver.session(database=db) as session:
                result = session.run(cypher, {
                    "device": self.device,
                    "peripheral": peripheral,
                })
                record = result.single()
                cnt = record["cnt"] if record else 0
                if cnt > 0:
                    total += cnt
                    logger.debug("  DS_USES_PERIPHERAL: %s → %d functions linked",
                                 peripheral, cnt)
                else:
                    logger.debug("  DS_USES_PERIPHERAL: %s — no MCALModule found (skipped)",
                                 peripheral)

        self.stats["DS_USES_PERIPHERAL"] += total
        logger.info("  DS_USES_PERIPHERAL rels: %d (across %d peripherals)",
                    total, len(peripheral_symbols))

    # -- DS_VARIANT_OF (Device → CM_DeviceVariant) --------------------------

    def _create_variant_of_rel(self):
        """Link DS_Device to matching CM_DeviceVariant if one exists."""
        device_uid = f"DS_DEV_{self.device}"
        # Try to match by name substring (e.g. BGA436_COM → TC4D9_COM has "COM")
        # or exact device variant name match
        cypher = """
        MATCH (ds:DS_Device {uid: $device_uid})
        MATCH (cv:CM_DeviceVariant)
        WHERE cv.name CONTAINS $variant_hint
        MERGE (ds)-[:DS_VARIANT_OF]->(cv)
        RETURN count(*) AS cnt
        """
        # Extract variant hint: BGA436_COM → "COM", BGA292_STD → "STD"
        parts = self.device.split("_")
        variant_hint = parts[-1] if len(parts) > 1 else self.device

        if self.dry_run:
            logger.info("  [DRY] DS_VARIANT_OF: %s → CM_DeviceVariant (hint=%s)",
                        self.device, variant_hint)
            return

        db = self.neo4j_cfg["database"]
        with self._driver.session(database=db) as session:
            result = session.run(cypher, {
                "device_uid": device_uid,
                "variant_hint": variant_hint,
            })
            record = result.single()
            cnt = record["cnt"] if record else 0
            if cnt > 0:
                self.stats["DS_VARIANT_OF"] += cnt
                logger.info("  DS_VARIANT_OF: %s → %d CM_DeviceVariant(s) linked",
                            self.device, cnt)
            else:
                logger.info("  DS_VARIANT_OF: no matching CM_DeviceVariant for %s (hint=%s)",
                            self.device, variant_hint)

    # -- Stats & Reporting --------------------------------------------------

    def _report_stats(self, elapsed: float):
        """Print final statistics."""
        logger.info("=" * 60)
        logger.info("Datasheet KG Build Complete — %s (%.1fs)", self.device, elapsed)
        for key, val in sorted(self.stats.items()):
            logger.info("  %-30s %d", key, val)
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from env_config import load_yaml_with_env

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Ingest datasheet pin mux CSV into Neo4j KG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python datasheet_kg_builder.py BGA436_COM_pin_mux.csv "
            "--device BGA436_COM --profile mcal\n"
            "  python datasheet_kg_builder.py BGA292_STD.csv "
            "--device BGA292_STD --profile test --clear\n"
        ),
    )
    parser.add_argument("csv", help="Path to pin mux CSV file")
    parser.add_argument("--device", required=True,
                        help="Device/package name (e.g., BGA436_COM)")
    parser.add_argument("--module", default="PORT",
                        help="Module name for project tagging (default: PORT)")
    parser.add_argument("--project", default="A3G",
                        help="Project tag (default: A3G)")
    parser.add_argument("--profile", default="mcal",
                        help="Storage config profile (default: mcal)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse only, no Neo4j writes")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing DS data for this device first")
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

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    builder = DatasheetKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        csv_path=csv_path,
        device=args.device,
        module=args.module,
        project=args.project,
        dry_run=args.dry_run,
        clear_device=args.clear,
    )
    builder.build()


if __name__ == "__main__":
    main()
