"""
Fast PDF → Section-list parser for the Ephemeral Sandbox.

Uses pymupdf4llm (already in requirements.txt as PyMuPDF) to convert a PDF
to clean Markdown, then splits the result into per-section dicts for
semantic vector chunking in the sandbox.

No GPT/LLM calls — this runs entirely offline and is suitable for real-time
sandbox uploads.

Public API
----------
convert_pdf_to_sections(pdf_path, pages=None)
    Convert a PDF and return a list of section dicts.

clean_markdown(md)
    Clean raw pymupdf4llm output.  Can be called separately if the caller
    already has the raw Markdown string.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers — bit-diagram / garbage-table removal
#  (ported from the embedded-driver-assistant pdf_to_md.py helper script)
# ─────────────────────────────────────────────────────────────────────────────

_ACCESS_MODE_RE = re.compile(
    r'^\s*(?:r|rw|rh|rwh|w)(?:\s+(?:r|rw|rh|rwh|w))*\s*$')

_BIT_POS_RE = re.compile(
    r'^\s*(?:\d{1,2}\s+){4,}\d{1,2}\s*$')

_BOLD_FIELD_BOX_RE = re.compile(
    r'^\s*(?:\*\*[\w_/\s]{1,20}\*\*\s*){1,}\s*$')


def _remove_bit_diagrams(text: str) -> str:
    """Remove visual register bit-position box diagrams."""
    lines = text.split('\n')
    remove: set = set()

    def _is_diagram_element(s: str) -> bool:
        if s == '':
            return True
        if _BIT_POS_RE.match(s):
            return True
        if _ACCESS_MODE_RE.match(s):
            return True
        if _BOLD_FIELD_BOX_RE.match(s):
            if re.match(r'\*\*Field\*\*\s+\*\*Bits\*\*', s):
                return False
            return True
        return False

    def _consume_block(start: int) -> int:
        j = start + 1
        while j < len(lines):
            s = lines[j].strip()
            if _is_diagram_element(s):
                j += 1
                continue
            if re.match(r'^\|', s) and not re.match(r'^\|Field\|', s):
                cols = s.count('|') - 1
                cells = [c.strip() for c in s.split('|')[1:-1]]
                all_short = all(len(c) <= 15 for c in cells)
                if cols <= 10 and all_short:
                    j += 1
                    if j < len(lines) and re.match(r'^\|(?:-{1,}\|)+\s*$', lines[j]):
                        j += 1
                    continue
            break
        return j

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if _BIT_POS_RE.match(s):
            block_start = i
            j = _consume_block(i)
            if j > block_start + 1:
                for k in range(block_start, j):
                    remove.add(k)
            i = j
        elif _BOLD_FIELD_BOX_RE.match(s):
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and _ACCESS_MODE_RE.match(lines[j].strip()):
                block_start = i
                j = _consume_block(i)
                if j > block_start + 1:
                    for k in range(block_start, j):
                        remove.add(k)
                i = j
            else:
                i += 1
        else:
            i += 1

    return '\n'.join(l for idx, l in enumerate(lines) if idx not in remove)


def _remove_garbage_tables(text: str) -> str:
    """Remove timing-diagram tables, figure artifacts, and page-break fragments."""
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if re.match(r'^\|.*\|$', lines[i]):
            tbl_start = i
            tbl = [lines[i]]
            i += 1
            while i < len(lines) and re.match(r'^\|.*\|$', lines[i]):
                tbl.append(lines[i])
                i += 1

            header = tbl[0]
            num_cols = header.count('|') - 1
            col_placeholder_count = len(re.findall(r'Col\d+', header))

            is_garbage = False
            if num_cols >= 10 and col_placeholder_count >= 3:
                is_garbage = True
            if num_cols == 2 and 'Col2' in header:
                is_garbage = True
            if re.search(r'\(continued\)', header):
                is_garbage = True
            if re.search(r'ontinued\)', header):
                is_garbage = True
            if len(tbl) == 2 and re.match(r'^\|(?:-+\|)+\s*$', tbl[1]):
                is_garbage = True

            if not is_garbage:
                result.extend(tbl)
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def _convert_field_pipe_to_plain(text: str) -> str:
    """Convert |Field|Bits|Type|Description| pipe tables to plain text."""
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if re.match(r'^\|Field\|Bits\|Type\|Description\|', lines[i]):
            tbl = [lines[i]]
            i += 1
            while i < len(lines) and re.match(r'^\|', lines[i]):
                tbl.append(lines[i])
                i += 1

            result.append('**Field** **Bits** **Type** **Description**')
            result.append('')

            for row in tbl[2:]:
                cells = row.split('|')
                if len(cells) < 5:
                    continue
                field = cells[1].strip()
                bits = cells[2].strip()
                ftype = cells[3].strip()
                desc = '|'.join(cells[4:]).rstrip('|').strip()

                field = re.sub(r'<br>\s*[DRAFT ]*$', '', field)
                bits = bits.replace('<br>', ' ')
                desc_parts = re.split(r'<br>\s*', desc)
                first_line = desc_parts[0].strip()

                result.append(f'{field} {bits} {ftype} {first_line}')
                for part in desc_parts[1:]:
                    part = part.strip()
                    if part:
                        result.append(part)
                result.append('')
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def _convert_remaining_pipe_to_plain(text: str) -> str:
    """Remove or convert remaining pipe tables."""
    lines = text.split('\n')

    plain_table_nums: set = set()
    for line in lines:
        m = re.match(r'^\s*\*\*Table\s+(\d+)\*\*', line)
        if m:
            plain_table_nums.add(m.group(1))

    _OVERVIEW_KW = ('Short name', 'Long name', 'Module', 'Base address',
                    'Condition', 'MOD TYPE', 'MOD REV', 'Keyword')

    result = []
    i = 0
    while i < len(lines):
        if re.match(r'^\|.*\|$', lines[i]):
            tbl = [lines[i]]
            i += 1
            while i < len(lines) and re.match(r'^\|.*\|$', lines[i]):
                tbl.append(lines[i])
                i += 1

            first_row = tbl[0]
            skip = False

            m = re.search(r'Table\s+(\d+)', first_row)
            if m and m.group(1) in plain_table_nums:
                skip = True
            if re.search(r'\d+\.\d+(?:\.\d+)?\s+Table\s+\d+', first_row):
                skip = True
            if any(kw in first_row for kw in _OVERVIEW_KW):
                skip = True

            if skip:
                continue

            for row in tbl:
                if re.match(r'^\|(?:-+\|)+\s*$', row):
                    continue
                cells = row.split('|')
                cells = [c.strip() for c in cells[1:-1]]
                cells = [c for c in cells if c and not re.fullmatch(r'Col\d+', c)]
                if not cells:
                    continue
                cells = [c.replace('<br>', ' ') for c in cells]
                result.append(' '.join(cells))
            result.append('')
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def _remove_duplicate_column_headers(text: str) -> str:
    """Remove duplicate bold column-header lines within each ## section."""
    bold_hdr_re = re.compile(r'^\s*(?:\*\*[\w_/\s]{1,20}\*\*\s*){2,}\s*$')
    parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    result = []
    for part in parts:
        seen: set = set()
        lines = part.split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if bold_hdr_re.match(stripped):
                if stripped in seen:
                    continue
                seen.add(stripped)
            cleaned.append(line)
        result.append('\n'.join(cleaned))
    return ''.join(result)


def _remove_spillover_register_tables(text: str) -> str:
    """Remove cross-page spill-over content in register sections."""
    field_line_re = re.compile(r'^(\w[\w_/]*(?:<br>\w[\w_/]*)*)\s+\d')
    field_full_re = re.compile(
        r'^(\w[\w_]*)\s+\d+(?::\d+)?\s+(?:r|rw|rh|rwh|w)\s', re.MULTILINE)
    field_hdr_re = re.compile(
        r'^\*\*Field\*\*\s+\*\*Bits\*\*\s+\*\*Type\*\*\s+\*\*Description\*\*')
    parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    result = []

    recent_fields: List[set] = []
    _MAX_LOOKBACK = 10

    for idx, part in enumerate(parts):
        is_detail = bool(re.search(r'Offset address:', part))

        if not is_detail:
            result.append(part)
            continue

        lines = part.split('\n')

        prev_fields: set = set()
        for fs in recent_fields:
            prev_fields |= fs

        _OVERVIEW_TEXT_RE = re.compile(
            r'^(?:Short name|Long name|Module|Base address|'
            r'Keyword|Condition|Table\s+\d+\s+Register)')
        phase1 = []
        i = 0
        in_overview_spillover = False
        while i < len(lines):
            s = lines[i].strip()
            if re.match(r'^\|.*\|$', lines[i]):
                tbl = []
                while i < len(lines) and re.match(r'^\|.*\|$', lines[i]):
                    tbl.append(lines[i])
                    i += 1
                header = tbl[0] if tbl else ''
                is_spillover = False
                if 'Short name' in header or 'Long name' in header:
                    is_spillover = True
                if 'MOD TYPE' in header or 'MOD REV' in header:
                    is_spillover = True
                if 'Condition' in header and 'Col3' in header:
                    is_spillover = True
                if not is_spillover:
                    phase1.extend(tbl)
            else:
                if _OVERVIEW_TEXT_RE.match(s):
                    in_overview_spillover = True
                if in_overview_spillover:
                    if 'Offset address:' in s or s.startswith('## ') \
                            or field_hdr_re.match(s):
                        in_overview_spillover = False
                        phase1.append(lines[i])
                else:
                    phase1.append(lines[i])
                i += 1

        hdr_positions = [i for i, l in enumerate(phase1)
                         if field_hdr_re.match(l.strip())]

        if len(hdr_positions) >= 2:
            last_hdr = hdr_positions[-1]
            remove: set = set()
            for k in range(len(hdr_positions) - 1):
                start = hdr_positions[k]
                end = hdr_positions[k + 1]
                for linenum in range(start, end):
                    remove.add(linenum)
            phase2 = [l for i, l in enumerate(phase1) if i not in remove]
        else:
            phase2 = phase1

        if prev_fields:
            cleaned_lines = []
            found_own_content = False
            for line in phase2:
                s = line.strip()
                if found_own_content:
                    cleaned_lines.append(line)
                    continue
                if s.startswith('## ') or 'Offset address:' in s \
                        or re.search(r'value:\s*[0-9A-Fa-f]', s) \
                        or s == '':
                    cleaned_lines.append(line)
                    continue
                if field_hdr_re.match(s):
                    found_own_content = True
                    cleaned_lines.append(line)
                    continue
                m = field_line_re.match(s)
                if m:
                    fname = m.group(1).split('<br>')[0]
                    if fname in prev_fields:
                        continue
                    else:
                        found_own_content = True
                        cleaned_lines.append(line)
                else:
                    if not any(field_line_re.match(c.strip())
                               for c in cleaned_lines if c.strip()):
                        continue
                    found_own_content = True
                    cleaned_lines.append(line)
        else:
            cleaned_lines = phase2

        own_fields: set = set(field_full_re.findall('\n'.join(cleaned_lines)))
        recent_fields.append(own_fields)
        if len(recent_fields) > _MAX_LOOKBACK:
            recent_fields.pop(0)

        result.append('\n'.join(cleaned_lines))

    return ''.join(result)


def _fix_numbered_heading_levels(text: str) -> str:
    """Restore correct heading depth from dot-separated section numbers."""
    section_heading_re = re.compile(
        r'^(#{2,})\s+(\d+(?:\.\d+)*)\s+(.*)$', re.MULTILINE)

    def _replace_heading(m: re.Match) -> str:
        num = m.group(2)
        title = m.group(3)
        parts = num.split('.')
        level = max(1, min(6, len(parts)))
        return f"{'#' * level} {num} {title}"

    return section_heading_re.sub(_replace_heading, text)


# ─────────────────────────────────────────────────────────────────────────────
#  Public: clean_markdown
# ─────────────────────────────────────────────────────────────────────────────

def clean_markdown(md: str) -> str:
    """Strip ToC, boilerplate, watermarks, bit diagrams, and garbage tables
    from raw pymupdf4llm Markdown output.

    Identical pipeline to the embedded-driver-assistant ``pdf_to_md.py``
    helper script.
    """
    lines = md.split("\n")

    bold_counts: Dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r'\*\*[^*]+\*\*', stripped):
            bold_counts[stripped] = bold_counts.get(stripped, 0) + 1
    running_headers = {b for b, c in bold_counts.items() if c >= 5}

    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r'(?:#{1,6}\s+)?(?:\*\*)?contents(?:\*\*)?', stripped, re.IGNORECASE):
            continue
        if re.search(r'\.(\s*\.){4,}', stripped):
            continue
        if stripped in running_headers:
            continue
        if re.fullmatch(r'-{3,}', stripped):
            continue
        if re.fullmatch(r'\S+\.vsdx\s*', stripped):
            continue
        if re.fullmatch(r'\*{0,2}\(table continues\.{0,3}\s*\)\*{0,2}', stripped):
            continue
        if re.match(r'^\*{2}\(table\s+continues', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\*\*Table\s+\d+\*\*\s+\*\*\(continued\)', stripped):
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)

    text = re.sub(r'\[title:\s*[^\]]*\]', '', text)
    text = re.sub(r'\[li:\s*[^\]]*\]', '', text)
    text = re.sub(r'^(#{1,6}\s+.*\S)\*{2,}\s*$', r'\1', text, flags=re.MULTILINE)

    text = re.split(r'\n\s*\*{0,2}Trademarks\*{0,2}\s*\n', text, maxsplit=1)[0]

    text = re.sub(r'^D\s+R\s+A\s+F\s+T\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\**restricted\**\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^www\.infineon\.com\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d{4}-\d{2}-\d{2}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^Please read the sections.*end of this document\s*$',
                  '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d{1,3}\s*$', '', text, flags=re.MULTILINE)

    text = re.sub(r'(?<=\|)[^|]*D\s+R\s+A\s+F\s+T[^|]*(?=\|)',
                  lambda m: re.sub(r'\s*D\s+R\s+A\s+F\s+T\s*', ' ', m.group(0)).strip(),
                  text)
    text = re.sub(r'<br>\n?\s*[DRAFT]\s*(?=\|)', '', text)
    text = re.sub(r'(?<=\|)\s*[DRAFT]\s*\n?<br>\n?', '', text)
    text = re.sub(r'\s+(?:[DRAFT] ){1,}[DRAFT]?\s*(?=\|)', '', text)
    text = re.sub(r'<br>\s*(?=\|)', '', text)
    text = re.sub(r'<br>\s*[DRAFT ]+(?=\s+\d)', '', text)

    text = _remove_garbage_tables(text)
    text = _convert_field_pipe_to_plain(text)
    text = _remove_bit_diagrams(text)
    text = _convert_remaining_pipe_to_plain(text)

    # Collapse all deep headings (####, #####, ######) to ## for section splitting.
    # _fix_numbered_heading_levels will restore the correct depth afterwards.
    text = re.sub(r'^#{4,6}\s+', '## ', text, flags=re.MULTILINE)

    text = re.sub(r'^##\s+NoC\s+\w+.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+CMEM\s*$', '', text, flags=re.MULTILINE)

    text = _remove_spillover_register_tables(text)
    text = _remove_duplicate_column_headers(text)
    text = _fix_numbered_heading_levels(text)

    text = re.sub(r'\n{4,}', '\n\n\n', text)

    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Public: convert_pdf_to_sections
# ─────────────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)', re.MULTILINE)
_SECTION_NUM_RE = re.compile(r'^(\d+(?:\.\d+)*)\s+')

# Matches dot-leader TOC entries that survived clean_markdown()
# e.g. "1.4.1 Functional overview ..... 42"
# (same pattern as illd_rag_ingestion.py _TOC_LINE_RE)
_TOC_LINE_RE = re.compile(r'^\d+(?:\.\d+)*\s+.+\.{4,}')

# Matches heading text that identifies a Table-of-Contents or index section.
# Handles numbered variants like "1 Table of Contents", "2.1 Contents", etc.
_TOC_HEADING_RE = re.compile(
    r'(?:table\s+of\s+contents?|list\s+of\s+(?:figures?|tables?|abbreviations?|'
    r'symbols?)|contents?|index)\s*$',
    re.IGNORECASE,
)


def _split_sections(md: str, max_content_chars: int = 2000) -> List[Dict[str, Any]]:
    """Split cleaned Markdown into per-heading section dicts.

    TOC protections (matching illd_rag_ingestion.py + prepare_markdowns.py):

    1. **Dot-leader guard** — any dot-leader TOC lines that survived
       ``clean_markdown()`` are stripped from section content before chunking.
    2. **TOC heading skip** — sections whose heading matches known TOC/index
       keywords are discarded entirely.
    3. **Deduplication** — when the same section number appears twice
       (once as a short TOC stub, once as the real body), only the longer
       version is kept.  Mirrors prepare_markdowns.py dedup logic.

    Each returned dict has:
        heading      (str)  — heading text without leading #s
        level        (int)  — 1–6
        section_num  (str)  — leading section number if present, else ""
        content      (str)  — text under this heading (trimmed to max_content_chars)
    """
    # Find all heading positions
    positions = []
    for m in _HEADING_RE.finditer(md):
        positions.append((m.start(), len(m.group(1)), m.group(2).strip()))

    if not positions:
        # No headings — return the whole text as one section
        content = md.strip()
        if content:
            return [{"heading": "Document", "level": 1, "section_num": "", "content": content[:max_content_chars]}]
        return []

    raw_sections = []
    for i, (pos, level, heading_text) in enumerate(positions):
        # Content runs from end of this heading line to start of next heading
        content_start = md.index('\n', pos) + 1 if '\n' in md[pos:] else len(md)
        content_end = positions[i + 1][0] if i + 1 < len(positions) else len(md)
        raw_content = md[content_start:content_end]

        # ── Protection 1: strip any dot-leader TOC lines that survived clean_markdown() ──
        cleaned_lines = [
            line for line in raw_content.split('\n')
            if not _TOC_LINE_RE.match(line.strip())
        ]
        content = '\n'.join(cleaned_lines).strip()

        # ── Protection 2: skip TOC / index headings by keyword ──
        if _TOC_HEADING_RE.search(heading_text):
            continue

        # Skip near-empty sections (headings with no real content after stripping)
        if len(content) < 30 and not any(c.isalpha() for c in content):
            continue

        # Extract section number from heading text ("1.4.2 Clock Control" → "1.4.2")
        m = _SECTION_NUM_RE.match(heading_text)
        section_num = m.group(1) if m else ""
        clean_heading = heading_text[len(m.group(0)):].strip() if m else heading_text

        raw_sections.append({
            "heading": heading_text,
            "clean_heading": clean_heading,
            "level": level,
            "section_num": section_num,
            "content": content[:max_content_chars],
        })

    # ── Protection 3: deduplicate by section number (keep longest content) ──
    # A section number can appear once in the TOC stub (short) and once in
    # the real body (long).  Mirrors prepare_markdowns.py dedup logic.
    numbered = {}   # section_num → index of best entry in raw_sections
    unnumbered = []
    for i, sec in enumerate(raw_sections):
        num = sec["section_num"]
        if not num:
            unnumbered.append(i)
            continue
        prev = numbered.get(num)
        if prev is None:
            numbered[num] = i
        elif len(sec["content"]) > len(raw_sections[prev]["content"]):
            numbered[num] = i

    # Reconstruct in original order, preferring deduped winners
    winning_indices = set(numbered.values()) | set(unnumbered)
    sections = [
        raw_sections[i]
        for i in range(len(raw_sections))
        if i in winning_indices
    ]

    return sections


def convert_pdf_to_sections(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    max_content_chars: int = 2000,
) -> Dict[str, Any]:
    """Convert a PDF file to a list of section dicts using pymupdf4llm.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the PDF file.
    pages : list[int] | None
        0-based page indices to convert.  None = all pages.
    max_content_chars : int
        Maximum characters per section content (default 2000).

    Returns
    -------
    dict with keys:
        "type"     : "pdf_md"
        "file"     : str
        "sections" : list[dict]  — see _split_sections() for field names
        "raw_md"   : str         — full cleaned markdown (for fallback chunking)
    """
    try:
        import pymupdf4llm  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "pymupdf4llm is required for fast PDF parsing: pip install pymupdf4llm"
        ) from exc

    raw_md: str = pymupdf4llm.to_markdown(pdf_path, pages=pages, show_progress=False)
    cleaned_md: str = clean_markdown(raw_md)
    sections = _split_sections(cleaned_md, max_content_chars=max_content_chars)

    return {
        "type": "pdf_md",
        "file": str(pdf_path),
        "sections": sections,
        "raw_md": cleaned_md,
    }
