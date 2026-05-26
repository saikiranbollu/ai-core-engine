"""
ReqIF Parser for AURIX Hardware User Manual
=============================================

Parses ``.reqifz`` files (OMG ReqIF format) from the AURIX HW User Manual
repository.  Extracts the full document structure hierarchically and returns
typed content objects (headings, information blocks, registers, bitfields).

The ReqIF file is a ZIP containing:
  - One large ``.reqif`` XML file (~190MB for TC44x)
  - An ``images/`` folder with SVG (vector) and PNG (raster) diagrams

This parser extracts:
  - Module chapters (66 modules for TC44x)
  - Hierarchical sections (functional description, registers, etc.)
  - Registers with IP-XACT structured bitfield data
  - Diagrams (SVG/PNG paths for LLM description)
  - Formula images (MathML rendered as PNG)
  - Tables (HTML → structured data)

Usage::

    from reqif_parser import ReqIFParser

    parser = ReqIFParser(reqifz_path="path/to/file.reqifz")
    parser.load()

    # Get all module names
    modules = parser.get_module_names()

    # Extract one module's full chapter
    chapter = parser.extract_module("GPT12")
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from lxml import etree

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ReqIFImage:
    """An image referenced in the ReqIF XHTML content."""
    path: str                    # e.g. "images/plo1631511702541_Standard.svg"
    image_type: str              # "diagram", "formula", "register_bitfield"
    alt_text: str = ""
    description: str = ""        # Populated later by LLM vision


@dataclass
class ReqIFBitField:
    """A single bitfield within a register."""
    name: str
    bits: str                    # e.g. "31:24" or "0"
    field_type: str              # e.g. "rw", "r", "w", "rh"
    reset_value: str = ""
    description: str = ""


@dataclass
class ReqIFRegister:
    """A hardware register extracted from IP-XACT data."""
    name: str                    # Short name (e.g. "EXTRGSEL")
    long_name: str               # Full name (e.g. "External trigger select register...")
    offset: str = ""             # Address offset (e.g. "0x0000")
    size: str = ""               # Register size (e.g. "32")
    reset_value: str = ""        # Reset value (e.g. "00000000H")
    access: str = ""             # Access mode (e.g. "rw")
    bitfields: list[ReqIFBitField] = field(default_factory=list)
    raw_html: str = ""           # Full XHTML for reference


@dataclass
class ReqIFSection:
    """A content section (information block) within a module chapter."""
    obj_id: str                  # ReqIF IDENTIFIER
    title: str
    kind: str                    # "Heading", "Information", "Register"
    text_content: str = ""       # Plain text extracted from XHTML
    html_content: str = ""       # Raw XHTML for table/structure preservation
    has_table: bool = False
    images: list[ReqIFImage] = field(default_factory=list)
    register: Optional[ReqIFRegister] = None  # Populated for kind="Register"


@dataclass
class ReqIFModule:
    """A complete module chapter extracted from the ReqIF."""
    name: str                    # Module name (e.g. "GPT12", "ADC", "DMA")
    prefix: str                  # IP-XACT prefix (e.g. "GPTONETWO", "EVADC")
    chapter_obj_id: str          # Root hierarchy node object ID
    sections: list[ReqIFSection] = field(default_factory=list)
    device_variant: str = ""     # e.g. "TC44x"


# ---------------------------------------------------------------------------
# HTML/XHTML Helpers
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Extract plain text from XHTML, stripping all tags."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data.strip())

    def get_text(self) -> str:
        return " ".join(p for p in self._parts if p)


def _extract_text(html: str) -> str:
    """Strip HTML tags and return plain text."""
    ext = _TextExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    return ext.get_text()


def _extract_images(html: str) -> list[ReqIFImage]:
    """Find all image references in XHTML content."""
    images = []
    # Pattern: <xhtml:object data="images/xxx.png" type="image/png">
    for m in re.finditer(r'data="(images/[^"]+\.(png|svg|jpg))"', html, re.IGNORECASE):
        path = m.group(1)
        # Classify image type
        if "mathml_" in path:
            img_type = "formula"
        elif "Standard" in path:
            img_type = "diagram"
        else:
            img_type = "other"
        # Extract alt text (fallback text inside <object>)
        alt = ""
        alt_m = re.search(
            re.escape(path) + r'[^>]*>([^<]*)</(?:xhtml:)?object>',
            html
        )
        if alt_m:
            alt = alt_m.group(1).strip()
        images.append(ReqIFImage(path=path, image_type=img_type, alt_text=alt))
    return images


def _extract_register_data(html: str) -> Optional[ReqIFRegister]:
    """Parse IP-XACT structured register data from XHTML.

    Registers have three sections identified by CSS classes:
    - RegisterAbout: name, offset, reset value
    - RegisterImage: bitfield diagram (HTML table)
    - RegisterDefinition: field details table
    """
    if 'ipxact_name' not in html and 'RegisterAbout' not in html:
        return None

    # Extract register name
    name_m = re.search(r'class="ipxact_name"[^>]*>([^<]+)', html)
    name = name_m.group(1).strip() if name_m else ""

    # Extract long name
    long_m = re.search(r'class="ipxact_longName"[^>]*>([^<]+)', html)
    long_name = long_m.group(1).strip() if long_m else ""

    # Extract offset
    offset_m = re.search(r'class="ipxact_addressOffset"[^>]*>([^<]+)', html)
    offset = offset_m.group(1).strip() if offset_m else ""

    # Extract size
    size_m = re.search(r'class="ipxact_size"[^>]*>([^<]+)', html)
    size = size_m.group(1).strip() if size_m else ""

    # Extract reset value
    reset_m = re.search(r'class="ipxact_reset_value"[^>]*>([^<]+)', html)
    reset_value = reset_m.group(1).strip() if reset_m else ""

    # Extract access mode
    access_m = re.search(r'class="ipxact_access"[^>]*>([^<]+)', html)
    access = access_m.group(1).strip() if access_m else ""

    reg = ReqIFRegister(
        name=name,
        long_name=long_name,
        offset=offset,
        size=size,
        reset_value=reset_value,
        access=access,
        raw_html=html,
    )

    # Extract bitfields from RegisterDefinition table
    # Pattern 1: rows with 5 columns including ipxact_reset_value
    field_pattern_5col = re.compile(
        r'class="ipxact_field"[^>]*>([^<]*)</.*?'
        r'class="ipxact_bitoffset_bitwidth"[^>]*>([^<]*)</.*?'
        r'class="ipxact_access"[^>]*>([^<]*)</.*?'
        r'class="ipxact_reset_value"[^>]*>([^<]*)</.*?'
        r'class="ipxact_description"[^>]*>(.*?)</(?:xhtml:)?td',
        re.DOTALL
    )

    for fm in field_pattern_5col.finditer(html):
        bf = ReqIFBitField(
            name=fm.group(1).strip(),
            bits=fm.group(2).strip(),
            field_type=fm.group(3).strip(),
            reset_value=fm.group(4).strip(),
            description=_extract_text(fm.group(5)),
        )
        reg.bitfields.append(bf)

    # Pattern 2: 4-column format (no ipxact_reset_value column)
    # Columns: ipxact_name, ipxact_bitoffset_bitwidth, ipxact_access, ipxact_description
    if not reg.bitfields:
        rows = re.findall(
            r'<(?:xhtml:)?tr[^>]*class="ipxact_field"[^>]*>(.*?)</(?:xhtml:)?tr>',
            html, re.DOTALL
        )
        for row in rows:
            # Use class-based cell identification for correct mapping
            name_m = re.search(r'class="ipxact_name"[^>]*>(.*?)</(?:xhtml:)?td', row, re.DOTALL)
            bits_m = re.search(r'class="ipxact_bitoffset_bitwidth"[^>]*>(.*?)</(?:xhtml:)?td', row, re.DOTALL)
            access_m = re.search(r'class="ipxact_access"[^>]*>(.*?)</(?:xhtml:)?td', row, re.DOTALL)
            desc_m = re.search(r'class="ipxact_description"[^>]*>(.*?)</(?:xhtml:)?td', row, re.DOTALL)
            reset_m = re.search(r'class="ipxact_reset_value"[^>]*>(.*?)</(?:xhtml:)?td', row, re.DOTALL)

            bf_name = _extract_text(name_m.group(1)) if name_m else ""
            bf_bits = _extract_text(bits_m.group(1)) if bits_m else ""
            bf_access = _extract_text(access_m.group(1)) if access_m else ""
            bf_desc = _extract_text(desc_m.group(1)) if desc_m else ""
            bf_reset = _extract_text(reset_m.group(1)) if reset_m else ""

            if bf_name and bf_name.lower() not in ("field", "bits", "type"):
                bf = ReqIFBitField(
                    name=bf_name,
                    bits=bf_bits,
                    field_type=bf_access,
                    reset_value=bf_reset,
                    description=bf_desc,
                )
                reg.bitfields.append(bf)

    # Fallback: positional cell extraction if class-based approaches failed
    if not reg.bitfields:
        rows = re.findall(
            r'<(?:xhtml:)?tr[^>]*class="ipxact_field"[^>]*>(.*?)</(?:xhtml:)?tr>',
            html, re.DOTALL
        )
        for row in rows:
            cells = re.findall(r'<(?:xhtml:)?td[^>]*>(.*?)</(?:xhtml:)?td>', row, re.DOTALL)
            if len(cells) >= 4:
                # If only 4 cells, the 4th is description (no reset_value column)
                if len(cells) == 4:
                    bf = ReqIFBitField(
                        name=_extract_text(cells[0]),
                        bits=_extract_text(cells[1]),
                        field_type=_extract_text(cells[2]),
                        reset_value="",
                        description=_extract_text(cells[3]),
                    )
                else:
                    bf = ReqIFBitField(
                        name=_extract_text(cells[0]),
                        bits=_extract_text(cells[1]),
                        field_type=_extract_text(cells[2]),
                        reset_value=_extract_text(cells[3]),
                        description=_extract_text(cells[4]),
                    )
                if bf.name and bf.name.lower() not in ("field", "bits", "type"):
                    reg.bitfields.append(bf)

    return reg


# ---------------------------------------------------------------------------
# Module Prefix Mapping
# ---------------------------------------------------------------------------
# Maps IP-XACT module identifier prefixes (from register IDs) to
# human-readable module names. Built dynamically during parsing.

# Known prefix → module name overrides
_PREFIX_TO_MODULE: dict[str, str] = {
    "GPTONETWO": "GPT12",
    "EVADC": "ADC",
    "EVADCC": "ADCC",
    "ASCLIN": "LIN",
    "MCMCAN": "CAN",
    "EGTM": "GTM",
    "QSPI": "SPI",
    "LETH": "LETH",
    "GETH": "GETH",
    "SDMMC": "SDMMC",
    "HSPDM": "HSPDM",
    "CONVCTRL": "CONVCTRL",
    "EDSADC": "DSADC",
}


# ---------------------------------------------------------------------------
# Main Parser Class
# ---------------------------------------------------------------------------

class ReqIFParser:
    """Parses an AURIX Hardware User Manual .reqifz file."""

    def __init__(self, reqifz_path: str | Path):
        self.reqifz_path = Path(reqifz_path)
        self._content: str = ""
        self._hierarchy: str = ""
        self._objects_section: str = ""
        self._hierarchy_tree: Optional[etree._Element] = None
        self._parent_map: dict = {}
        self._module_prefixes: dict[str, list[str]] = {}  # prefix → [obj_ids]
        self._loaded = False

    def load(self) -> None:
        """Load and index the ReqIF file. Call this first."""
        if self._loaded:
            return

        logger.info("Loading ReqIF: %s", self.reqifz_path.name)
        if not self.reqifz_path.exists():
            raise FileNotFoundError(f"ReqIF file not found: {self.reqifz_path}")

        with zipfile.ZipFile(str(self.reqifz_path), 'r') as z:
            # Find the .reqif XML file inside the ZIP
            reqif_files = [f for f in z.namelist() if f.endswith('.reqif')]
            if not reqif_files:
                raise ValueError(f"No .reqif file found in {self.reqifz_path}")

            reqif_name = reqif_files[0]
            logger.info("Reading %s from ZIP...", reqif_name)
            with z.open(reqif_name) as f:
                self._content = f.read().decode('utf-8', errors='replace')

        file_size_mb = len(self._content) / (1024 * 1024)
        logger.info("Loaded %.0f MB", file_size_mb)

        # Extract hierarchy section (SPECIFICATIONS)
        specs_start = self._content.find('<SPECIFICATIONS>')
        specs_end = self._content.find('</SPECIFICATIONS>') + len('</SPECIFICATIONS>')
        self._hierarchy = self._content[specs_start:specs_end]
        logger.info("Hierarchy section: %d KB", len(self._hierarchy) // 1024)

        # Extract objects section (SPEC-OBJECTS) — keep start/end for offset lookup
        self._obj_start = self._content.find('<SPEC-OBJECTS>')
        self._obj_end = self._content.find('</SPEC-OBJECTS>') + 15
        self._objects_section = self._content[self._obj_start:self._obj_end]
        logger.info("Objects section: %d MB", len(self._objects_section) // (1024 * 1024))

        # Parse hierarchy as XML tree
        logger.info("Parsing hierarchy XML...")
        self._hierarchy_tree = etree.fromstring(self._hierarchy.encode('utf-8'))
        self._parent_map = {c: p for p in self._hierarchy_tree.iter() for c in p}

        # Index module prefixes from register IDs
        logger.info("Indexing module prefixes...")
        self._index_modules()

        self._loaded = True
        logger.info("ReqIF loaded. Found %d module prefixes.", len(self._module_prefixes))

    def _index_modules(self) -> None:
        """Scan SPEC-OBJECT-REFs for module prefix patterns."""
        # Pattern: IFX_ATVMC_CE_{PREFIX}_100_{generic|delta}_...
        pattern = re.compile(r'IFX_ATVMC_CE_([A-Z0-9]+)_100_')
        refs = re.findall(r'<SPEC-OBJECT-REF>([^<]+)</SPEC-OBJECT-REF>', self._hierarchy)

        prefix_refs: dict[str, set[str]] = {}
        for ref in refs:
            m = pattern.search(ref)
            if m:
                prefix = m.group(1)
                if prefix not in prefix_refs:
                    prefix_refs[prefix] = set()
                prefix_refs[prefix].add(ref)

        self._module_prefixes = {k: list(v) for k, v in prefix_refs.items()}

    def get_module_names(self) -> list[str]:
        """Return list of discovered module names (human-readable)."""
        self._ensure_loaded()
        result = []
        for prefix in sorted(self._module_prefixes.keys()):
            name = _PREFIX_TO_MODULE.get(prefix, prefix)
            result.append(name)
        return result

    def get_module_prefixes(self) -> dict[str, str]:
        """Return mapping of module name → IP-XACT prefix."""
        self._ensure_loaded()
        result = {}
        for prefix in self._module_prefixes:
            name = _PREFIX_TO_MODULE.get(prefix, prefix)
            result[name] = prefix
        return result

    def _ensure_loaded(self):
        if not self._loaded:
            raise RuntimeError("Call .load() first")

    def _get_obj_ref(self, node: etree._Element) -> str:
        """Get SPEC-OBJECT-REF text from a hierarchy node."""
        ref = node.find('.//SPEC-OBJECT-REF')
        return ref.text.strip() if ref is not None and ref.text else ''

    def _find_module_chapter(self, prefix: str) -> Optional[etree._Element]:
        """Find the chapter-level hierarchy node for a module.

        Strategy: find a known register ref for this module in the hierarchy,
        then walk UP to find the common ancestor that contains both the
        functional description and register sections.
        """
        # Find RegistersChapter or similar pattern for this module
        all_hier = self._hierarchy_tree.findall('.//SPEC-HIERARCHY')

        # Find nodes referencing this module's register objects
        module_nodes = []
        for h in all_hier:
            ref = self._get_obj_ref(h)
            if f'IFX_ATVMC_CE_{prefix}_100_' in ref:
                module_nodes.append(h)

        if not module_nodes:
            logger.warning("No hierarchy nodes found for prefix %s", prefix)
            return None

        # Find the common ancestor of all module nodes
        # Use first and last to find LCA
        first_node = module_nodes[0]
        last_node = module_nodes[-1]

        first_ancestors = set()
        current = first_node
        while current is not None:
            first_ancestors.add(id(current))
            current = self._parent_map.get(current)

        # Walk up from last_node to find first common ancestor
        current = last_node
        while current is not None:
            if id(current) in first_ancestors:
                # This is the LCA — but we want the module chapter, not SPECIFICATION root
                # The module chapter is typically 2 levels below SPECIFICATION
                tag = current.tag
                if tag == 'CHILDREN':
                    # Go up one more — the SPEC-HIERARCHY wrapping this CHILDREN
                    parent = self._parent_map.get(current)
                    if parent is not None and parent.tag == 'SPEC-HIERARCHY':
                        return parent
                    # If parent is SPECIFICATION, we went too high
                    # Return the CHILDREN element (contains all chapter sections)
                    return current
                elif tag == 'SPEC-HIERARCHY':
                    return current
                break
            current = self._parent_map.get(current)

        # Fallback: return the CHILDREN containing all module refs
        return None

    def extract_module(self, module_name: str) -> Optional[ReqIFModule]:
        """Extract a complete module chapter from the ReqIF.

        Args:
            module_name: Module name (e.g. "GPT12", "ADC") or prefix (e.g. "GPTONETWO")

        Returns:
            ReqIFModule with all sections, or None if not found.
        """
        self._ensure_loaded()

        # Resolve module name to prefix
        prefix = None
        name_upper = module_name.upper()

        # Check if it's already a prefix
        if name_upper in self._module_prefixes:
            prefix = name_upper
        else:
            # Look up in the name→prefix map
            prefixes = self.get_module_prefixes()
            if name_upper in prefixes:
                prefix = prefixes[name_upper]
            else:
                # Try reverse lookup
                for pname, ppref in _PREFIX_TO_MODULE.items():
                    if ppref.upper() == name_upper:
                        prefix = pname
                        break

        if prefix is None:
            logger.error("Module '%s' not found. Available: %s",
                         module_name, self.get_module_names()[:10])
            return None

        human_name = _PREFIX_TO_MODULE.get(prefix, prefix)
        logger.info("Extracting module: %s (prefix=%s)", human_name, prefix)

        # Find chapter in hierarchy
        chapter_node = self._find_module_chapter(prefix)
        if chapter_node is None:
            logger.error("Could not find chapter hierarchy for %s", prefix)
            return None

        # Extract all SPEC-OBJECT-REFs from the chapter subtree
        all_refs = []
        for ref_elem in chapter_node.iter('SPEC-OBJECT-REF'):
            if ref_elem.text:
                all_refs.append(ref_elem.text.strip())

        logger.info("Chapter has %d object references", len(all_refs))

        # Look up each object and extract content
        module = ReqIFModule(
            name=human_name,
            prefix=prefix,
            chapter_obj_id=self._get_obj_ref(chapter_node) if chapter_node.tag == 'SPEC-HIERARCHY' else '',
        )

        for ref_id in all_refs:
            section = self._extract_object(ref_id)
            if section:
                module.sections.append(section)

        logger.info("Extracted %d sections for %s", len(module.sections), human_name)
        return module

    def _extract_object(self, obj_id: str) -> Optional[ReqIFSection]:
        """Look up a SPEC-OBJECT by ID and extract its content."""
        marker = f'IDENTIFIER="{obj_id}"'
        pos = self._objects_section.find(marker)
        if pos < 0:
            return None

        obj_end = self._objects_section.find('</SPEC-OBJECT>', pos)
        if obj_end < 0:
            obj_end = pos + 200000
        obj_text = self._objects_section[pos:obj_end]

        # Extract Kind
        kind_match = re.search(r'<ENUM-VALUE-REF>([^<]+)</ENUM-VALUE-REF>', obj_text)
        kind_raw = kind_match.group(1) if kind_match else 'Unknown'
        if 'HEADING' in kind_raw.upper():
            kind = 'Heading'
        elif 'INFORMATION' in kind_raw.upper():
            kind = 'Information'
        elif 'REGISTER' in kind_raw.upper():
            kind = 'Register'
        else:
            kind = kind_raw

        # Fallback: some devices omit or misclassify register objects.
        # TC48x: no ENUM-VALUE-REF at all, uses _stype_register_ attrs.
        # TC49x: uses _stype_erratum_ attrs and ERRATUM enum, but has ipxact content.
        # Detect by attribute refs or ipxact content presence.
        if kind not in ('Heading', 'Information', 'Register'):
            if '_stype_register_' in obj_text or 'class="ipxact' in obj_text:
                kind = 'Register'

        # Extract title (LONG-NAME or first THE-VALUE)
        title = ''
        ln_match = re.search(r'LONG-NAME="([^"]*)"', obj_text)
        if ln_match:
            title = ln_match.group(1)
        if not title:
            val_matches = re.findall(r'THE-VALUE="([^"]*)"', obj_text)
            if val_matches:
                title = val_matches[0]

        # Extract XHTML content
        xhtml_content = ''
        text_start = obj_text.find('<xhtml:div')
        text_end = obj_text.rfind('</xhtml:div>')
        if text_start >= 0 and text_end > text_start:
            xhtml_content = obj_text[text_start:text_end + 12]

        # Extract plain text
        plain_text = _extract_text(xhtml_content)
        # Clean _stype_ metadata noise from text
        plain_text = re.sub(r'\s*_stype_\w+', '', plain_text)

        # If title is still empty or looks like an ID, derive from text content
        if not title or re.match(r'^[A-Z]{4}-[A-Z]{4}-', title):
            # Use first line/sentence of text as title
            if plain_text:
                # Remove _stype_ metadata noise
                clean = re.sub(r'\s*_stype_\w+', '', plain_text)
                first_line = clean.split('.')[0].split('\n')[0][:120]
                title = first_line.strip()
            else:
                title = obj_id

        # Check for tables
        has_table = '<table' in xhtml_content.lower() or '<xhtml:table' in xhtml_content.lower()

        # Extract images
        images = _extract_images(xhtml_content)

        # Extract register data if applicable
        register = None
        if kind == 'Register':
            register = _extract_register_data(xhtml_content)

        section = ReqIFSection(
            obj_id=obj_id,
            title=title if title else _extract_text(xhtml_content)[:80],
            kind=kind,
            text_content=plain_text,
            html_content=xhtml_content,
            has_table=has_table,
            images=images,
            register=register,
        )

        return section

    def extract_image_bytes(self, image_path: str) -> Optional[bytes]:
        """Extract an image file from the .reqifz ZIP.

        Args:
            image_path: Relative path inside ZIP (e.g. "images/plo163...svg")

        Returns:
            Raw image bytes, or None if not found.
        """
        try:
            with zipfile.ZipFile(str(self.reqifz_path), 'r') as z:
                if image_path in z.namelist():
                    return z.read(image_path)
        except Exception as e:
            logger.warning("Could not extract image %s: %s", image_path, e)
        return None

    def get_available_images(self) -> list[str]:
        """List all image files in the .reqifz ZIP."""
        with zipfile.ZipFile(str(self.reqifz_path), 'r') as z:
            return [f for f in z.namelist() if f.startswith('images/')]
