#!/usr/bin/env python3
"""
Polarion PRQ + SHRQ Requirement Fetch
======================================
Fetches Product Requirements (PRQ) and Stakeholder Requirements (SHRQ)
from a Polarion project via SOAP API.  The output JSON is compatible
with the ``build_knowledge_graph.py`` ingestion pipeline.

For a given module (e.g. GPT), this script:
  1.  Queries all ``ifxProductRequirement`` items whose title starts with
      the module prefix (e.g. "Gpt:").
  2.  For each PRQ, fetches custom fields (ASIL, verification, etc.)
      and linked work items (ifxRefines → SHRQ links).
  3.  Follows ``ifxRefines`` links to collect referenced SHRQ items.
  4.  Saves everything to ``jama-req/polarion_<module>_combined_requirements.json``.

The SHRQ items are discovered *through traceability links* on the PRQ items,
mirroring the Jama fetch approach.

Usage:
  python fetch_polarion_requirements.py --module GPT
  python fetch_polarion_requirements.py --module GPT --force
  python fetch_polarion_requirements.py --module GPT --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent            # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                            # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                         # .../HybridRAG
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from env_config import load_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fetch_polarion_requirements")

# ---------------------------------------------------------------------------
# XML namespaces used in Polarion SOAP responses
# ---------------------------------------------------------------------------
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_SESSION = "http://ws.polarion.com/session"
NS_TRACKER = "http://ws.polarion.com/TrackerWebService"
NS_TRACKER_TYPES = "http://ws.polarion.com/TrackerWebService-types"
NS_TYPES = "http://ws.polarion.com/types"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# Register namespaces so ET doesn't mangle them
ET.register_namespace("soapenv", NS_SOAP)
ET.register_namespace("trac", NS_TRACKER)
ET.register_namespace("session", NS_SESSION)

# Tag helpers
def _trac(tag: str) -> str:
    return f"{{{NS_TRACKER_TYPES}}}{tag}"

def _types(tag: str) -> str:
    return f"{{{NS_TYPES}}}{tag}"

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")

def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    if not text:
        return ""
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MULTI_WS_RE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Polarion SOAP Client (minimal, no zeep dependency)
# ---------------------------------------------------------------------------
class PolarionSoapClient:
    """Lightweight SOAP client for Polarion TrackerWebService."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        project_id: str,
        verify_ssl: bool = False,
    ):
        self.server_url = server_url.rstrip("/")
        self.project_id = project_id
        self._session = requests.Session()
        self._session.verify = verify_ssl
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._session_url = f"{self.server_url}/polarion/ws/services/SessionWebService"
        self._tracker_url = f"{self.server_url}/polarion/ws/services/TrackerWebService"
        self._session_id: Optional[str] = None

        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        soap = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:ses="http://ws.polarion.com/SessionWebService">'
            "<soapenv:Body><ses:logIn>"
            f"<ses:userName>{_xml_escape(username)}</ses:userName>"
            f"<ses:password>{_xml_escape(password)}</ses:password>"
            "</ses:logIn></soapenv:Body></soapenv:Envelope>"
        )
        r = self._session.post(
            self._session_url,
            data=soap.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "logIn"},
            timeout=30,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)
        sid_el = root.find(f".//{{{NS_SESSION}}}sessionID")
        if sid_el is None or not sid_el.text:
            raise RuntimeError(f"Polarion SOAP login failed. Response: {r.text[:500]}")
        self._session_id = sid_el.text
        logger.info("Logged in to Polarion at %s (session %s)", self.server_url, self._session_id[:8] + "...")

    def _tracker_call(self, action: str, body_xml: str) -> ET.Element:
        """Send a SOAP request to TrackerWebService and return parsed XML root."""
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:trac="http://ws.polarion.com/TrackerWebService"'
            ' xmlns:ns1="http://ws.polarion.com/session">'
            f"<soapenv:Header><ns1:sessionID>{self._session_id}</ns1:sessionID></soapenv:Header>"
            f"<soapenv:Body>{body_xml}</soapenv:Body>"
            "</soapenv:Envelope>"
        )
        r = self._session.post(
            self._tracker_url,
            data=envelope.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action},
            timeout=120,
        )
        r.raise_for_status()
        return ET.fromstring(r.text)

    def query_workitems(
        self,
        lucene_query: str,
        sort: str = "id",
        fields: Optional[List[str]] = None,
    ) -> List[ET.Element]:
        """Execute a Lucene query and return work item XML elements."""
        if fields is None:
            fields = ["id", "title", "type", "status", "outlineNumber",
                       "description", "created", "updated", "linkedWorkItems"]

        fields_xml = "".join(f"<trac:fields>{f}</trac:fields>" for f in fields)
        body = (
            "<trac:queryWorkItems>"
            f"<trac:query>{_xml_escape(lucene_query)}</trac:query>"
            f"<trac:sort>{_xml_escape(sort)}</trac:sort>"
            f"{fields_xml}"
            "</trac:queryWorkItems>"
        )
        root = self._tracker_call("queryWorkItems", body)
        # Work items are <queryWorkItemsReturn> elements
        return root.findall(f".//{{{NS_TRACKER}}}queryWorkItemsReturn") or []

    def get_workitem_by_uri(self, uri: str) -> Optional[ET.Element]:
        """Fetch a single work item with ALL fields (including custom fields)."""
        body = (
            "<trac:getWorkItemByUri>"
            f"<trac:workitemUri>{_xml_escape(uri)}</trac:workitemUri>"
            "</trac:getWorkItemByUri>"
        )
        root = self._tracker_call("getWorkItemByUri", body)
        return root.find(f".//{{{NS_TRACKER}}}getWorkItemByUriReturn")

    def make_uri(self, workitem_id: str) -> str:
        """Build a subterra URI from a work item ID."""
        return f"subterra:data-service:objects:/default/{self.project_id}${{WorkItem}}{workitem_id}"

    def close(self) -> None:
        """End the SOAP session."""
        try:
            soap = (
                '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
                ' xmlns:ses="http://ws.polarion.com/SessionWebService"'
                ' xmlns:ns1="http://ws.polarion.com/session">'
                f"<soapenv:Header><ns1:sessionID>{self._session_id}</ns1:sessionID></soapenv:Header>"
                "<soapenv:Body><ses:endSession/></soapenv:Body></soapenv:Envelope>"
            )
            self._session.post(
                self._session_url, data=soap.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "endSession"},
                timeout=10,
            )
        except Exception:
            pass


def _xml_escape(text: str) -> str:
    """Escape special XML characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


# ---------------------------------------------------------------------------
# Work item XML → dict parsers
# ---------------------------------------------------------------------------

def _find_text(el: ET.Element, tag_local: str) -> str:
    """Find text in a child element, searching across common Polarion namespaces."""
    for ns in [NS_TRACKER_TYPES, NS_TRACKER]:
        child = el.find(f"{{{ns}}}{tag_local}")
        if child is not None and child.text:
            return child.text.strip()
    # Fallback: no namespace
    child = el.find(tag_local)
    return child.text.strip() if child is not None and child.text else ""


def _find_enum_id(el: ET.Element, tag_local: str) -> str:
    """Find an EnumOptionId value (e.g. status, type)."""
    for ns in [NS_TRACKER_TYPES, NS_TRACKER]:
        parent = el.find(f"{{{ns}}}{tag_local}")
        if parent is not None:
            id_el = parent.find(f"{{{ns}}}id")
            if id_el is not None and id_el.text:
                return id_el.text.strip()
    return ""


def _find_description(el: ET.Element) -> str:
    """Extract description content from a Text element."""
    for ns in [NS_TRACKER_TYPES, NS_TRACKER]:
        desc = el.find(f"{{{ns}}}description")
        if desc is not None:
            content = desc.find(f"{{{NS_TYPES}}}content")
            if content is not None and content.text:
                return content.text
    return ""


def _parse_linked_workitems(el: ET.Element) -> List[Dict[str, Any]]:
    """Extract linked work items from the SOAP response."""
    links = []
    for ns in [NS_TRACKER_TYPES, NS_TRACKER]:
        for lwi in el.findall(f".//{{{ns}}}LinkedWorkItem"):
            role_el = lwi.find(f"{{{ns}}}role")
            role_id = ""
            if role_el is not None:
                rid = role_el.find(f"{{{ns}}}id")
                role_id = rid.text.strip() if rid is not None and rid.text else ""

            suspect_el = lwi.find(f"{{{ns}}}suspect")
            suspect = suspect_el.text.strip().lower() == "true" if suspect_el is not None and suspect_el.text else False

            uri_el = lwi.find(f"{{{ns}}}workItemURI")
            uri = uri_el.text.strip() if uri_el is not None and uri_el.text else ""

            # Extract ID from URI: ...${WorkItem}AURI-8131
            target_id = ""
            if uri and "WorkItem}" in uri:
                target_id = uri.split("WorkItem}")[-1]

            if role_id and target_id:
                links.append({
                    "role": role_id,
                    "target_id": target_id,
                    "target_uri": uri,
                    "suspect": suspect,
                })
    return links


def _parse_custom_fields(el: ET.Element) -> Dict[str, Any]:
    """Extract custom fields from a getWorkItemByUri response."""
    customs = {}
    for ns in [NS_TRACKER_TYPES, NS_TRACKER]:
        for custom in el.findall(f".//{{{ns}}}Custom"):
            key_el = custom.find(f"{{{ns}}}key")
            if key_el is None or not key_el.text:
                continue
            key = key_el.text.strip()

            value_el = custom.find(f"{{{ns}}}value")
            if value_el is None:
                customs[key] = None
                continue

            # Determine value type from xsi:type attribute
            xsi_type = value_el.get(f"{{{NS_XSI}}}type", "")

            if "EnumOptionId" in xsi_type:
                # Enum value
                id_el = value_el.find(f"{{{ns}}}id")
                customs[key] = id_el.text.strip() if id_el is not None and id_el.text else None
            elif "ArrayOfEnumOptionId" in xsi_type:
                # Array of enum values (e.g., ifxComponent)
                ids = []
                for enum_el in value_el.findall(f".//{{{ns}}}EnumOptionId"):
                    id_el = enum_el.find(f"{{{ns}}}id")
                    if id_el is not None and id_el.text:
                        ids.append(id_el.text.strip())
                customs[key] = ids[0] if len(ids) == 1 else ids
            elif "Text" in xsi_type:
                # Rich text
                content_el = value_el.find(f"{{{NS_TYPES}}}content")
                customs[key] = content_el.text if content_el is not None and content_el.text else None
            elif "boolean" in xsi_type.lower():
                customs[key] = value_el.text.strip().lower() == "true" if value_el.text else False
            else:
                # Simple string or other type
                customs[key] = value_el.text.strip() if value_el.text else None
    return customs


def _parse_basic_workitem(el: ET.Element) -> Dict[str, Any]:
    """Parse a work item element from queryWorkItems response."""
    uri = el.get("uri", "")
    wi_id = _find_text(el, "id")
    return {
        "id": wi_id,
        "uri": uri,
        "title": _find_text(el, "title"),
        "type": _find_enum_id(el, "type"),
        "status": _find_enum_id(el, "status"),
        "outline_number": _find_text(el, "outlineNumber"),
        "description_html": _find_description(el),
        "description": strip_html(_find_description(el)),
        "created": _find_text(el, "created"),
        "updated": _find_text(el, "updated"),
        "linked_workitems": _parse_linked_workitems(el),
    }


# ---------------------------------------------------------------------------
# Module discovery — maps module name to known PRQ document IDs
# ---------------------------------------------------------------------------
# Naming convention: AURIX_RC1_SW_MCAL_PRQ_<Module>
PRQ_DOC_PREFIX = "AURIX_RC1_SW_MCAL_PRQ_"
PRQ_SPACE = "Product Requirements"

# Module name → document ID suffix (most follow the pattern directly)
MODULE_DOC_MAP = {
    "ADC": "ADC",
    "BFX": "Bfx",
    "BMC": "Bmc",
    "CAN": "Can_17_XsCan",
    "CCD": "Ccd",
    "CRC": "Crc",
    "DIO": "Dio",
    "DMA": "Dma",
    "ENCODER": "Encoder",
    "FEE": "Fee",
    "GPT": "Gpt",
    "I2C": "I2c",
    "ICU": "Icu",
    "LIN": "Lin_17_MxLin",
    "MCU": "Mcu",
    "MEMACC": "MemAcc",
    "MEM_NVM": "Mem_17_Nvm",
    "OCU": "Ocu",
    "PORT": "Port",
    "PWM": "Pwm_17_TimerIp",
    "RVLIB": "RvLib",
    "SENT": "SENT",
    "SPI": "Spi",
    "TINFRA": "TInfra",
    "UART": "UART",
    "WDG": "Wdg_17_AvWdt",
    "XSPI": "XSpi",
    "GENERICTYPES": "GenericTypes",
    "GENERAL": "General",
    "GENERAL_CS": "General_CS",
    "GENERAL_FS": "General_FS",
}


def _get_prq_doc_id(module: str) -> str:
    """Return the PRQ document ID for a module."""
    suffix = MODULE_DOC_MAP.get(module.upper())
    if suffix:
        return f"{PRQ_DOC_PREFIX}{suffix}"
    # Fallback: capitalize first letter
    return f"{PRQ_DOC_PREFIX}{module.capitalize()}"


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------

def fetch_module_requirements(
    module: str,
    *,
    server_url: str,
    username: str,
    password: str,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
) -> Optional[Path]:
    """Fetch PRQ + linked SHRQ from Polarion and save to JSON.

    Returns the output file path, or None on failure.
    """
    module = module.upper()
    output_file = JAMA_REQ_DIR / f"polarion_{module.lower()}_combined_requirements.json"

    if output_file.exists() and not force:
        logger.info("Output already exists: %s (use --force to re-fetch)", output_file)
        return output_file

    prq_doc_id = _get_prq_doc_id(module)
    logger.info("Module: %s  |  PRQ doc: %s  |  Project: %s", module, prq_doc_id, project_id)

    if dry_run:
        logger.info("[DRY-RUN] Would fetch PRQ from doc '%s'", prq_doc_id)
        return None

    client = PolarionSoapClient(server_url, username, password, project_id)

    try:
        # -----------------------------------------------------------------
        # Step 1: Query all PRQ items from the module's PRQ document
        # -----------------------------------------------------------------
        # Use document.id with quoted "Space/DocName" for precise scoping.
        # This retrieves all work items in the document, filtered to PRQ type.
        doc_path = f"{PRQ_SPACE}/{prq_doc_id}"
        query = (
            f'project.id:{project_id} AND type:ifxProductRequirement'
            f' AND document.id:"{doc_path}"'
        )
        logger.info("Querying PRQ items: %s", query)
        t0 = time.perf_counter()
        prq_elements = client.query_workitems(query, sort="outlineNumber")
        elapsed = time.perf_counter() - t0
        logger.info("  Got %d PRQ items in %.1fs", len(prq_elements), elapsed)

        # Parse basic fields + linked work items
        prq_items: List[Dict[str, Any]] = []
        all_shrq_ids: Set[str] = set()

        for el in prq_elements:
            item = _parse_basic_workitem(el)
            item["item_type"] = "ifxProductRequirement"
            item["project_id"] = project_id
            item["source"] = "polarion"
            prq_items.append(item)

            # Collect SHRQ IDs from ifxRefines links
            for link in item["linked_workitems"]:
                if link["role"] == "ifxRefines":
                    all_shrq_ids.add(link["target_id"])

        logger.info("  Parsed %d PRQ items, found %d ifxRefines links to SHRQs",
                     len(prq_items), len(all_shrq_ids))

        # -----------------------------------------------------------------
        # Step 2: Fetch custom fields for each PRQ (via getWorkItemByUri)
        # -----------------------------------------------------------------
        logger.info("Fetching custom fields for %d PRQ items ...", len(prq_items))
        t0 = time.perf_counter()
        for i, item in enumerate(prq_items):
            if not item["uri"]:
                continue
            try:
                full_el = client.get_workitem_by_uri(item["uri"])
                if full_el is not None:
                    item["raw_fields"] = _parse_custom_fields(full_el)
                    # Also extract document_key (Jama cross-ref)
                    item["document_key"] = item["raw_fields"].get("ifxInfineonSystemId", "")
            except Exception as e:
                logger.warning("  Failed to fetch custom fields for %s: %s", item["id"], e)
                item["raw_fields"] = {}
                item["document_key"] = ""

            if (i + 1) % 50 == 0:
                logger.info("  ... %d / %d", i + 1, len(prq_items))

        elapsed = time.perf_counter() - t0
        logger.info("  Custom fields fetched in %.1fs", elapsed)

        # -----------------------------------------------------------------
        # Step 3: Fetch linked SHRQ items
        # -----------------------------------------------------------------
        shrq_items: List[Dict[str, Any]] = []
        if all_shrq_ids:
            logger.info("Fetching %d linked SHRQ items ...", len(all_shrq_ids))
            t0 = time.perf_counter()
            for i, shrq_id in enumerate(sorted(all_shrq_ids)):
                try:
                    uri = client.make_uri(shrq_id)
                    el = client.get_workitem_by_uri(uri)
                    if el is not None:
                        item = _parse_basic_workitem(el)
                        item["item_type"] = _find_enum_id(el, "type") or "ifxStakeholderRequirement"
                        item["project_id"] = project_id
                        item["source"] = "polarion"
                        item["raw_fields"] = _parse_custom_fields(el)
                        item["document_key"] = item["raw_fields"].get("ifxInfineonSystemId", "")
                        shrq_items.append(item)
                except Exception as e:
                    logger.warning("  Failed to fetch SHRQ %s: %s", shrq_id, e)

                if (i + 1) % 50 == 0:
                    logger.info("  ... %d / %d", i + 1, len(all_shrq_ids))

            elapsed = time.perf_counter() - t0
            logger.info("  Fetched %d SHRQ items in %.1fs", len(shrq_items), elapsed)

        # -----------------------------------------------------------------
        # Step 4: Combine and save
        # -----------------------------------------------------------------
        combined = prq_items + shrq_items
        prq_count = len(prq_items)
        shrq_count = len(shrq_items)

        logger.info("Total: %d items (PRQ: %d, SHRQ: %d)", len(combined), prq_count, shrq_count)

        JAMA_REQ_DIR.mkdir(parents=True, exist_ok=True)

        output = {
            "metadata": {
                "source": "polarion",
                "project_id": project_id,
                "server_url": server_url,
                "module": module,
                "prq_document_id": prq_doc_id,
                "prq_count": prq_count,
                "shrq_count": shrq_count,
                "total_count": len(combined),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "items": combined,
        }

        output_file.write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        logger.info("Saved to %s", output_file)
        return output_file

    finally:
        client.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch PRQ + SHRQ requirements from Polarion for a given MCAL module.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_polarion_requirements.py --module GPT\n"
            "  python fetch_polarion_requirements.py --module ADC --force\n"
            "  python fetch_polarion_requirements.py --module DMA --dry-run\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show what would be fetched, don't actually fetch.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if the output file already exists.")
    parser.add_argument("--server-url", default=None,
                        help="Polarion server URL (default: from POLARION_URL env var).")
    parser.add_argument("--project-id", default=None,
                        help="Polarion project ID (default: from POLARION_PROJECT env var).")
    args = parser.parse_args()

    load_env()

    server_url = args.server_url or os.environ.get("POLARION_URL", "https://alm-plr.intra.infineon.com")
    username = os.environ.get("POLARION_USERNAME") or os.environ.get("IFX_USERNAME", "")
    password = os.environ.get("POLARION_PASSWORD") or os.environ.get("IFX_PASSWORD", "")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT", "AURIX_RC1_MCAL")

    if not username or not password:
        logger.error("POLARION_USERNAME and POLARION_PASSWORD must be set in env/.env or environment.")
        return 1

    result = fetch_module_requirements(
        module=args.module,
        server_url=server_url,
        username=username,
        password=password,
        project_id=project_id,
        dry_run=args.dry_run,
        force=args.force,
    )
    return 0 if result is not None or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
