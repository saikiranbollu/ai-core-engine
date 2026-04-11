"""
Context Builder — Sprint 8
============================
Token-budget-aware slot-based context assembler extracted from graphrag_query.py.

Algorithm:
  1. Group candidates by slot, sort by relevance
  2. Fill each slot up to its budget (best items first)
  3. Redistribute unused budget to hungry slots
  4. Second pass to fill redistributed budget
  5. Hard-cap total tokens

Shared by SearchService (hybrid_search) and RLMOrchestrator (sub-query assembly).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Token estimation ──────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Fast token estimate: ~3 chars per token (conservative)."""
    if not text:
        return 0
    return max(1, len(text) // 3)


# ── Slot categories ──────────────────────────────────────────────────

class ContextSlot:
    """Context slot categories with reserved budget allocation."""
    REQUIREMENTS = "requirements"
    API_FUNCTIONS = "api_functions"
    TESTS = "tests"
    DEPENDENCIES = "dependencies"
    RELATIONSHIPS = "relationships"
    CODE_EXAMPLES = "code_examples"
    SAFETY = "safety"
    REGISTERS = "registers"
    CONVERSATION = "conversation"
    CUSTOM = "custom"

    ALL = [
        REQUIREMENTS, API_FUNCTIONS, TESTS, DEPENDENCIES, RELATIONSHIPS,
        CODE_EXAMPLES, SAFETY, REGISTERS, CONVERSATION, CUSTOM,
    ]


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class ContextItem:
    """Single piece of context to include in the LLM prompt."""
    slot: str
    content: str
    relevance_score: float = 0.0
    source: str = ""
    entity_id: Optional[str] = None
    tokens: int = 0

    def __post_init__(self):
        if self.tokens == 0:
            self.tokens = estimate_tokens(self.content)


_DEFAULT_SLOT_BUDGETS: Dict[str, int] = {
    ContextSlot.REQUIREMENTS: 3000,
    ContextSlot.API_FUNCTIONS: 5000,
    ContextSlot.TESTS: 3000,
    ContextSlot.DEPENDENCIES: 2500,
    ContextSlot.RELATIONSHIPS: 1500,
    ContextSlot.CODE_EXAMPLES: 4000,
    ContextSlot.SAFETY: 1200,
    ContextSlot.REGISTERS: 3000,
    ContextSlot.CONVERSATION: 300,
    ContextSlot.CUSTOM: 1000,
}


@dataclass
class ContextBudget:
    """Token budget allocation per slot."""
    total_budget: int = 8000
    slot_budgets: Dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_SLOT_BUDGETS))

    def remaining(self, slot: str, used: int) -> int:
        return max(0, self.slot_budgets.get(slot, 0) - used)

    def redistribute_unused(self, usage: Dict[str, int]) -> None:
        """Give surplus budget from underused slots to overflowing ones."""
        unused_total = 0
        hungry: List[str] = []
        for slot, budget in self.slot_budgets.items():
            used = usage.get(slot, 0)
            if used < budget * 0.3:
                unused_total += budget - used
            elif used >= budget * 0.9:
                hungry.append(slot)
        if hungry and unused_total > 0:
            per_slot = unused_total // len(hungry)
            for slot in hungry:
                self.slot_budgets[slot] += per_slot


@dataclass
class AssembledContext:
    """Final assembled context ready for the LLM prompt."""
    items: List[ContextItem]
    total_tokens: int
    budget_used: Dict[str, int]
    items_included: int
    items_dropped: int
    provenance: List[Dict[str, str]]


# ── Builder ──────────────────────────────────────────────────────────

class ContextBuilder:
    """
    Token-budget-aware context assembler.

    Algorithm:
      1. Group candidates by slot, sort by relevance
      2. Fill each slot up to its budget (best items first)
      3. Redistribute unused budget to hungry slots
      4. Second pass to fill redistributed budget
      5. Hard-cap total tokens
    """

    def __init__(self, budget: Optional[ContextBudget] = None):
        self.budget = budget or ContextBudget()

    def build(
        self,
        candidates: List[ContextItem],
        max_tokens: Optional[int] = None,
    ) -> AssembledContext:
        total_budget = max_tokens or self.budget.total_budget
        if max_tokens:
            scale = max_tokens / 8000
            for slot in self.budget.slot_budgets:
                self.budget.slot_budgets[slot] = int(
                    self.budget.slot_budgets[slot] * scale
                )

        # Group by slot, sort by relevance descending
        by_slot: Dict[str, List[ContextItem]] = {}
        for item in candidates:
            by_slot.setdefault(item.slot, []).append(item)
        for slot in by_slot:
            by_slot[slot].sort(key=lambda x: x.relevance_score, reverse=True)

        # First pass: fill each slot up to budget
        selected: List[ContextItem] = []
        usage: Dict[str, int] = {}
        dropped = 0

        for slot, items in by_slot.items():
            slot_budget = self.budget.slot_budgets.get(slot, 0)
            slot_used = 0
            for item in items:
                if slot_used + item.tokens <= slot_budget:
                    selected.append(item)
                    slot_used += item.tokens
                else:
                    dropped += 1
            usage[slot] = slot_used

        # Redistribute unused budget to hungry slots
        self.budget.redistribute_unused(usage)

        # Second pass: fill redistributed budget
        for slot, items in by_slot.items():
            already = {id(i) for i in selected}
            new_budget = self.budget.slot_budgets.get(slot, 0)
            slot_used = usage.get(slot, 0)
            for item in items:
                if id(item) in already:
                    continue
                if slot_used + item.tokens <= new_budget:
                    selected.append(item)
                    slot_used += item.tokens
                    dropped -= 1
            usage[slot] = slot_used

        # Hard-cap: trim lowest-relevance items if over total budget
        total_used = sum(usage.values())
        if total_used > total_budget:
            selected.sort(key=lambda x: x.relevance_score)
            while total_used > total_budget and selected:
                removed = selected.pop(0)
                total_used -= removed.tokens
                usage[removed.slot] = usage.get(removed.slot, 0) - removed.tokens
                dropped += 1

        provenance = [
            {"entity_id": item.entity_id or "", "source": item.source, "slot": item.slot}
            for item in selected
        ]
        total_tokens = sum(item.tokens for item in selected)

        logger.info(
            "Context assembled: %d items (%d tokens / %d budget), %d dropped",
            len(selected), total_tokens, total_budget, dropped,
        )

        return AssembledContext(
            items=selected,
            total_tokens=total_tokens,
            budget_used=usage,
            items_included=len(selected),
            items_dropped=dropped,
            provenance=provenance,
        )

    @staticmethod
    def render(assembled: AssembledContext, separator: str = "\n\n") -> str:
        """Render assembled context into a string for the LLM prompt."""
        sections: Dict[str, List[str]] = {}
        for item in assembled.items:
            sections.setdefault(item.slot, []).append(item.content)

        parts: List[str] = []
        for slot in ContextSlot.ALL:
            items = sections.get(slot)
            if items:
                header = f"=== {slot.upper().replace('_', ' ')} ==="
                parts.append(header)
                parts.extend(items)

        return separator.join(parts)
