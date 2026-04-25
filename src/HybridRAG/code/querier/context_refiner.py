"""
Context Refiner — GAP-A07 (Research Upgrade v2)
==================================================
**Research Upgrade:** Added Corrective RAG (HuskyInSalt/CRAG) threshold
validation and Self-RAG (AkariAsai/self-rag) reflection tokens.

Corrective RAG: Each agent result is validated against a relevance threshold
before acceptance. Below-threshold results trigger corrective re-queries with
adjusted parameters (not just re-running the same query).

Self-RAG: During RLM synthesis, the LLM is prompted to self-assess whether
additional retrieval is needed. Reflection tokens [Retrieve], [IsRelevant],
[IsSupportive] guide the decision.

Architecture:
  Complex query → Coordinator → Specialist agents
    → CRAG threshold validation (NEW) → corrective re-query if needed
    → Validator → Self-RAG reflection (NEW) → optional additional retrieval
    → Refined context

Max 3 iterations, 2000 token budget cap.
Only for "complex" queries (~10% of traffic).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

REFINER_ENABLED = os.getenv("CONTEXT_REFINER_ENABLED", "true").lower() == "true"
MAX_ITERATIONS = int(os.getenv("REFINER_MAX_ITERATIONS", "3"))
AGENT_TOKEN_BUDGET = int(os.getenv("REFINER_AGENT_BUDGET", "2000"))
CRAG_RELEVANCE_THRESHOLD = float(os.getenv("CRAG_RELEVANCE_THRESHOLD", "0.4"))
SELF_RAG_ENABLED = os.getenv("SELF_RAG_ENABLED", "true").lower() == "true"


class AgentType(str, Enum):
    CODE = "code"
    REGISTER = "register"
    REQUIREMENT = "requirement"
    SAFETY = "safety"
    COORDINATOR = "coordinator"
    VALIDATOR = "validator"


_AGENT_PROMPTS = {
    AgentType.COORDINATOR: """You are the coordinator for an AURIX TC3xx knowledge system.
Given a complex query and context chunks, identify:
1. Which specialists are needed (code, register, requirement, safety)
2. What context is missing
3. Priority order
Respond JSON: {"agents_needed": ["code", "register"], "missing_context": ["desc"], "priority": "high"}""",

    AgentType.CODE: """You are a code specialist for AURIX TC3xx / iLLD / AUTOSAR MCAL.
Identify: missing function signatures, incomplete dependency chains, missing init sequences.
Respond JSON: {"gaps": ["desc"], "suggested_queries": ["query"], "completeness": 0.0-1.0}""",

    AgentType.REGISTER: """You are a register specialist for AURIX TC3xx peripherals.
Identify: missing register fields, incomplete bit-field mappings, missing base addresses.
Respond JSON: {"gaps": ["desc"], "suggested_queries": ["query"], "completeness": 0.0-1.0}""",

    AgentType.REQUIREMENT: """You are a requirement specialist for AUTOSAR/ASPICE traceability.
Identify: missing trace links, incomplete V-Model coverage, unlinked test cases.
Respond JSON: {"gaps": ["desc"], "suggested_queries": ["query"], "completeness": 0.0-1.0}""",

    AgentType.SAFETY: """You are a safety specialist for ISO 26262 / ASIL compliance.
Identify: missing ASIL decomposition, incomplete safety mechanisms, missing FMEA references.
Respond JSON: {"gaps": ["desc"], "suggested_queries": ["query"], "completeness": 0.0-1.0}""",

    AgentType.VALIDATOR: """You are a validation agent. Assess the refined context:
1. completeness (0-1): Does it answer the query?
2. consistency (0-1): Any contradictions?
3. needs_iteration: Should we refine more?
4. needs_retrieval: Does the LLM need additional external knowledge? (Self-RAG)
Respond JSON: {"completeness": 0.0-1.0, "consistency": 0.0-1.0, "needs_iteration": bool, "needs_retrieval": bool, "retrieval_query": "optional query if needs_retrieval", "reason": "explanation"}""",
}

# Self-RAG reflection prompt (added to synthesis step)
SELF_RAG_REFLECTION_PROMPT = """Before finalizing, assess your response:
- [IsRelevant]: Does every piece of context directly address the query?
- [IsSupportive]: Does the context support all claims in your response?
- [NeedsRetrieval]: Is there a specific piece of information missing that would significantly improve the answer?

If NeedsRetrieval=Yes, state EXACTLY what information is needed in one search query.
Respond JSON: {"is_relevant": bool, "is_supportive": bool, "needs_retrieval": bool, "retrieval_query": "query or empty"}"""


@dataclass
class AgentResult:
    agent_type: AgentType
    gaps: List[str] = field(default_factory=list)
    suggested_queries: List[str] = field(default_factory=list)
    completeness: float = 0.0
    raw_response: str = ""
    tokens_used: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None
    # CRAG: was this result accepted after threshold check?
    crag_accepted: bool = True
    crag_score: float = 1.0


@dataclass
class RefinementResult:
    refined_items: List[Dict[str, Any]]
    iterations: int
    agents_used: List[str]
    gaps_found: List[str]
    gaps_resolved: List[str]
    additional_queries: List[str]
    completeness_score: float
    total_tokens_used: int
    latency_ms: float
    refined: bool = True
    crag_corrections: int = 0
    self_rag_retrievals: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "refined": self.refined, "iterations": self.iterations,
            "agents_used": self.agents_used,
            "gaps_found": len(self.gaps_found),
            "gaps_resolved": len(self.gaps_resolved),
            "completeness_score": round(self.completeness_score, 2),
            "total_tokens_used": self.total_tokens_used,
            "latency_ms": round(self.latency_ms, 2),
            "crag_corrections": self.crag_corrections,
            "self_rag_retrievals": self.self_rag_retrievals,
        }


class ContextRefiner:
    """
    Multi-agent context refinement with Corrective RAG + Self-RAG.

    Pipeline:
      1. Coordinator plans refinement
      2. Specialist agents analyze gaps
      3. CRAG: Validate each agent's output against relevance threshold
      4. Corrective re-query for below-threshold results
      5. Gap-filling searches
      6. Validator checks completeness + Self-RAG reflection
      7. If needs_retrieval → one more targeted search
      8. Repeat if validator says needs_iteration (max 3)
    """

    def __init__(self, llm_fn: Optional[Callable] = None,
                 search_fn: Optional[Callable] = None,
                 enabled: bool = REFINER_ENABLED):
        self._llm_fn = llm_fn
        self._search_fn = search_fn
        self._enabled = enabled

    @property
    def available(self) -> bool:
        return self._enabled and self._llm_fn is not None

    def refine(self, query: str, items: List[Dict[str, Any]],
               complexity: str = "complex") -> RefinementResult:
        start = time.monotonic()

        if complexity != "complex" or not self.available:
            return RefinementResult(
                refined_items=items, iterations=0, agents_used=[],
                gaps_found=[], gaps_resolved=[], additional_queries=[],
                completeness_score=0.5, total_tokens_used=0,
                latency_ms=0.0, refined=False)

        total_tokens = 0
        all_gaps_found, all_gaps_resolved, all_additional_queries = [], [], []
        agents_used = []
        current_items = list(items)
        val_result = None
        last_iteration = 0
        crag_corrections = 0
        self_rag_retrievals = 0

        for iteration in range(MAX_ITERATIONS):
            last_iteration = iteration
            if total_tokens >= AGENT_TOKEN_BUDGET:
                break

            # Step 1: Coordinator
            coord = self._run_agent(AgentType.COORDINATOR, query, current_items)
            total_tokens += coord.tokens_used
            agents_used.append("coordinator")
            if not coord.success or not coord.gaps:
                break

            # Step 2: Specialist agents + CRAG validation
            for agent_name in coord.gaps:
                if total_tokens >= AGENT_TOKEN_BUDGET:
                    break
                try:
                    agent_type = AgentType(agent_name)
                except ValueError:
                    continue
                if agent_type in (AgentType.COORDINATOR, AgentType.VALIDATOR):
                    continue

                result = self._run_agent(agent_type, query, current_items)
                total_tokens += result.tokens_used
                agents_used.append(agent_name)

                if result.success:
                    # CRAG: Validate result relevance
                    crag_score = self._crag_validate(result, query)
                    result.crag_score = crag_score

                    if crag_score >= CRAG_RELEVANCE_THRESHOLD:
                        result.crag_accepted = True
                        all_gaps_found.extend(result.gaps)
                        all_additional_queries.extend(result.suggested_queries)
                    else:
                        # Corrective: reformulate query and retry
                        result.crag_accepted = False
                        crag_corrections += 1
                        corrective_queries = self._crag_corrective_queries(
                            agent_type, query, result)
                        all_additional_queries.extend(corrective_queries)
                        logger.info("CRAG correction: agent=%s, score=%.2f, corrective_queries=%d",
                                    agent_name, crag_score, len(corrective_queries))

            # Step 3: Gap-filling searches
            if self._search_fn and all_additional_queries:
                for gq in all_additional_queries[:3]:
                    try:
                        additional = self._search_fn(gq, max_results=3)
                        if additional:
                            current_items.extend(additional)
                            all_gaps_resolved.append(gq)
                    except Exception as exc:
                        logger.warning("Gap-fill search failed: %s", exc)

            # Step 4: Validator + Self-RAG reflection
            val_result = self._run_agent(AgentType.VALIDATOR, query, current_items)
            total_tokens += val_result.tokens_used
            agents_used.append("validator")

            # Self-RAG: Check if LLM needs additional retrieval
            if SELF_RAG_ENABLED and self._search_fn:
                needs_retrieval, retrieval_query = self._self_rag_reflect(
                    query, current_items)
                if needs_retrieval and retrieval_query:
                    try:
                        extra = self._search_fn(retrieval_query, max_results=3)
                        if extra:
                            current_items.extend(extra)
                            self_rag_retrievals += 1
                            logger.info("Self-RAG retrieval: query='%s', found=%d",
                                        retrieval_query[:50], len(extra))
                    except Exception as exc:
                        logger.warning("Self-RAG retrieval failed: %s", exc)

            if val_result.completeness >= 0.8 or not val_result.gaps:
                break

        elapsed = (time.monotonic() - start) * 1000
        completeness = val_result.completeness if val_result is not None else 0.5

        logger.info("Refinement: %d iters, %d agents, %d CRAG corrections, "
                    "%d Self-RAG retrievals, %.0fms",
                    last_iteration + 1, len(set(agents_used)),
                    crag_corrections, self_rag_retrievals, elapsed)

        return RefinementResult(
            refined_items=current_items, iterations=last_iteration + 1,
            agents_used=list(set(agents_used)), gaps_found=all_gaps_found,
            gaps_resolved=all_gaps_resolved,
            additional_queries=all_additional_queries,
            completeness_score=completeness,
            total_tokens_used=total_tokens, latency_ms=elapsed,
            crag_corrections=crag_corrections,
            self_rag_retrievals=self_rag_retrievals)

    # ── CRAG: Corrective RAG validation ────────────────────────────────

    def _crag_validate(self, agent_result: AgentResult, query: str) -> float:
        """Score agent output relevance using CRAG methodology."""
        if not agent_result.suggested_queries:
            return 0.5  # neutral if no queries suggested

        q_lower = query.lower()
        q_terms = set(q_lower.split())
        total_score = 0.0

        for sq in agent_result.suggested_queries:
            sq_terms = set(sq.lower().split())
            if q_terms and sq_terms:
                overlap = len(q_terms & sq_terms) / len(q_terms)
                total_score += overlap

        # Also check gaps relevance
        for gap in agent_result.gaps:
            gap_lower = gap.lower()
            if any(t in gap_lower for t in q_terms if len(t) > 3):
                total_score += 0.2

        n = len(agent_result.suggested_queries) + len(agent_result.gaps)
        return min(total_score / max(n, 1), 1.0)

    def _crag_corrective_queries(self, agent_type: AgentType,
                                  original_query: str,
                                  result: AgentResult) -> List[str]:
        """Generate corrective queries when CRAG rejects agent output."""
        corrections = []
        prefix_map = {
            AgentType.CODE: "function implementation details for",
            AgentType.REGISTER: "register configuration and bit fields for",
            AgentType.REQUIREMENT: "traceability requirements for",
            AgentType.SAFETY: "safety requirements and ASIL level for",
        }
        prefix = prefix_map.get(agent_type, "details about")
        # Extract key entities from original query
        entities = re.findall(r'\bIfx[A-Z]\w+\b|\b[A-Z]{2,}_REQ_\d+\b', original_query)
        if entities:
            corrections.append(f"{prefix} {' '.join(entities[:3])}")
        else:
            corrections.append(f"{prefix} {original_query[:100]}")
        return corrections[:2]

    # ── Self-RAG: Reflection ───────────────────────────────────────────

    def _self_rag_reflect(self, query: str,
                          items: List[Dict[str, Any]]) -> tuple:
        """Self-RAG: Ask LLM if additional retrieval is needed."""
        if not self._llm_fn:
            return False, ""

        try:
            context_summary = self._summarize_context(items, max_chars=1500)
            prompt = (
                f"Query: {query}\n\n"
                f"Current context ({len(items)} chunks):\n{context_summary}\n\n"
                f"{SELF_RAG_REFLECTION_PROMPT}"
            )
            response = self._llm_fn(
                system="You are a retrieval quality assessor.",
                user=prompt,
                max_tokens=200,
            )

            parsed = self._parse_json(response)
            needs = parsed.get("needs_retrieval", False)
            rq = parsed.get("retrieval_query", "")
            return bool(needs), rq

        except Exception as exc:
            logger.debug("Self-RAG reflection failed: %s", exc)
            return False, ""

    # ── Agent execution ────────────────────────────────────────────────

    def _run_agent(self, agent_type: AgentType, query: str,
                   items: List[Dict[str, Any]]) -> AgentResult:
        start = time.monotonic()
        system_prompt = _AGENT_PROMPTS.get(agent_type, "")
        if not system_prompt:
            return AgentResult(agent_type=agent_type, success=False, error="No prompt")

        context_summary = self._summarize_context(items)
        user_prompt = f"Query: {query}\n\nContext ({len(items)} chunks):\n{context_summary}"

        try:
            remaining = AGENT_TOKEN_BUDGET // (MAX_ITERATIONS * 3)
            response = self._llm_fn(
                system=system_prompt, user=user_prompt,
                max_tokens=min(300, remaining),
            )
            tokens_used = len(response) // 4 if response else 0  # M09 fix
            elapsed = (time.monotonic() - start) * 1000
            parsed = self._parse_json(response)

            return AgentResult(
                agent_type=agent_type,
                gaps=parsed.get("gaps", parsed.get("agents_needed", [])),
                suggested_queries=parsed.get("suggested_queries", []),
                completeness=parsed.get("completeness", 0.5),
                raw_response=response or "",
                tokens_used=tokens_used, latency_ms=elapsed)
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return AgentResult(agent_type=agent_type, success=False,
                               error=str(exc), latency_ms=elapsed)

    @staticmethod
    def _summarize_context(items, max_chars=2000):
        parts, chars = [], 0
        for i, item in enumerate(items):
            content = item.get("content", "")
            ntype = item.get("node_type", item.get("slot", ""))
            snippet = content[:200] if content else "(no content)"
            line = f"  [{i}] ({ntype}) {snippet}"
            if chars + len(line) > max_chars:
                parts.append(f"  ... and {len(items) - i} more")
                break
            parts.append(line)
            chars += len(line)
        return "\n".join(parts)

    @staticmethod
    def _parse_json(response):
        if not response:
            return {}
        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"gaps": [], "completeness": 0.5}
