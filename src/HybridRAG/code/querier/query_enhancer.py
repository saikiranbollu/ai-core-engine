"""
Query Enhancer — GAP-A03 (Sprint 11)
======================================
Pre-processing pipeline that enhances user queries before hybrid search.

Components:
  1. Domain Synonym Expander  — AUTOSAR/MCAL term aliases
  2. Query Complexity Classifier — simple / medium / complex
  3. Search Strategy Predictor  — graph-heavy / vector-heavy / hybrid

Integration point: Called by SearchService before execute_hybrid_search().
Results passed as enhanced_query and search_hints to the pipeline.

Design principles:
  - Zero LLM dependency (all rule-based for speed and determinism)
  - Sub-millisecond latency (no external calls)
  - Graceful degradation: returns original query if enhancement fails
  - Domain-specific: AURIX TC3xx / AUTOSAR / MCAL terminology
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Config-driven default alpha (MEG_SW-308)
try:
    from env_config import get_default_search_alpha as _get_default_alpha
    _DEFAULT_SEARCH_ALPHA = _get_default_alpha()
except Exception:
    _DEFAULT_SEARCH_ALPHA = 0.6


# ═════════════════════════════════════════════════════════════════════════
#  Query Complexity
# ═════════════════════════════════════════════════════════════════════════

class QueryComplexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class SearchStrategy(str, Enum):
    GRAPH_HEAVY = "graph_heavy"     # alpha → 0.8 (mostly graph)
    VECTOR_HEAVY = "vector_heavy"   # alpha → 0.3 (mostly vector)
    HYBRID = "hybrid"               # alpha → 0.6 (balanced)
    EXACT = "exact"                 # alpha → 1.0 (entity lookup only)


# ═════════════════════════════════════════════════════════════════════════
#  Domain Synonym Dictionary
# ═════════════════════════════════════════════════════════════════════════

# AUTOSAR / MCAL / iLLD synonyms and abbreviations
# Key = canonical term, Value = set of aliases that should also be searched
_DOMAIN_SYNONYMS: Dict[str, Set[str]] = {
    # Module abbreviations
    "adc": {"analog to digital converter", "analog digital", "adc driver"},
    "can": {"controller area network", "can driver", "canfd", "can fd"},
    "spi": {"serial peripheral interface", "spi driver", "qspi"},
    "uart": {"universal asynchronous", "asclin", "serial communication"},
    "eth": {"ethernet", "ethernet driver", "geth"},
    "fls": {"flash", "flash driver", "data flash", "program flash", "dflash", "pflash"},
    "gpt": {"general purpose timer", "gpt driver", "gpt12"},
    "icu": {"input capture unit", "icu driver"},
    "mcu": {"microcontroller unit", "mcu driver", "clock", "reset"},
    "pwm": {"pulse width modulation", "pwm driver", "gtm", "tom", "atom"},
    "wdg": {"watchdog", "watchdog driver", "safety watchdog", "cpu watchdog"},
    "dio": {"digital input output", "dio driver", "gpio", "port"},
    "i2c": {"inter integrated circuit", "i2c driver"},
    "dma": {"direct memory access", "dma driver"},
    "eep": {"eeprom", "eeprom driver"},
    "lin": {"local interconnect network", "lin driver"},
    "fr": {"flexray", "flexray driver"},
    "crc": {"cyclic redundancy check"},

    # AUTOSAR concepts
    "autosar": {"autosar classic", "autosar cp"},
    "mcal": {"microcontroller abstraction layer", "mcal driver", "mcal module"},
    "bsw": {"basic software", "basic sw"},
    "swc": {"software component"},
    "rte": {"runtime environment"},
    "os": {"operating system", "autosar os"},
    "det": {"default error tracer", "development error"},
    "dem": {"diagnostic event manager"},
    "dcm": {"diagnostic communication manager"},

    # Architecture patterns
    "init": {"initialization", "initialise", "initialize", "startup", "init sequence"},
    "deinit": {"deinitialization", "deinitialize", "shutdown", "teardown"},
    "isr": {"interrupt service routine", "interrupt handler", "irq"},
    "callback": {"notification", "notification callback", "callout"},
    "polling": {"status polling", "poll", "busy wait"},

    # Safety / Quality
    "asil": {"automotive safety integrity level", "safety level", "safety rating"},
    "misra": {"misra c", "misra c 2012", "misra rules", "coding standard"},
    "aspice": {"a-spice", "automotive spice", "process assessment"},

    # Hardware
    "sfr": {"special function register", "register", "peripheral register"},
    "tc3xx": {"tc37x", "tc38x", "tc39x", "aurix", "tricore"},
    "trap": {"trap handler", "cpu trap", "exception"},
}

# Reverse lookup: alias → canonical term
_ALIAS_TO_CANONICAL: Dict[str, str] = {}
for _canonical, _aliases in _DOMAIN_SYNONYMS.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias.lower()] = _canonical

# ═════════════════════════════════════════════════════════════════════════
#  Structural query patterns (trigger graph-heavy strategy)
# ═════════════════════════════════════════════════════════════════════════

_STRUCTURAL_PATTERNS = [
    r"\bwhat\s+calls\b",
    r"\bcalled\s+by\b",
    r"\bdepend(?:s|encies|ency)\b",
    r"\btraceability\b",
    r"\brequirement.*(?:implement|trace|link|map)",
    r"\bimplement(?:s|ed|ation)\b.*requirement",
    r"\binit(?:ialization)?\s+(?:order|sequence|chain)\b",
    r"\bstruct(?:ure)?\s+(?:of|fields|members)\b",
    r"\brelationship(?:s)?\b",
    r"\bneighbor(?:s)?\b",
    r"\bparent(?:s)?\b",
    r"\bchild(?:ren)?\b",
    r"\bcoverage\s+gap",
    r"\bmissing\s+(?:test|requirement|link)",
    r"\bhow\s+many\b",
    r"\blist\s+all\b",
    r"\benumerate\b",
    r"\bcount\b.*\b(?:function|api|test|requirement)s?\b",
]
_STRUCTURAL_RE = [re.compile(p, re.IGNORECASE) for p in _STRUCTURAL_PATTERNS]

# Entity-like patterns (trigger exact lookup)
_ENTITY_PATTERNS = [
    r"\bIfx[A-Z][a-zA-Z_]+\b",             # iLLD function names
    r"\b[A-Z][a-z]+_(?:Init|DeInit|Read|Write|GetStatus)\b",  # AUTOSAR API names
    r"\b(?:ADC|CAN|SPI|UART|ETH|FLS|GPT|ICU|MCU|PWM|WDG|DIO)_\w+\b",  # Module-prefixed
    r"\b[A-Z]{2,}_REQ_\d+\b",              # Requirement IDs
    r"\bSWUD_\w+\b",                        # SWUD function references
]
_ENTITY_RE = [re.compile(p) for p in _ENTITY_PATTERNS]

# Semantic patterns (trigger vector-heavy strategy)
_SEMANTIC_PATTERNS = [
    r"\bhow\s+(?:to|do|does|can)\b",
    r"\bexplain\b",
    r"\bdescribe\b",
    r"\bwhat\s+is\b",
    r"\bwhy\b",
    r"\bbest\s+practice",
    r"\bexample\b",
    r"\btutorial\b",
    r"\bguide\b",
    r"\bconfigure\s+(?:the|a)?\s*(?:baud|clock|timer|interrupt)",
]
_SEMANTIC_RE = [re.compile(p, re.IGNORECASE) for p in _SEMANTIC_PATTERNS]


# ═════════════════════════════════════════════════════════════════════════
#  Data classes
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class EnhancedQuery:
    """Result of query enhancement."""
    original_query: str
    enhanced_query: str
    synonyms_added: List[str] = field(default_factory=list)
    complexity: QueryComplexity = QueryComplexity.SIMPLE
    strategy: SearchStrategy = SearchStrategy.HYBRID
    suggested_alpha: float = _DEFAULT_SEARCH_ALPHA
    suggested_max_results: int = 10
    detected_entities: List[str] = field(default_factory=list)
    detected_modules: List[str] = field(default_factory=list)
    is_aggregation: bool = False
    token_budget_hint: int = 8000

    def as_dict(self) -> Dict[str, Any]:
        return {
            "original_query": self.original_query,
            "enhanced_query": self.enhanced_query,
            "synonyms_added": self.synonyms_added,
            "complexity": self.complexity.value,
            "strategy": self.strategy.value,
            "suggested_alpha": self.suggested_alpha,
            "suggested_max_results": self.suggested_max_results,
            "detected_entities": self.detected_entities,
            "detected_modules": self.detected_modules,
            "is_aggregation": self.is_aggregation,
            "token_budget_hint": self.token_budget_hint,
        }


# ═════════════════════════════════════════════════════════════════════════
#  QueryEnhancer
# ═════════════════════════════════════════════════════════════════════════

class QueryEnhancer:
    """
    Rule-based query enhancement for AICE hybrid search.

    Pipeline:
      1. Detect named entities (iLLD/AUTOSAR function names, requirement IDs)
      2. Detect module references
      3. Expand domain synonyms (rule-based, sub-ms)
      4. Classify query complexity
      5. Predict optimal search strategy and alpha
      6. Set dynamic token budget hint
      7. (NEW v2) Optional LLM expansion for complex queries (ExpandR pattern)

    Steps 1-6 are deterministic and sub-millisecond.
    Step 7 adds ~100ms for complex queries only (~10% of traffic).
    """

    def __init__(self, custom_synonyms: Optional[Dict[str, Set[str]]] = None,
                 llm_fn: Optional[Callable] = None):
        self._synonyms = dict(_DOMAIN_SYNONYMS)
        if custom_synonyms:
            for canonical, aliases in custom_synonyms.items():
                if canonical in self._synonyms:
                    self._synonyms[canonical].update(aliases)
                else:
                    self._synonyms[canonical] = aliases
        self._llm_fn = llm_fn  # Optional: for LLM expansion of complex queries

    def enhance(self, query: str) -> EnhancedQuery:
        """
        Enhance a query with domain knowledge.

        Parameters
        ----------
        query : str
            Raw user/DA query.

        Returns
        -------
        EnhancedQuery with enhanced_query, complexity, strategy, and hints.
        """
        if not query or not query.strip():
            return EnhancedQuery(
                original_query=query or "",
                enhanced_query=query or "",
            )

        try:
            q = query.strip()

            # Step 1: Detect named entities
            entities = self._detect_entities(q)

            # Step 2: Detect module references
            modules = self._detect_modules(q)

            # Step 3: Expand domain synonyms
            expanded, synonyms_added = self._expand_synonyms(q)

            # Step 4: Classify complexity
            complexity = self._classify_complexity(q, entities, modules)

            # Step 5: Predict search strategy
            strategy, alpha = self._predict_strategy(q, entities, complexity)

            # Step 6: Aggregation detection
            is_agg = self._is_aggregation(q)

            # Step 7: Dynamic token budget
            budget = self._compute_token_budget(complexity)

            # Step 8: Max results hint
            max_results = self._compute_max_results(complexity, is_agg)

            # Step 9: LLM expansion for complex queries (ExpandR pattern, ~100ms)
            llm_expansions = []
            if complexity == QueryComplexity.COMPLEX and self._llm_fn:
                try:
                    llm_expansions = self._expand_with_llm(query)
                    if llm_expansions:
                        expanded = expanded + " [llm_expanded: " + "; ".join(llm_expansions[:3]) + "]"
                        synonyms_added.extend(llm_expansions[:3])
                except Exception as exc:
                    logger.debug("LLM expansion failed (non-critical): %s", exc)

            return EnhancedQuery(
                original_query=query,
                enhanced_query=expanded,
                synonyms_added=synonyms_added,
                complexity=complexity,
                strategy=strategy,
                suggested_alpha=alpha,
                suggested_max_results=max_results,
                detected_entities=entities,
                detected_modules=modules,
                is_aggregation=is_agg,
                token_budget_hint=budget,
            )

        except Exception as exc:
            logger.warning("Query enhancement failed, returning original: %s", exc)
            return EnhancedQuery(
                original_query=query,
                enhanced_query=query,
            )

    # ── Detection ─────────────────────────────────────────────────────

    def _detect_entities(self, query: str) -> List[str]:
        """Detect named entities (function names, requirement IDs, etc.)."""
        entities = []
        for pattern in _ENTITY_RE:
            for match in pattern.finditer(query):
                ent = match.group()
                if ent not in entities:
                    entities.append(ent)
        return entities

    def _detect_modules(self, query: str) -> List[str]:
        """Detect MCAL module references."""
        known_modules = {
            "ADC", "CAN", "SPI", "UART", "ETH", "FLS", "GPT", "ICU",
            "MCU", "PWM", "WDG", "DIO", "I2C", "DMA", "EEP", "LIN",
            "FR", "CRC", "CXPI", "SENT",
        }
        q_upper = query.upper()
        found = []
        for mod in known_modules:
            # Match as whole word (not substring)
            if re.search(rf'\b{mod}\b', q_upper):
                found.append(mod)
        return found

    # ── Synonym expansion ─────────────────────────────────────────────

    def _expand_synonyms(self, query: str) -> Tuple[str, List[str]]:
        """Expand query with domain synonyms. Returns (expanded_query, synonyms_added)."""
        q_lower = query.lower()
        additions = []

        for canonical, aliases in self._synonyms.items():
            # Check if canonical term appears in query
            if re.search(rf'\b{re.escape(canonical)}\b', q_lower):
                # Add the most common alias as expansion
                best_alias = min(aliases, key=len)  # shortest alias as most precise
                if best_alias.lower() not in q_lower:
                    additions.append(best_alias)
            else:
                # Check if any alias appears in query
                for alias in aliases:
                    if alias.lower() in q_lower and len(alias) > 3:
                        if canonical not in q_lower:
                            additions.append(canonical)
                        break

        if additions:
            # Append synonyms as context hints (not modifying original intent)
            expanded = query + " [context: " + ", ".join(additions[:5]) + "]"
            return expanded, additions[:5]

        return query, []

    # ── Complexity classification ─────────────────────────────────────

    def _classify_complexity(
        self,
        query: str,
        entities: List[str],
        modules: List[str],
    ) -> QueryComplexity:
        """Classify query complexity using heuristics."""
        score = 0

        # Word count
        words = query.split()
        if len(words) > 20:
            score += 2
        elif len(words) > 10:
            score += 1

        # Multiple entities
        if len(entities) >= 3:
            score += 2
        elif len(entities) >= 1:
            score += 1

        # Multiple modules
        if len(modules) >= 2:
            score += 2

        # Register-level keywords
        register_kw = ["register", "sfr", "bit field", "bit mask", "offset", "base address"]
        if any(kw in query.lower() for kw in register_kw):
            score += 1

        # ASIL/safety keywords
        safety_kw = ["asil", "safety", "iso 26262", "functional safety", "fmea"]
        if any(kw in query.lower() for kw in safety_kw):
            score += 1

        # Multi-part questions
        if query.count("?") > 1 or " and " in query.lower():
            score += 1

        # Comparison / analysis keywords
        analysis_kw = ["compare", "difference", "versus", "vs", "trade-off", "pros and cons"]
        if any(kw in query.lower() for kw in analysis_kw):
            score += 2

        if score >= 4:
            return QueryComplexity.COMPLEX
        elif score >= 2:
            return QueryComplexity.MEDIUM
        return QueryComplexity.SIMPLE

    # ── Strategy prediction ───────────────────────────────────────────

    def _predict_strategy(
        self,
        query: str,
        entities: List[str],
        complexity: QueryComplexity,
    ) -> Tuple[SearchStrategy, float]:
        """Predict optimal search strategy and alpha value."""

        # If query contains specific entity names → exact lookup
        if len(entities) >= 1 and len(query.split()) <= 5:
            return SearchStrategy.EXACT, 1.0

        # Structural query patterns → graph-heavy
        structural_hits = sum(1 for rx in _STRUCTURAL_RE if rx.search(query))
        if structural_hits >= 2:
            return SearchStrategy.GRAPH_HEAVY, 0.85

        # Semantic patterns → vector-heavy
        semantic_hits = sum(1 for rx in _SEMANTIC_RE if rx.search(query))
        if semantic_hits >= 2 and not entities:
            return SearchStrategy.VECTOR_HEAVY, 0.3

        # Single structural pattern with entities → hybrid but graph-leaning
        if structural_hits >= 1 and entities:
            return SearchStrategy.GRAPH_HEAVY, 0.75

        # Single semantic pattern → hybrid but vector-leaning
        if semantic_hits >= 1:
            return SearchStrategy.VECTOR_HEAVY, 0.4

        # Default: balanced hybrid
        return SearchStrategy.HYBRID, 0.6

    # ── Aggregation detection ─────────────────────────────────────────

    def _is_aggregation(self, query: str) -> bool:
        """Detect aggregation/enumeration queries."""
        agg_patterns = [
            r"\blist\s+all\b", r"\bhow\s+many\b", r"\bcount\b",
            r"\benumerate\b", r"\bshow\s+all\b", r"\bget\s+all\b",
        ]
        q_lower = query.lower()
        return any(re.search(p, q_lower) for p in agg_patterns)

    # ── Token budget ──────────────────────────────────────────────────

    @staticmethod
    def _compute_token_budget(complexity: QueryComplexity) -> int:
        """Dynamic token budget based on query complexity."""
        budgets = {
            QueryComplexity.SIMPLE: 4000,
            QueryComplexity.MEDIUM: 8000,
            QueryComplexity.COMPLEX: 12000,
        }
        return budgets[complexity]

    @staticmethod
    def _compute_max_results(complexity: QueryComplexity, is_agg: bool) -> int:
        """Suggest max_results based on complexity."""
        if is_agg:
            return 50  # aggregation queries need more results
        results = {
            QueryComplexity.SIMPLE: 5,
            QueryComplexity.MEDIUM: 10,
            QueryComplexity.COMPLEX: 20,
        }
        return results[complexity]

    # ── LLM Expansion (ExpandR pattern) ────────────────────────────────

    _LLM_EXPAND_PROMPT = """You are a query expansion expert for AURIX TC3xx automotive embedded software.
Given a complex engineering query, generate 3 domain-specific reformulations that
would help retrieve different aspects of the answer from a knowledge graph.
Each reformulation should target: (1) code/API aspects, (2) register/HW aspects,
(3) requirement/safety aspects.
Expand acronyms. Add domain synonyms. Be specific to AUTOSAR/MCAL/iLLD.
Respond ONLY with JSON array of 3 strings: ["reformulation1", "reformulation2", "reformulation3"]"""

    def _expand_with_llm(self, query: str) -> List[str]:
        """LLM-based query expansion for complex queries (ExpandR-style)."""
        if not self._llm_fn:
            return []

        try:
            response = self._llm_fn(
                system=self._LLM_EXPAND_PROMPT,
                user=f"Query to expand: {query}",
                max_tokens=300,
            )
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(r) for r in parsed[:3] if r]
        except Exception as exc:
            logger.debug("LLM expansion parse failed: %s", exc)
        return []
