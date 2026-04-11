"""
Context Builder — Re-export from HybridRAG querier module.

The authoritative ContextBuilder lives in src/HybridRAG/code/querier/context_builder.py
(Sprint 8, 10-slot token-budget algorithm). This module re-exports it for architectural
consistency since ContextBuilder is a Memory Layer component (see ADR-011).

The legacy Sprint 2 "librarian" builder is preserved as LegacyContextBuilder for
backward compatibility with E2E tests.
"""
from __future__ import annotations

# ── Authoritative exports (Sprint 8 slot-based builder) ───────────────
from src.HybridRAG.code.querier.context_builder import (  # noqa: F401
    ContextBuilder,
    ContextBudget,
    ContextItem,
    ContextSlot,
    AssembledContext,
    estimate_tokens,
)

# ── Legacy builder preserved for backward compatibility ────────────────
# (Original Sprint 2 implementation)
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4


def _legacy_estimate_tokens(text: str) -> int:
    """Rough token count estimate (Sprint 2 version: 1 token ≈ 4 chars)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


class LegacyContextBuilder:
    """
    Sprint 2 greedy librarian builder.

    DEPRECATED: Use ContextBuilder (re-exported above) for new code.
    This class is preserved only for backward compatibility with tests
    that depend on the (rag_results, conversation_history, session_context)
    call signature.
    """

    def __init__(self, max_tokens: int = 8000, budget_unit: str = "tokens"):
        self.max_tokens = max_tokens
        self.budget_unit = budget_unit
        self._max_chars = max_tokens * CHARS_PER_TOKEN if budget_unit == "tokens" else max_tokens

    def build(
        self,
        rag_results: Optional[List[Dict[str, Any]]] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build an optimized context payload.

        Parameters
        ----------
        rag_results : list of dicts
            Results from search_database. Each should have:
              - content/text/data: the actual content
              - score/relevance_score: ranking score
              - node_type, node_id, source: provenance
        conversation_history : list of dicts
            Previous conversation turns [{role, content}].
        session_context : dict
            Additional session metadata to include.

        Returns
        -------
        dict with: rendered_context, total_tokens, items_included,
                   items_dropped, provenance
        """
        budget_remaining = self._max_chars
        included: List[Dict[str, Any]] = []
        dropped: List[str] = []
        provenance: List[Dict[str, Any]] = []

        # 1. Reserve space for conversation history (~20% budget)
        history_budget = int(self._max_chars * 0.2)
        history_text = ""
        if conversation_history:
            for turn in conversation_history[-5:]:  # last 5 turns
                entry = f"[{turn.get('role', 'user')}]: {turn.get('content', '')}\n"
                if len(history_text) + len(entry) <= history_budget:
                    history_text += entry
            budget_remaining -= len(history_text)

        # 2. Reserve space for session context (~5% budget)
        session_text = ""
        if session_context:
            ctx_str = json.dumps(session_context, default=str)
            session_budget = int(self._max_chars * 0.05)
            if len(ctx_str) <= session_budget:
                session_text = f"[Session Context]: {ctx_str}\n"
                budget_remaining -= len(session_text)

        # 3. Sort RAG results by relevance (highest first)
        results = list(rag_results or [])
        results.sort(key=lambda r: r.get("score", r.get("relevance_score", 0)), reverse=True)

        # 4. Greedily fill remaining budget
        for item in results:
            content = self._extract_content(item)
            content_len = len(content)

            if content_len <= budget_remaining:
                included.append(item)
                budget_remaining -= content_len
                provenance.append({
                    "node_id": item.get("node_id", item.get("id", "?")),
                    "node_type": item.get("node_type", item.get("label", "?")),
                    "source": item.get("source", "hybrid"),
                    "score": round(item.get("score", item.get("relevance_score", 0)), 4),
                    "tokens": _legacy_estimate_tokens(content),
                })
            else:
                dropped.append(item.get("node_id", item.get("id", "unknown")))

        # 5. Render final context
        context_parts = []
        if session_text:
            context_parts.append(session_text)
        if history_text:
            context_parts.append(f"[Conversation History]:\n{history_text}")

        for item in included:
            content = self._extract_content(item)
            node_type = item.get("node_type", item.get("label", ""))
            node_id = item.get("node_id", item.get("id", ""))
            context_parts.append(f"[{node_type}: {node_id}]\n{content}")

        rendered = "\n---\n".join(context_parts)
        total_tokens = _legacy_estimate_tokens(rendered)

        return {
            "rendered_context": rendered,
            "total_tokens": total_tokens,
            "budget_unit": self.budget_unit,
            "max_tokens": self.max_tokens,
            "items_included": len(included),
            "items_dropped": len(dropped),
            "dropped_ids": dropped[:10],  # cap for response size
            "provenance": provenance,
        }

    def _extract_content(self, item: Dict[str, Any]) -> str:
        """Extract text content from a RAG result item."""
        for key in ("content", "text", "rendered", "description"):
            if key in item and isinstance(item[key], str):
                return item[key]
        if "data" in item and isinstance(item["data"], dict):
            return json.dumps(item["data"], default=str)
        if "properties" in item and isinstance(item["properties"], dict):
            return json.dumps(item["properties"], default=str)
        return json.dumps(item, default=str)
