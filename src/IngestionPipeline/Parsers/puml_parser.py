"""
PlantUML Sequence Diagram Parser
=================================

Analyses one or more PlantUML (``.puml``) files and extracts two things:

- **core_functions** — function frequency categorisation
  (always / frequently / conditional / rare)
- **phase_patterns** — per-phase (init / operation / error / cleanup)
  common functions & example sequences

Accepts a single ``.puml`` file **or** a directory containing multiple
``.puml`` files.  All diagrams across all files are analysed together.

Usage::

    from IngestionPipeline.Parsers import puml_parser

    # Single file
    result = puml_parser.parse("diagram.puml")

    # Directory of .puml files
    result = puml_parser.parse("lld/Cxpi/doc/arch/input/")

    # result == {"core_functions": {...}, "phase_patterns": {...}}
"""

import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Safety limits
_MAX_REGEX = 100
_FREQ_ALWAYS = 0.9
_FREQ_FREQUENTLY = 0.5
_FREQ_CONDITIONAL = 0.2


class _PUMLAnalyzer:
    """Internal analyser — not part of the public API."""

    def __init__(self, files: List[Tuple[str, str]]):
        """*files* is a list of ``(filename, content)`` tuples."""
        self._sections: List[Dict[str, Any]] = []
        for name, content in files:
            self._sections.extend(self._split_sections(content, name))

    # ── section splitting ────────────────────────────────────────────

    @staticmethod
    def _split_sections(content: str, source: str) -> List[Dict[str, Any]]:
        marker = (
            r"'\s*={60,}\s*\n'\s*DIAGRAM\s+(\d+):\s*(.+?)\n"
            r"'\s*Source:\s*(.+?\.puml)\s*\n'\s*={60,}"
        )
        matches = list(re.finditer(marker, content, re.MULTILINE))

        if matches:
            sections = []
            for i, m in enumerate(matches):
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                c = content[start:end]
                if c.strip():
                    sections.append({
                        "name": m.group(2).strip(),
                        "diagram_number": int(m.group(1)),
                        "content": c,
                        "source_file": m.group(3).strip(),
                    })
            if sections:
                return sections

        # Fallback — split by newpage
        pages = re.split(r'newpage\s*\n', content)
        sections = []
        for idx, page in enumerate(pages):
            if page.strip():
                nm = re.search(r"'?\s*DIAGRAM\s+(\d+):\s*([^\n]+)", page)
                sections.append({
                    "name": nm.group(2).strip() if nm else f"Diagram_{idx}",
                    "diagram_number": idx,
                    "content": page,
                    "source_file": source,
                })
        return sections or [{
            "name": "Main", "diagram_number": 0,
            "content": content, "source_file": source,
        }]

    # ── low-level extractors ─────────────────────────────────────────

    @staticmethod
    def _extract_func_calls(text: str) -> List[Tuple[str, int]]:
        out, order = [], 0
        for m in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', text):
            out.append((m.group(1), order))
            order += 1
            if order >= _MAX_REGEX:
                break
        return out

    # ── phase helpers ────────────────────────────────────────────────

    @staticmethod
    def _phase_name(text: str) -> str:
        m = re.search(r'==\s*([^=]+)\s*==', text)
        if not m:
            return "operation"
        name = m.group(1).lower().strip()
        for phase, kws in {
            "initialization": ["init", "setup", "config"],
            "operation": ["operation", "transfer", "send", "receive", "test", "communication"],
            "error_handling": ["error", "timeout", "inject", "detection"],
            "cleanup": ["cleanup", "end", "release"],
        }.items():
            if any(k in name for k in kws):
                return phase
        return "operation"

    def _split_phases(self, text: str) -> Dict[str, str]:
        phases: Dict[str, str] = defaultdict(str)
        parts = re.split(r'(==\s*[^=]+\s*==)', text)
        cur = "initialization"
        for part in parts:
            if re.match(r'==\s*[^=]+\s*==', part):
                cur = self._phase_name(part)
            else:
                phases[cur] += part
        return dict(phases)

    def _func_seq(self, phase_text: str) -> List[str]:
        return [n for n, _ in sorted(self._extract_func_calls(phase_text), key=lambda x: x[1])]

    # ── validity filter ──────────────────────────────────────────────

    @staticmethod
    def _valid(fn: str) -> bool:
        if len(fn) < 5 or '_' not in fn or fn.isupper():
            return False
        if len(fn.split('_')) < 2:
            return False
        _artifacts = {
            'received', 'initialized', 'mode', 'header', 'response', 'transfer',
            'initialization', 'success', 'failure', 'start', 'end', 'done',
            'pending', 'active', 'inactive', 'enabled', 'disabled',
        }
        return fn.lower() not in _artifacts

    # ── per-section analysis (internal) ──────────────────────────────

    def _analyze_section(self, sec: Dict[str, Any]) -> Dict[str, Any]:
        c = sec["content"]
        return {
            "function_calls": [n for n, _ in self._extract_func_calls(c)],
            "phases": self._split_phases(c),
        }

    # ── core_functions ───────────────────────────────────────────────

    def _categorise_funcs(self, analyses: List[Dict]) -> Dict[str, List[str]]:
        freq: Counter = Counter()
        total = len(analyses)
        for a in analyses:
            freq.update({f for f in a["function_calls"] if self._valid(f)})
        cats: Dict[str, List[str]] = {
            "always_present": [], "frequently_present": [],
            "conditional": [], "rare": [],
        }
        for fn, cnt in freq.items():
            r = cnt / total if total else 0
            if r >= _FREQ_ALWAYS:
                cats["always_present"].append(fn)
            elif r >= _FREQ_FREQUENTLY:
                cats["frequently_present"].append(fn)
            elif r >= _FREQ_CONDITIONAL:
                cats["conditional"].append(fn)
            else:
                cats["rare"].append(fn)
        return cats

    # ── phase_patterns ───────────────────────────────────────────────

    def _phase_patterns(self, analyses: List[Dict]) -> Dict[str, Any]:
        seqs: Dict[str, List[List[str]]] = defaultdict(list)
        funcs: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for a in analyses:
            for phase, content in a["phases"].items():
                seq = [f for f in self._func_seq(content) if self._valid(f)]
                if seq:
                    seqs[phase].append(seq)
                for f in seq:
                    funcs[phase][f] += 1

        out: Dict[str, Any] = {}
        for phase in ("initialization", "operation", "error_handling", "cleanup"):
            if phase not in seqs:
                continue
            common = sorted(funcs[phase].items(), key=lambda x: x[1], reverse=True)
            seen: set = set()
            unique: List = []
            for s in seqs[phase]:
                key = tuple(s)
                if key not in seen:
                    seen.add(key)
                    unique.append(s)
                    if len(unique) >= 3:
                        break
            avg = (
                sum(len(s) for s in seqs[phase]) / len(seqs[phase])
                if seqs[phase] else 0
            )
            out[phase] = {
                "common_functions": [f for f, _ in common[:10] if self._valid(f)],
                "example_sequences": unique,
                "typical_length": round(avg),
            }
        return out

    # ── top-level ────────────────────────────────────────────────────

    def analyze(self) -> Dict[str, Any]:
        analyses = [self._analyze_section(s) for s in self._sections]
        return {
            "core_functions": self._categorise_funcs(analyses),
            "phase_patterns": self._phase_patterns(analyses),
        }


def parse(path: str) -> Dict[str, Any]:
    """
    Analyse PlantUML file(s) and return ``core_functions`` + ``phase_patterns``.

    Args:
        path: Path to a single ``.puml`` file **or** a directory containing
              one or more ``.puml`` files.

    Returns:
        ``{"core_functions": {...}, "phase_patterns": {...}}``

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError:        If a directory contains no ``.puml`` files.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if p.is_dir():
        puml_files = sorted(p.rglob("*.puml"))
        if not puml_files:
            raise ValueError(f"No .puml files found in {path}")
        files = [(f.name, f.read_text(encoding="utf-8")) for f in puml_files]
        logger.info("Found %d .puml files in %s", len(files), path)
    else:
        files = [(p.name, p.read_text(encoding="utf-8"))]

    return _PUMLAnalyzer(files).analyze()
