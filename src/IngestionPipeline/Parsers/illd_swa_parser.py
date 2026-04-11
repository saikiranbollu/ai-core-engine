"""
SWA Header File Parser (with LLM enrichment & parallel processing)
===================================================================

Parses C header files written in the SWA (Software Architecture) format and
extracts macros, typedefs, enums, structs, and function prototypes into
structured dicts.  An **optional** LLM enrichment step fills in purpose /
usage / technical details for each element.

Features:
  - Parallel LLM enrichment via ``ThreadPoolExecutor``
  - Checkpoint / resume capability (survives crashes mid-enrichment)
  - Retry logic with backoff (3 retries per item)
  - Proper HTTP timeout handling

Usage::

    from IngestionPipeline.Parsers import illd_swa_parser

    # Pure parsing — no LLM, no network
    result = illd_swa_parser.parse("IfxCxpi_swa.h")

    # With LLM enrichment (requires api_key or token_manager)
    result = illd_swa_parser.parse(
        "IfxCxpi_swa.h",
        enrich=True,
        api_key="...",
        base_url="https://gpt4ifx.icp.infineon.com",
        model="gpt-5.2",
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_TIMEOUT = 300       # 5 minutes per LLM call
_MAX_RETRIES = 3         # retry failed enrichments up to 3 times

# ---------------------------------------------------------------------------
# Core extraction helpers (no LLM, no I/O beyond file read)
# ---------------------------------------------------------------------------

class _SWAExtractor:
    """Pure parser for SWA header files."""

    def __init__(self, content: str, filename: str):
        self._content = content
        self._base = Path(filename).stem
        self._module = self._base.replace('_swa', '').replace('Ifx', '')

    @property
    def module_name(self) -> str:
        return self._module

    # ── section extraction ───────────────────────────────────────────

    def _section(self, name: str) -> str:
        pat = r'/\*[-]+([\w\s]+)[-]+\*/'
        matches = list(re.finditer(pat, self._content))
        for i, m in enumerate(matches):
            if m.group(1).strip().lower() == name.strip().lower():
                start = m.end()
                end = len(self._content) if i + 1 >= len(matches) else matches[i + 1].start()
                return self._content[start:end]
        return ""

    # ── macros ───────────────────────────────────────────────────────

    def extract_macros(self) -> List[Dict[str, Any]]:
        section = self._section('Macros') or self._content
        lines = section.split('\n')
        names = {
            l.split()[1]
            for l in lines if l.strip().startswith('#define') and len(l.split()) >= 2
        }
        macros: List[Dict[str, Any]] = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line.startswith('#define'):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            value = ' '.join(parts[2:]).strip()
            desc = self._comment_before(lines, i)
            related = self._related_macros(name, names)
            macros.append({
                'name': name, 'value': value, 'description': desc,
                'related_macros': related,
            })
        return macros

    # ── typedefs ─────────────────────────────────────────────────────

    def extract_typedefs(self) -> List[Dict[str, Any]]:
        section = self._section('Type Definitions')
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r'typedef\s+([^;]+)\s+(\w+)\s*;', section):
            dox = self._preceding_doxygen(section[:m.start()])
            out.append({
                'name': m.group(2).strip(), 'type': m.group(1).strip(),
                'brief': self._brief(dox), 'doxygen_full': dox,
            })
        return out

    # ── enums ────────────────────────────────────────────────────────

    def extract_enums(self) -> List[Dict[str, Any]]:
        section = self._section('Enumerations')
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r'typedef\s+enum\s*\{([\s\S]*?)\}\s*(\w+)\s*;', section):
            dox = self._preceding_doxygen(section[:m.start()])
            vals: List[Dict[str, str]] = []
            for v in re.finditer(
                r'(\w+)\s*=\s*([^,/]*)\s*(?:,.*?)?\s*/\*\*<\s*\\brief\s*([^*]*)\*/',
                m.group(1),
            ):
                vals.append({'name': v.group(1).strip(), 'value': v.group(2).strip(), 'description': v.group(3).strip()})
            out.append({
                'name': m.group(2).strip(), 'brief': self._brief(dox),
                'values': vals, 'doxygen_full': dox,
            })
        return out

    # ── structs ──────────────────────────────────────────────────────

    def extract_structs(self) -> List[Dict[str, Any]]:
        section = self._section('Data Structures')
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r'typedef\s+struct\s*\{([\s\S]*?)\}\s*(\w+)\s*;', section):
            dox = self._preceding_doxygen(section[:m.start()])
            members = self._struct_members(m.group(1))
            out.append({
                'name': m.group(2).strip(), 'brief': self._brief(dox),
                'members': members, 'doxygen_full': dox,
            })
        return out

    # ── functions ────────────────────────────────────────────────────

    def extract_functions(self) -> List[Dict[str, Any]]:
        section = self._section('Global Function Prototypes') or self._content
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r'([\w\s\*]+)\s+(\w+)\s*\(([^)]*)\)\s*;', section):
            ret = m.group(1).strip()
            name = m.group(2).strip()
            params_str = m.group(3).strip()
            dox = self._preceding_doxygen(section[:m.start()])
            params = self._parse_params(params_str)
            out.append({
                'name': name, 'return_type': ret,
                'parameters': params, 'brief': self._brief(dox),
                'detailed_description': self._brief(dox),
                'dependencies': self._dependencies(dox),
                'trace_info': self._trace(dox, self._brief(dox)),
                'param_details': self._param_details(params, dox),
                'param_ranges': self._param_ranges(dox),
                'return_details': self._return_details(dox),
                'retval_details': self._retval_details(dox),
            })
        return out

    # ── internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _comment_before(lines: List[str], idx: int) -> str:
        desc = 'No description'
        j = idx - 1
        while j >= 0:
            l = lines[j].strip()
            if not (l.startswith('/**') or l.startswith('*') or l.startswith('//') or l == ''):
                break
            if l.startswith(('/**', '*', '//')):
                c = re.sub(r'^\s*\*?\s*', '', l).replace('*/', '')
                if r'\brief' in c:
                    desc = re.sub(r'\\brief\s*', '', c).strip()
                    break
                if desc == 'No description' and c:
                    desc = c.strip()
            j -= 1
        return desc

    @staticmethod
    def _related_macros(name: str, names: set) -> List[str]:
        pairs = {'TRUE': 'FALSE', 'FALSE': 'TRUE', 'ENABLE': 'DISABLE',
                 'DISABLE': 'ENABLE', 'GET': 'SET', 'SET': 'GET'}
        return [v for k, v in pairs.items() if re.search(k, name, re.IGNORECASE) and v in names]

    @staticmethod
    def _preceding_doxygen(text: str) -> str:
        blocks = re.findall(r'/\*[\s\S]*?\*/', text)
        return blocks[-1] if blocks else ''

    @staticmethod
    def _brief(dox: str) -> str:
        m = re.search(r'\\brief\s+([^\n]+)', dox)
        return m.group(1).strip() if m else ''

    @staticmethod
    def _struct_members(body: str) -> List[Dict[str, str]]:
        members: List[Dict[str, str]] = []
        for raw_line in body.split('\n'):
            line = raw_line.strip()
            if not line or line.startswith(('/*', '*')):
                continue
            idx = raw_line.find('/*')
            decl = raw_line[:idx].strip() if idx >= 0 else raw_line.strip()
            if ';' not in decl:
                continue
            decl = re.sub(r';.*$', '', decl).strip()
            tokens = decl.split()
            if len(tokens) < 2:
                continue
            name_tok = tokens[-1]
            type_tok = ' '.join(tokens[:-1])
            # inline comment description
            cm = re.search(r'/\*[\s\S]*?\*/', raw_line)
            desc = ''
            if cm:
                bm = re.search(r'\\brief\s+([^\n*]+)', cm.group(0))
                desc = bm.group(1).strip() if bm else re.sub(r'/\*+|\\?\*+/|/\*<|//+|\*', '', cm.group(0)).strip()
            members.append({'type': type_tok, 'name': re.sub(r'^\*+', '', name_tok).strip(), 'description': desc})
        return members

    @staticmethod
    def _parse_params(s: str) -> List[Dict[str, str]]:
        if not s or s == 'void':
            return []
        out: List[Dict[str, str]] = []
        for p in s.split(','):
            p = p.strip()
            m = re.match(r'(.+?)(\b\w+)(\s*(?:\[[^\]]*\])?)', p)
            if m:
                out.append({'type': m.group(1).strip(), 'name': m.group(2).strip()})
            else:
                out.append({'type': p, 'name': ''})
        return out

    @staticmethod
    def _dependencies(dox: str) -> List[str]:
        m = re.search(r'\\depends\{([^}]+)\}', dox)
        return [s.strip() for s in m.group(1).split(',')] if m else []

    @staticmethod
    def _return_details(dox: str) -> str:
        m = re.search(r'\\return\s*([^\n]+)', dox)
        return m.group(1).strip() if m else 'Not available'

    @staticmethod
    def _retval_details(dox: str) -> List[str]:
        m = re.search(r'\\retval\{([^}]+)\}', dox)
        return [s.strip() for s in m.group(1).split(',')] if m else []

    @staticmethod
    def _param_details(params: List[Dict[str, str]], dox: str) -> List[str]:
        tags = list(re.finditer(r'\\param(?:\[[^\]]*\])?\s*(\w+)?\s*([^\n]*)', dox))
        out: List[str] = []
        for i, param in enumerate(params):
            pn = param.get('name', '')
            detail = 'Not available'
            if pn and tags:
                hit = next((t for t in tags if t.group(1) and t.group(1).strip() == pn), None)
                if hit:
                    detail = hit.group(2).strip() or detail
            elif i < len(tags):
                detail = tags[i].group(2).strip() or detail
            out.append(f"{pn}: {detail}" if pn else detail)
        return out

    @staticmethod
    def _param_ranges(dox: str) -> Dict[str, Dict[str, str]]:
        return {
            m.group(1): {'min': m.group(2).strip(), 'max': m.group(3).strip()}
            for m in re.finditer(r'\\paramrange\{(\w+),\s*([^,]+),\s*([^\}]+)\}', dox)
        }

    @staticmethod
    def _trace(dox: str, fallback: str) -> Dict[str, Any]:
        info: Dict[str, Any] = {'name': '', 'requirements': [], 'desc': fallback}
        tm = re.search(r'\\trace\s*\{', dox, re.IGNORECASE)
        if not tm:
            return info
        start = dox.find('{', tm.start())
        if start < 0:
            return info
        i, depth = start + 1, 1
        while i < len(dox) and depth:
            if dox[i] == '{':
                depth += 1
            elif dox[i] == '}':
                depth -= 1
            i += 1
        tc = dox[start + 1:i - 1].strip() if depth == 0 else ''
        if not tc:
            return info
        dm = re.search(r'Desc:\s*([\s\S]*)$', tc, re.IGNORECASE)
        if dm:
            info['desc'] = dm.group(1).replace('}', '').strip()
        comma = tc.find(',')
        info['name'] = tc[:comma].strip() if comma >= 0 else tc.split(',')[0].strip()
        # requirements
        reqs: set = set()
        for m in re.finditer(r'@tar\{([\s\S]*?)\}', tc):
            if m.group(1):
                reqs.add(m.group(1).strip())
        san = re.sub(r'[{}@]', ' ', tc)
        for m in re.finditer(r'\b[A-Z0-9]+-REQ[A-Z0-9]*-\d+\b', san):
            reqs.add(m.group(0).strip())
        info['requirements'] = sorted(reqs)
        return info


# ---------------------------------------------------------------------------
# Checkpoint manager — survives crashes mid-enrichment
# ---------------------------------------------------------------------------

class _CheckpointManager:
    """Persists enrichment progress so a long run can be resumed."""

    def __init__(self, source_name: str, checkpoint_dir: Path | None = None):
        self._dir = checkpoint_dir or (
            Path.home() / ".cache" / "aice_swa_parser" / "checkpoints"
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / f"{source_name}.json"

    def save(self, done_keys: set[str], data: Dict[str, Any]) -> None:
        payload = {
            "done_keys": sorted(done_keys),
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }
        with open(self._file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)

    def load(self) -> tuple[set[str], Dict[str, Any] | None]:
        if self._file.exists():
            with open(self._file, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return set(payload.get("done_keys", [])), payload.get("data")
        return set(), None

    def clear(self) -> None:
        if self._file.exists():
            self._file.unlink()


# ---------------------------------------------------------------------------
# Optional LLM enrichment (parallel, with retry & checkpoints)
# ---------------------------------------------------------------------------

def _build_enrichment_client(
    api_key: str, base_url: str, model: str, ca_bundle: Optional[str],
):
    """Create a ChatOpenAI client with timeout and optional CA bundle."""
    import httpx
    from langchain_openai import ChatOpenAI

    verify: bool | str = True
    if ca_bundle and Path(ca_bundle).exists():
        verify = ca_bundle

    http_client = httpx.Client(verify=verify, timeout=httpx.Timeout(_API_TIMEOUT))

    return ChatOpenAI(
        api_key=api_key, base_url=base_url, model=model,
        max_tokens=13000, temperature=0,
        http_client=http_client, request_timeout=_API_TIMEOUT,
    )


_FIELD_MAP = {
    "macros": ["purpose", "usage_context", "technical_details"],
    "typedefs": ["purpose", "usage_context", "technical_details"],
    "enums": ["purpose", "usage_context", "value_analysis", "best_practices"],
    "structs": ["purpose", "usage_context", "member_analysis", "initialization", "memory_considerations"],
    "functions": ["purpose", "usage_notes", "error_handling"],
}


def _enrich_item(
    client, category: str, fields: list, module: str,
    item: Dict[str, Any], retry: int = 0,
) -> None:
    """Enrich a single item via LLM call with retry logic."""
    prompt = (
        f"You are a firmware documentation expert. Generate JSON with these keys "
        f"for a {category[:-1]} from module {module}: {fields}.\n"
        f"Element: {json.dumps(item, default=str)}\nReturn ONLY valid JSON."
    )

    try:
        resp = client.invoke(prompt)
        txt = resp.content if hasattr(resp, "content") else str(resp)
        jm = re.search(r"\{[\s\S]*\}", txt)
        obj = json.loads(jm.group(0)) if jm else {}
    except Exception as exc:
        if retry < _MAX_RETRIES - 1:
            logger.warning(
                "Enrichment %s/%s attempt %d failed (%s). Retrying in 5s…",
                category, item.get("name", "?"), retry + 1, exc,
            )
            time.sleep(5)
            return _enrich_item(client, category, fields, module, item, retry + 1)
        logger.error(
            "Enrichment %s/%s failed after %d retries: %s",
            category, item.get("name", "?"), _MAX_RETRIES, exc,
        )
        obj = {}

    for f in fields:
        item[f] = obj.get(f, "Not available")


def _enrich(
    data: Dict[str, Any], module: str,
    api_key: str, base_url: str, model: str,
    ca_bundle: Optional[str],
    resume: bool = True,
) -> Dict[str, Any]:
    """Enrich extracted data with LLM-generated descriptions (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..config import get_max_workers

    client = _build_enrichment_client(api_key, base_url, model, ca_bundle)

    # ── Checkpoint: load previous progress ────────────────────────────
    ckpt = _CheckpointManager(f"{module}_swa")
    done_keys: set[str] = set()
    if resume:
        done_keys, saved_data = ckpt.load()
        if saved_data and done_keys:
            logger.info(
                "Resuming enrichment — %d items already done", len(done_keys),
            )
            # Restore previously enriched items
            for cat in _FIELD_MAP:
                saved_items = {
                    it["name"]: it for it in saved_data.get(cat, [])
                }
                for item in data.get(cat, []):
                    if item["name"] in saved_items:
                        item.update(saved_items[item["name"]])

    # ── Build pending work list ───────────────────────────────────────
    pending: list[tuple[str, list, Dict[str, Any]]] = []
    for category, fields in _FIELD_MAP.items():
        for item in data.get(category, []):
            key = f"{category}:{item.get('name', '')}"
            if key not in done_keys:
                pending.append((category, fields, item))

    if not pending:
        logger.info("All items already enriched (from checkpoint)")
        ckpt.clear()
        return data

    # ── Parallel enrichment ───────────────────────────────────────────
    max_workers = get_max_workers("parsers.swa")
    max_workers = max(1, max_workers)
    logger.info(
        "Enriching %d items with %d workers…", len(pending), max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for category, fields, item in pending:
            fut = pool.submit(
                _enrich_item, client, category, fields, module, item,
            )
            futures[fut] = f"{category}:{item.get('name', '')}"

        for fut in as_completed(futures):
            key = futures[fut]
            fut.result()  # propagate unexpected exceptions
            done_keys.add(key)

            # Save checkpoint after each completed item
            ckpt.save(done_keys, data)

    # Clear checkpoint on success
    ckpt.clear()
    logger.info("Enrichment complete — %d items processed", len(pending))
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(
    path: str,
    *,
    enrich: bool = False,
    api_key: Optional[str] = None,
    base_url: str = "https://gpt4ifx.icp.infineon.com",
    model: str = "gpt-5.2",
    ca_bundle: Optional[str] = None,
    resume: bool = True,
) -> Dict[str, Any]:
    """
    Parse an SWA-format C header file.

    Args:
        path:       Path to the ``.h`` file.
        enrich:     If ``True``, call an LLM to generate purpose /
                    usage / technical-detail fields for each element.
        api_key:    API key for enrichment (falls back to ``token_manager``).
        base_url:   OpenAI-compatible endpoint for enrichment.
        model:      Model name for enrichment.
        ca_bundle:  Optional CA bundle for SSL.
        resume:     Resume from checkpoint if available (default True).

    Returns:
        A dict with keys ``module``, ``macros``, ``typedefs``, ``enums``,
        ``structs``, and ``functions``.

    Raises:
        FileNotFoundError: If *path* does not exist.
        RuntimeError:      If *enrich* is True but no API key is available.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    content = p.read_text(encoding="utf-8")
    ext = _SWAExtractor(content, p.name)
    logger.info(
        "Parsing SWA header %s (module=%s, enrich=%s)",
        p.name, ext.module_name, enrich,
    )

    data: Dict[str, Any] = {
        "module": ext.module_name,
        "macros": ext.extract_macros(),
        "typedefs": ext.extract_typedefs(),
        "enums": ext.extract_enums(),
        "structs": ext.extract_structs(),
        "functions": ext.extract_functions(),
    }

    if enrich:
        if not api_key:
            from src.HybridRAG.code.token_manager import get_token
            try:
                token = get_token()
            except RuntimeError:
                raise RuntimeError(
                    "No API key for enrichment and automatic token refresh failed. "
                    "Ensure IFX_USERNAME and IFX_PASSWORD are configured."
                )
        else:
            token = api_key
        data = _enrich(
            data, ext.module_name, token, base_url, model, ca_bundle,
            resume=resume,
        )

    return data
