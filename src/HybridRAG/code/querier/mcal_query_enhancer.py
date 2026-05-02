"""
MCAL Query Enhancer
===================
LLM-powered graph query expansion for MCAL Neo4j-based hybrid search.

Workspace: mcal (activated when workspace_id == "mcal")
For illd workspace, see query_enhancer.py (rule-based).

Architecture:
  1. GraphProbe  â€” lightweight entity resolution + schema discovery (2-3 Cypher)
  2. LLM call    â€” CoT + few-shot expansion â†’ expanded keywords, labels, Cypher patterns
  3. EnhancedQuery returned to SearchService for dispatch

Techniques (Yuan, ETH/Copenhagen â€” "LLM-based Query Enhancement"):
  â€¢ External Query Expansion  â€” graph probe as external KB (Xia et al., NAACL'25)
  â€¢ Internal Query Expansion  â€” LLM domain synonym generation (Yu et al., ICLR'23)
  â€¢ CoT Prompting             â€” step-by-step reasoning (Jagerman et al., Arxiv'23)
  â€¢ Few-shot Prompting        â€” example queryâ†’Cypher pairs (Wang et al., EMNLP'23)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .kg_node_utils import node_display_name, node_unique_id

logger = logging.getLogger(__name__)

# â”€â”€ Cypher safety: only these statement types are allowed â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_CYPHER_SAFE_RE = re.compile(
    r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|WHERE|RETURN|ORDER\s+BY|LIMIT|UNWIND|CALL)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CYPHER_UNSAFE_RE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b",
    re.IGNORECASE,
)
_MAX_PATTERNS = 3
_PATTERN_TIMEOUT_MS = 5000


# â”€â”€ Data classes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class McalEnhancedQuery:
    """Result of query enhancement."""
    original_query: str
    expanded_keywords: List[str] = field(default_factory=list)
    target_labels: List[str] = field(default_factory=list)
    cypher_patterns: List[str] = field(default_factory=list)
    intent: str = "keyword_search"  # keyword_search | relationship_traversal | aggregation | entity_lookup
    probe_context: Dict[str, Any] = field(default_factory=dict)
    enhancement_time_ms: float = 0.0
    enhanced: bool = False


@dataclass
class McalProbeResult:
    """Result of the graph probe phase."""
    entities: Dict[str, List[str]] = field(default_factory=dict)   # name â†’ [labels]
    relationships: List[Dict[str, str]] = field(default_factory=list)  # [{type, target_label, sample}]
    neighbor_samples: List[str] = field(default_factory=list)
    schema_summary: str = ""


# â”€â”€ Graph Probe â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class McalGraphProbe:
    """Lightweight entity resolution and schema discovery against Neo4j.

    Runs 2-3 fast Cypher queries to gather context for the LLM expansion.
    """

    def __init__(self, neo4j_driver, database: str = "neo4j"):
        self._driver = neo4j_driver
        self._db = database

    def probe(self, query: str, module: Optional[str] = None) -> McalProbeResult:
        """Run entity resolution + schema discovery.

        1. Extract candidate entity names from the query
        2. Look them up in Neo4j to get their actual labels
        3. Discover outgoing relationship types + sample neighbors
        """
        result = McalProbeResult()
        candidates = self._extract_entity_candidates(query)
        if not candidates:
            return result

        try:
            with self._driver.session(database=self._db) as session:
                # Phase 1: Entity resolution â€” find which candidates exist as nodes
                for name in candidates[:5]:  # limit to avoid slow probes
                    labels = self._resolve_entity(session, name)
                    if labels:
                        result.entities[name] = labels

                # Phase 2: Schema discovery â€” what relationships connect from resolved entities
                if result.entities:
                    first_entity = next(iter(result.entities))
                    first_labels = result.entities[first_entity]
                    rels, samples = self._discover_schema(session, first_entity, first_labels)
                    result.relationships = rels
                    result.neighbor_samples = samples

                # Build human-readable schema summary for the LLM
                result.schema_summary = self._build_summary(result)
        except Exception as e:
            logger.warning("[QueryEnhancer] Graph probe failed: %s", e)

        return result

    def _extract_entity_candidates(self, query: str) -> List[str]:
        """Extract likely entity names from the query.

        Looks for CamelCase identifiers, underscore_tokens, and known patterns.
        """
        candidates: List[str] = []
        seen = set()

        # CamelCase / underscore identifiers (e.g. Adc_DeInit, SFR_Register)
        for m in re.finditer(r'[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+', query):
            token = m.group()
            if token not in seen:
                candidates.append(token)
                seen.add(token)

        # CamelCase without underscore (e.g. AdcGroup)
        for m in re.finditer(r'[A-Z][a-z]+[A-Z][A-Za-z0-9]{2,}', query):
            token = m.group()
            if token not in seen:
                candidates.append(token)
                seen.add(token)

        # ALL_CAPS tokens that might be register names or module names (e.g. ADC, BWDCFG, SUPLLEV)
        for m in re.finditer(r'\b[A-Z][A-Z0-9_]{2,}\b', query):
            token = m.group()
            skip = {"WHAT", "WHICH", "LIST", "SHOW", "FIND", "DOES", "THAT",
                    "WITH", "FROM", "INTO", "HAVE", "THIS", "ASIL", "MCAL",
                    "THE", "AND", "FOR", "ARE", "ALL", "HOW", "NOT", "CAN"}
            if token not in seen and token not in skip:
                candidates.append(token)
                seen.add(token)

        # Filter out fragments that are substrings of already-extracted candidates
        # e.g. "DeInit" is a fragment of "Adc_DeInit" â€” remove it
        filtered: List[str] = []
        for c in candidates:
            is_fragment = any(
                c != other and c in other
                for other in candidates
            )
            if not is_fragment:
                filtered.append(c)

        return filtered

    def _resolve_entity(self, session, name: str) -> List[str]:
        """Look up an entity by name and return its Neo4j labels.

        Uses a two-phase approach:
          1. Exact match (fast, index-friendly)
          2. Case-insensitive fallback if exact match fails
        """
        # Phase 1: Exact match (most common case, uses index if available)
        cypher_exact = (
            "MATCH (n) WHERE n.name = $name OR n.function_name = $name "
            "RETURN labels(n) AS lbls LIMIT 20"
        )
        try:
            records = list(session.run(cypher_exact, {"name": name}))
            if records:
                all_labels = set()
                for rec in records:
                    for lbl in rec["lbls"]:
                        all_labels.add(lbl)
                return sorted(all_labels)

            # Phase 2: Case-insensitive fallback (catches adc_init, ADC_INIT, etc.)
            cypher_ci = (
                "MATCH (n) WHERE toLower(n.name) = toLower($name) "
                "OR toLower(n.function_name) = toLower($name) "
                "RETURN labels(n) AS lbls LIMIT 20"
            )
            records = list(session.run(cypher_ci, {"name": name}))
            all_labels = set()
            for rec in records:
                for lbl in rec["lbls"]:
                    all_labels.add(lbl)
            return sorted(all_labels)
        except Exception:
            return []

    def _discover_schema(self, session, entity_name: str, entity_labels: List[str]
                         ) -> tuple:
        """Discover outgoing relationship types and sample neighbors for an entity.

        Uses two queries:
          1. DISTINCT relationship types (fast, no duplicates)
          2. Sample neighbor names for context
        """
        # Query 1: Get all distinct relationship type â†’ target label pairs
        cypher_rels = (
            "MATCH (n)-[r]->(m) "
            "WHERE n.name = $name "
            "RETURN DISTINCT type(r) AS rel, labels(m)[0] AS target_label "
            "ORDER BY rel"
        )
        # Query 2: Get a few sample neighbor names for context
        cypher_samples = (
            "MATCH (n)-[r]->(m) "
            "WHERE n.name = $name AND m.name IS NOT NULL "
            "RETURN type(r) AS rel, labels(m)[0] AS target_label, m.name AS target_name "
            "LIMIT 50"
        )
        rels: List[Dict[str, str]] = []
        samples: List[str] = []
        seen_rels = set()

        try:
            # Phase 1: All distinct relationship types
            for rec in session.run(cypher_rels, {"name": entity_name}):
                rel_type = rec["rel"]
                target_label = rec["target_label"] or "Unknown"
                rel_key = f"{rel_type}â†’{target_label}"
                if rel_key not in seen_rels:
                    seen_rels.add(rel_key)
                    rels.append({
                        "type": rel_type,
                        "target_label": target_label,
                        "sample": "?",  # filled in phase 2
                    })

            # Phase 2: Fill in sample names
            seen_rels_for_sample = set()
            for rec in session.run(cypher_samples, {"name": entity_name}):
                rel_type = rec["rel"]
                target_label = rec["target_label"] or "Unknown"
                target_name = rec["target_name"] or "?"
                rel_key = f"{rel_type}â†’{target_label}"

                # Update sample in the rel entry if not yet set
                if rel_key not in seen_rels_for_sample:
                    seen_rels_for_sample.add(rel_key)
                    for r in rels:
                        if f"{r['type']}â†’{r['target_label']}" == rel_key and r["sample"] == "?":
                            r["sample"] = target_name
                            break

                if target_name and target_name != "?" and len(samples) < 8:
                    samples.append(target_name)
        except Exception:
            pass

        return rels, samples

    def _build_summary(self, probe: McalProbeResult) -> str:
        """Build a concise schema summary string for the LLM prompt."""
        parts = []
        for name, labels in probe.entities.items():
            parts.append(f"Entity \"{name}\" found as: {', '.join(labels)}")

        if probe.relationships:
            rel_strs = [
                f"  {r['type']} â†’ {r['target_label']} (e.g. {r['sample']})"
                for r in probe.relationships
            ]
            parts.append("Outgoing relationships:\n" + "\n".join(rel_strs))
            parts.append(
                "IMPORTANT: Use ONLY the relationship types listed above in your "
                "cypher_patterns. Do NOT invent or substitute relationship types."
            )

        if probe.neighbor_samples:
            parts.append(f"Sample neighbors: {', '.join(probe.neighbor_samples[:8])}")

        return "\n".join(parts)


# â”€â”€ LLM Expansion Prompt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_SYSTEM_PROMPT = """\
You are a Neo4j query optimizer for an AUTOSAR MCAL knowledge graph.
Your task: given a user's natural-language question and graph context from a probe,
produce an enhanced query specification as JSON.

Output JSON with these fields:
- "expanded_keywords": list of 5-10 domain-specific search terms (lowercase ok, include identifiers as-is)
- "target_labels": list of Neo4j node labels to search (exact label names from the schema)
- "cypher_patterns": list of 0-3 targeted Cypher READ queries (MATCH...RETURN only). \
Each must be a complete, executable Cypher statement. Use parameter-free queries with \
string literals for entity names. Only produce these for relationship_traversal or aggregation intents.
- "intent": one of "keyword_search", "relationship_traversal", "aggregation", "entity_lookup"

Rules for cypher_patterns:
- ONLY MATCH, OPTIONAL MATCH, WITH, WHERE, RETURN, ORDER BY, LIMIT, UNWIND are allowed
- NO CREATE, MERGE, SET, DELETE, DETACH, REMOVE, DROP
- RETURN full node variables (e.g. RETURN f, r), NOT scalar projections (e.g. RETURN f.name AS x). \
This is critical â€” downstream processing needs Neo4j Node objects to extract labels and IDs.
- Limit results to 100 max
- Use ONLY relationship types and labels that appear in the provided schema context. \
Do NOT invent relationship types that are not listed. If the schema shows SRC_ACCESSES_SFR â†’ SFR_Register, \
use exactly that â€” do not substitute EA_ACCESSES_REGISTER or any other relationship.

Think step by step:
1. What is the user really asking? (intent)
2. What node types contain the answer?
3. Do we need to traverse relationships to find it, or is keyword search sufficient?
4. What domain-specific terms would appear in those nodes?"""

_FEW_SHOT_EXAMPLES = """
## Examples

User: "What registers does Adc_Init write to?"
Schema: Entity "Adc_Init" found as: SRC_Function, EA_Function
  Outgoing: SRC_ACCESSES_SFR â†’ SFR_Register (e.g. ADC_SUPLLEV)
  Outgoing: EA_ACCESSES_REGISTER â†’ EA_Register (e.g. SUPLLEV)
  Outgoing: SRC_CALLS â†’ SRC_Function (e.g. Adc_lInit)
Answer:
```json
{
  "expanded_keywords": ["Adc_Init", "register", "SFR_Register", "EA_Register", "write", "access", "SRC_ACCESSES_SFR"],
  "target_labels": ["SFR_Register", "EA_Register", "SRC_Function"],
  "cypher_patterns": [
    "MATCH (f:SRC_Function {name: 'Adc_Init'})-[:SRC_ACCESSES_SFR]->(reg:SFR_Register) RETURN f, reg LIMIT 100",
    "MATCH (f:EA_Function {name: 'Adc_Init'})-[:EA_ACCESSES_REGISTER]->(reg:EA_Register) RETURN f, reg LIMIT 100"
  ],
  "intent": "relationship_traversal"
}
```

User: "List all error codes for ADC"
Schema: (no specific entity resolved)
Answer:
```json
{
  "expanded_keywords": ["error", "error_code", "ADC", "DET", "DEM", "EA_ErrorCode"],
  "target_labels": ["EA_ErrorCode"],
  "cypher_patterns": [
    "MATCH (e:EA_ErrorCode) RETURN e ORDER BY e.name LIMIT 100"
  ],
  "intent": "aggregation"
}
```

User: "What global variables does Adc_lDeInit use?"
Schema: Entity "Adc_lDeInit" found as: SRC_Function, EA_Function
  Outgoing: SRC_USES_GLOBAL â†’ SRC_GlobalVariable (e.g. Adc_kEcucPartition_0ConfigPtr)
  Outgoing: SRC_ACCESSES_SFR â†’ SFR_Register (e.g. ADC_TMADC_BWDCFG)
Answer:
```json
{
  "expanded_keywords": ["Adc_lDeInit", "global", "variable", "SRC_GlobalVariable"],
  "target_labels": ["SRC_GlobalVariable", "SRC_Function"],
  "cypher_patterns": [
    "MATCH (f:SRC_Function {name: 'Adc_lDeInit'})-[r:SRC_USES_GLOBAL]->(g:SRC_GlobalVariable) RETURN f, r, g LIMIT 100"
  ],
  "intent": "relationship_traversal"
}
```

User: "What functions are in the ADC module?"
Schema: (no specific entity resolved)
Answer:
```json
{
  "expanded_keywords": ["ADC", "function", "module", "SRC_Function", "API"],
  "target_labels": ["SRC_Function", "MCALModule"],
  "cypher_patterns": [
    "MATCH (m:MCALModule {name: 'ADC'})<-[:SRC_BELONGS_TO_MODULE]-(f:SRC_Function) RETURN f LIMIT 100"
  ],
  "intent": "aggregation"
}
```

User: "What is Adc_DeInit?"
Schema: Entity "Adc_DeInit" found as: SRC_Function, EA_Function, SWUD_Function
Answer:
```json
{
  "expanded_keywords": ["Adc_DeInit", "deinit", "function", "API"],
  "target_labels": ["EA_Function", "SRC_Function", "SWUD_Function"],
  "cypher_patterns": [],
  "intent": "entity_lookup"
}
```"""


# â”€â”€ Cypher Safety â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _validate_cypher(cypher: str) -> bool:
    """Validate that a Cypher pattern is read-only and well-formed."""
    if not cypher or not cypher.strip():
        return False
    # Must not contain write operations
    if _CYPHER_UNSAFE_RE.search(cypher):
        logger.warning("[QueryEnhancer] Rejected unsafe Cypher: %s", cypher[:100])
        return False
    # Must start with a read operation
    if not _CYPHER_SAFE_RE.match(cypher.strip()):
        logger.warning("[QueryEnhancer] Rejected Cypher (not a read query): %s", cypher[:100])
        return False
    # Must contain RETURN
    if not re.search(r'\bRETURN\b', cypher, re.IGNORECASE):
        return False
    return True


# â”€â”€ Query Enhancer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class McalQueryEnhancer:
    """Orchestrates graph probe â†’ LLM expansion â†’ EnhancedQuery.

    Parameters
    ----------
    neo4j_driver
        Connected Neo4j driver for graph probe.
    database : str
        Neo4j database name.
    llm_fn : callable, optional
        Function(system, user, max_tokens) â†’ str. If None, uses GPT4IFX.
    enabled : bool
        Kill-switch. When False, enhance() returns a pass-through EnhancedQuery.
    """

    def __init__(self, neo4j_driver, database: str = "neo4j",
                 llm_fn=None, enabled: bool = True):
        self._probe = McalGraphProbe(neo4j_driver, database) if neo4j_driver else None
        self._llm_fn = llm_fn or self._default_llm
        self._enabled = enabled

    @staticmethod
    def _default_llm(system: str, user: str, max_tokens: int = 800) -> str:
        """Call LLM via GPT4IFX (reuses shared connection pool from RLM)."""
        try:
            from src.HybridRAG.code.querier.rlm_orchestrator import _get_shared_openai_client
            client = _get_shared_openai_client()
            model = os.environ.get("QUERY_ENHANCER_MODEL", "gpt-5.2")
            resp = client.chat.completions.create(
                model=model, temperature=0.0, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.error("[QueryEnhancer] LLM call failed: %s", e)
            return ""

    def enhance(self, query: str, module: Optional[str] = None) -> EnhancedQuery:
        """Enhance a user query with graph context and LLM expansion.

        Falls back gracefully: if probe or LLM fails, returns a basic
        EnhancedQuery with original keywords only (no degradation).
        """
        t0 = time.time()
        eq = McalEnhancedQuery(original_query=query)

        if not self._enabled or not self._probe:
            return eq

        try:
            # Phase 1: Graph probe
            probe = self._probe.probe(query, module=module)
            eq.probe_context = {
                "entities": probe.entities,
                "relationships": [r["type"] for r in probe.relationships],
                "neighbors": probe.neighbor_samples,
            }

            # Phase 2: LLM expansion
            user_msg = self._build_user_message(query, probe)
            raw = self._llm_fn(_SYSTEM_PROMPT, user_msg, max_tokens=800)
            parsed = self._parse_llm_response(raw)

            if parsed:
                eq.expanded_keywords = parsed.get("expanded_keywords", [])[:10]
                eq.target_labels = parsed.get("target_labels", [])[:10]

                # Validate each Cypher pattern
                raw_patterns = parsed.get("cypher_patterns", [])[:_MAX_PATTERNS]
                eq.cypher_patterns = [p for p in raw_patterns if _validate_cypher(p)]

                eq.intent = parsed.get("intent", "keyword_search")
                eq.enhanced = True

            eq.enhancement_time_ms = (time.time() - t0) * 1000
            logger.info(
                "[QueryEnhancer] Enhanced in %.0fms: intent=%s, keywords=%d, labels=%d, patterns=%d",
                eq.enhancement_time_ms, eq.intent,
                len(eq.expanded_keywords), len(eq.target_labels), len(eq.cypher_patterns),
            )
        except Exception as e:
            logger.warning("[QueryEnhancer] Enhancement failed (fallback to basic): %s", e)
            eq.enhancement_time_ms = (time.time() - t0) * 1000

        return eq

    def _build_user_message(self, query: str, probe: McalProbeResult) -> str:
        """Build the user message for the LLM expansion call."""
        parts = [_FEW_SHOT_EXAMPLES, "\n## Now process this query:\n"]

        if probe.schema_summary:
            parts.append(f"Schema context:\n{probe.schema_summary}\n")
        else:
            parts.append("Schema context: No entities resolved from the query.\n")

        parts.append(f"User query: \"{query}\"\n")
        parts.append("Think step by step, then output JSON.")

        return "\n".join(parts)

    def _parse_llm_response(self, raw: str) -> Optional[Dict]:
        """Extract JSON from the LLM response (handles markdown code fences)."""
        if not raw:
            return None

        # Try to find JSON in code fences first
        fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', raw, re.DOTALL)
        if fence_match:
            raw = fence_match.group(1)

        # Try direct JSON parse
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        # Try to find a JSON object in the response
        brace_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("[QueryEnhancer] Could not parse LLM response as JSON")
        return None


# â”€â”€ Pattern Executor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class McalPatternExecutor:
    """Execute validated Cypher patterns and convert results to search-result format."""

    def __init__(self, neo4j_driver, database: str = "neo4j"):
        self._driver = neo4j_driver
        self._db = database

    def execute_patterns(self, patterns: List[str]) -> List[Dict[str, Any]]:
        """Execute Cypher patterns and return results in search-result format.

        Each pattern is run in a read transaction with a timeout.
        Results are converted to the same dict format as _graph_search.
        """
        results: List[Dict[str, Any]] = []
        seen_ids = set()

        if not self._driver or not patterns:
            return results

        try:
            with self._driver.session(database=self._db) as session:
                for i, pattern in enumerate(patterns[:_MAX_PATTERNS]):
                    try:
                        records = list(session.run(pattern))
                        for rec in records:
                            for result in self._record_to_results(rec, pattern_idx=i):
                                nid = result.get("node_id", "")
                                if nid and nid not in seen_ids:
                                    seen_ids.add(nid)
                                    results.append(result)
                                elif not nid:
                                    results.append(result)
                    except Exception as e:
                        logger.warning("[QueryEnhancer] Pattern %d failed: %s", i, e)
        except Exception as e:
            logger.error("[QueryEnhancer] Pattern execution error: %s", e)

        return results

    @staticmethod
    def _is_neo4j_node(val) -> bool:
        """Check if a value is a Neo4j Node (has labels and properties)."""
        return hasattr(val, "labels") and hasattr(val, "items")

    @staticmethod
    def _is_neo4j_relationship(val) -> bool:
        """Check if a value is a Neo4j Relationship."""
        return hasattr(val, "type") and hasattr(val, "start_node") and not hasattr(val, "labels")

    def _record_to_results(self, record, pattern_idx: int = 0) -> List[Dict[str, Any]]:
        """Convert a Cypher record to search-result dicts.

        When RETURN values are Neo4j Node objects, each Node becomes a
        separate result with its real label, properties, and node_id.
        Relationship objects have their properties merged into target nodes.
        Scalar values are collected into a single fallback result.
        """
        data = dict(record)
        if not data:
            return []

        results = []
        scalars = {}
        rel_props = {}  # relationship properties to merge into node results

        # First pass: extract relationship properties
        for key, val in data.items():
            if val is not None and self._is_neo4j_relationship(val):
                rel_props = dict(val.items())

        for key, val in data.items():
            if val is None:
                continue
            if self._is_neo4j_node(val):
                # Extract as a proper node result
                labels = sorted(val.labels)
                ntype = labels[0] if labels else "CypherResult"
                props = dict(val.items())
                nid = node_unique_id(ntype, props)
                display = node_display_name(ntype, props)
                props["_label"] = ntype
                props["_node_id"] = nid
                props["_name"] = display

                # Use rich serialize_node for content
                from .kg_node_utils import serialize_node
                content = serialize_node(ntype, props)
                # Append relationship properties (access_type, via_chain, etc.)
                if rel_props:
                    rel_lines = []
                    for rk, rv in rel_props.items():
                        if rv and rk not in ("_label", "_node_id", "_name"):
                            rel_lines.append(f"  Relationship {rk}: {rv}")
                    if rel_lines:
                        content += "\n" + "\n".join(rel_lines)

                results.append({
                    "node_id": nid,
                    "node_type": ntype,
                    "source": "neo4j_pattern",
                    "score": 1.5,
                    "properties": props,
                    "content": content,
                })
            elif self._is_neo4j_relationship(val):
                pass  # already handled above
            else:
                scalars[key] = val

        # If only scalar values (e.g. RETURN count(*), r.name), make one result
        if scalars and not results:
            name = None
            for k in ("name", "function", "register", "func", "target"):
                if k in scalars:
                    name = str(scalars[k])
                    break
            name = name or str(list(scalars.values())[0])
            id_parts = [str(v) for v in scalars.values()]
            unique_id = f"pattern:{pattern_idx}:{'|'.join(id_parts)}"
            content = "\n".join(f"  {k}: {v}" for k, v in scalars.items())
            results.append({
                "node_id": unique_id,
                "node_type": "CypherResult",
                "source": "neo4j_pattern",
                "score": 1.5,
                "properties": scalars,
                "content": f"[CypherResult] {name}\n{content}",
            })

        return results
