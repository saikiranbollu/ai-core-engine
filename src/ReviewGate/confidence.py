"""
Confidence Calculator — Sprint 4
==================================
Deterministic formula (NOT LLM-based) for review routing.

From PPTX v3 Slide 25:
  Base Score = 50
  Quality signals add points, risk signals subtract.
  AUTO (>=80) | QUICK (50-79) | FULL (<50)

Weights are configurable per workspace and self-adjust over time
based on feedback loop correlation analysis.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Default weights (from PPTX slide 25)
# ─────────────────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS = {
    # Quality signals (positive)
    "has_kg_context":        30,   # All inputs available (requirements, specs, examples)
    "high_relevance":        20,   # Context relevance > 0.9
    "has_proven_patterns":   15,   # Used proven patterns from knowledge base
    "format_correct":        10,   # Output matches expected format/structure
    "misra_compliant":       10,   # MISRA/AUTOSAR compliance checks passed
    "similar_approved":       5,   # Similar past generation was approved
    "has_dependency_order":  20,   # Dependency/init order resolved
    # Risk signals (negative)
    "missing_requirements": -30,   # Missing requirements or specs
    "low_relevance":        -20,   # Context relevance < 0.7
    "novel_pattern":        -15,   # Novel pattern (not in knowledge base)
    "compliance_warnings":  -20,   # Compliance check warnings
    "complex_logic":        -10,   # Complex logic or edge cases
    "is_safety_critical":   -15,   # ASIL-B or higher
}

# Review routing thresholds
THRESHOLD_AUTO = 80   # Score >= 80 → AUTO (~5 min review)
THRESHOLD_QUICK = 50  # Score 50-79 → QUICK (~15-20 min review)
                      # Score < 50  → FULL (~1+ hour review)


class ConfidenceCalculator:
    """
    Deterministic confidence scoring for review routing.

    Parameters
    ----------
    weights : dict, optional
        Override default signal weights.
    auto_threshold : int
        Score threshold for AUTO routing (default 80).
    quick_threshold : int
        Score threshold for QUICK routing (default 50).
    """

    def __init__(
        self,
        weights: Optional[Dict[str, int]] = None,
        auto_threshold: int = THRESHOLD_AUTO,
        quick_threshold: int = THRESHOLD_QUICK,
    ):
        self._weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self._auto = auto_threshold
        self._quick = quick_threshold

    def evaluate(
        self,
        signals: Dict[str, Any],
        response_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Score an AI artifact and determine review routing.

        Parameters
        ----------
        signals : dict
            Quality/risk signal flags. Keys should match weight names.
            Values: bool (True/False) or numeric (0.0-1.0 for scaled signals).
            Special keys:
              - validation_score: int 0-100, mapped to format_correct if > 80
              - relevance_score: float 0-1, mapped to high/low relevance
        response_id : str, optional
            If not provided, auto-generated.

        Returns
        -------
        dict with: score, review_type, routing, breakdown, response_id
        """
        rid = response_id or f"resp_{uuid.uuid4().hex[:8]}"
        base_score = 50
        breakdown: List[Dict[str, Any]] = []

        # Map special composite signals
        mapped = dict(signals)

        # validation_score → format_correct
        vs = mapped.pop("validation_score", None)
        if vs is not None and isinstance(vs, (int, float)):
            mapped.setdefault("format_correct", vs >= 80)

        # relevance_score → high_relevance / low_relevance
        rs = mapped.pop("relevance_score", None)
        if rs is not None and isinstance(rs, (int, float)):
            if rs >= 0.9:
                mapped.setdefault("high_relevance", True)
            elif rs < 0.7:
                mapped.setdefault("low_relevance", True)

        # Calculate score
        total_adjust = 0
        for signal_name, value in mapped.items():
            weight = self._weights.get(signal_name)
            if weight is None:
                continue

            # Bool signals: apply full weight if True
            if isinstance(value, bool):
                if value:
                    adjustment = weight
                    breakdown.append({
                        "signal": signal_name,
                        "value": True,
                        "weight": weight,
                        "adjustment": adjustment,
                    })
                    total_adjust += adjustment
            # Numeric signals: scale weight by value
            elif isinstance(value, (int, float)):
                adjustment = int(weight * min(1.0, max(0.0, float(value))))
                if adjustment != 0:
                    breakdown.append({
                        "signal": signal_name,
                        "value": round(float(value), 3),
                        "weight": weight,
                        "adjustment": adjustment,
                    })
                    total_adjust += adjustment

        raw_score = base_score + total_adjust
        score = max(0, min(100, raw_score))

        # Determine routing
        if score >= self._auto:
            review_type = "AUTO"
            est_minutes = 5
        elif score >= self._quick:
            review_type = "QUICK"
            est_minutes = 15
        else:
            review_type = "FULL"
            est_minutes = 60

        return {
            "response_id": rid,
            "score": score,
            "raw_score": raw_score,
            "base_score": base_score,
            "review_type": review_type,
            "routing": {
                "type": review_type,
                "estimated_minutes": est_minutes,
                "threshold_auto": self._auto,
                "threshold_quick": self._quick,
            },
            "breakdown": sorted(breakdown, key=lambda b: abs(b["adjustment"]), reverse=True),
            "signal_count": len(breakdown),
            "timestamp": time.time(),
        }


# ─────────────────────────────────────────────────────────────────────────
# Feedback Sink — stores review decisions for continuous learning
# ─────────────────────────────────────────────────────────────────────────

class FeedbackSink:
    """
    Stores human review decisions and tracks learning metrics.

    In production, writes to Neo4j (ApprovedPattern/FailurePattern nodes)
    and PostgreSQL (audit trail). Sprint 8: PostgreSQL write-through enabled.
    """

    def __init__(self, postgres_client=None, pattern_store=None, pattern_index=None):
        self._feedbacks: Dict[str, Dict[str, Any]] = {}
        self._reviews: Dict[str, Dict[str, Any]] = {}
        self._failure_patterns: List[Dict[str, Any]] = []
        self._approved_patterns: List[Dict[str, Any]] = []
        self._pg = postgres_client      # Optional PostgresClient for write-through
        self._pattern_store = pattern_store    # Optional PatternStore (Neo4j)
        self._pattern_index = pattern_index    # Optional PatternIndex (Qdrant)

    # ── Feedback Recording ─────────────────────────────────────────────

    def submit_feedback(
        self,
        response_id: str,
        decision: str,
        reviewer_id: Optional[str] = None,
        issues_found: int = 0,
        correction_notes: Optional[str] = None,
        module: Optional[str] = None,
        task_type: Optional[str] = None,
        response_context: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a human review decision and feed into the learning loop.

        Sprint 9: APPROVE/APPROVE_WITH_EDITS now writes to PatternStore (Neo4j)
        and indexes in PatternIndex (Qdrant) for future similarity matching.
        This enables the confidence scorer's 'has_proven_patterns' signal.
        """
        valid_decisions = {"APPROVE", "APPROVE_WITH_EDITS", "REJECT", "ESCALATE"}
        if decision not in valid_decisions:
            raise ValueError(f"Invalid decision '{decision}'. Must be one of: {valid_decisions}")

        feedback_id = f"fb_{uuid.uuid4().hex[:8]}"
        entry = {
            "feedback_id": feedback_id,
            "response_id": response_id,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "issues_found": issues_found,
            "correction_notes": correction_notes,
            "module": module,
            "task_type": task_type,
            "timestamp": time.time(),
        }
        self._feedbacks[feedback_id] = entry

        # Write-through to PostgreSQL
        if self._pg:
            self._pg.save_feedback(
                feedback_id=feedback_id, response_id=response_id,
                decision=decision, reviewer_id=reviewer_id,
                issues_found=issues_found, correction_notes=correction_notes,
                module=module, task_type=task_type,
            )

        pattern_stored = False
        pattern_indexed = False

        # ── LEARNING LOOP: APPROVE → PatternStore + PatternIndex ──
        if decision in ("APPROVE", "APPROVE_WITH_EDITS") and response_context:
            # Store as ApprovedPattern in Neo4j via PatternStore
            if self._pattern_store:
                try:
                    from src.MemoryLayer.memory.semantic_memory import ApprovedPattern
                    pattern = ApprovedPattern(
                        pattern_text=response_context[:4000],  # Cap at 4K chars
                        pattern_type=task_type or "generic",
                        module=module or "unknown",
                        profile=profile or "illd",
                        confidence=0.9 if decision == "APPROVE" else 0.75,
                        approver_id=reviewer_id,
                        source_request_id=response_id,
                    )
                    self._pattern_store.store(pattern)
                    pattern_stored = True
                    logger.info(
                        "[FeedbackSink] Stored approved pattern %s for %s/%s",
                        pattern.pattern_id, module, task_type,
                    )
                except Exception as e:
                    logger.warning("[FeedbackSink] PatternStore write failed: %s", e)

            # Index in Qdrant via PatternIndex for semantic search
            if self._pattern_index and module:
                try:
                    # PatternIndex delegates to PatternStore; collection assumed pre-existing
                    # PatternIndex.index_pattern expects an ApprovedPattern
                    if pattern_stored and pattern is not None:  # N-H04 fix
                        self._pattern_index.index_pattern(pattern)
                        pattern_indexed = True
                        logger.info(
                            "[FeedbackSink] Indexed pattern in Qdrant for %s/%s",
                            profile or "illd", module,
                        )
                except Exception as e:
                    logger.warning("[FeedbackSink] PatternIndex write failed: %s", e)

        # Track patterns in memory
        if decision == "REJECT":
            failure_entry = {
                "response_id": response_id,
                "issues": issues_found,
                "notes": correction_notes,
                "module": module,
                "task_type": task_type,
                "timestamp": time.time(),
            }
            self._failure_patterns.append(failure_entry)
            if self._pg:
                self._pg.save_failure_pattern(
                    description=correction_notes,
                    severity="high" if issues_found > 3 else "medium",
                    module=module,
                    category=task_type,
                )
        elif decision in ("APPROVE", "APPROVE_WITH_EDITS"):
            self._approved_patterns.append({
                "response_id": response_id,
                "module": module,
                "task_type": task_type,
                "timestamp": time.time(),
                "stored_in_neo4j": pattern_stored,
                "indexed_in_qdrant": pattern_indexed,
            })

        logger.info("[FeedbackSink] Recorded %s for %s: %s", feedback_id, response_id, decision)
        return {
            "feedback_id": feedback_id,
            "recorded": True,
            "accuracy_assessment": decision in ("APPROVE", "APPROVE_WITH_EDITS"),
            "pattern_stored": pattern_stored,
            "pattern_indexed": pattern_indexed,
        }

    # ── Review Completion ──────────────────────────────────────────────

    def complete_review(
        self,
        response_id: str,
        decision: str,
        reviewer_id: Optional[str] = None,
        issues_found: int = 0,
        rationale: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark a review as completed (closes the review gate)."""
        review_id = f"rv_{uuid.uuid4().hex[:8]}"
        entry = {
            "review_id": review_id,
            "response_id": response_id,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "issues_found": issues_found,
            "rationale": rationale,
            "timestamp": time.time(),
        }
        self._reviews[review_id] = entry
        # Write-through to PostgreSQL
        if self._pg:
            self._pg.save_review_evidence(
                review_id=review_id, response_id=response_id,
                decision=decision, reviewer_id=reviewer_id,
                issues_found=issues_found, rationale=rationale,
            )
        logger.info("[FeedbackSink] Review %s completed for %s: %s", review_id, response_id, decision)
        return {"review_id": review_id, "feedback_recorded": True}

    # ── Review Override ────────────────────────────────────────────────

    def override_routing(
        self,
        response_id: str,
        new_review_type: str,
        reason: str,
        previous_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Override automatic review routing."""
        valid_types = {"AUTO", "QUICK", "FULL"}
        if new_review_type not in valid_types:
            raise ValueError(f"Invalid review_type '{new_review_type}'")
        override_id = f"or_{uuid.uuid4().hex[:8]}"
        entry = {
            "override_id": override_id,
            "response_id": response_id,
            "previous": previous_type,
            "current": new_review_type,
            "reason": reason,
            "timestamp": time.time(),
        }
        logger.info("[FeedbackSink] Override %s: %s → %s (%s)",
                     response_id, previous_type, new_review_type, reason)
        return {"applied": True, "previous": previous_type, "current": new_review_type}

    # ── Learning Metrics ───────────────────────────────────────────────

    def get_learning_metrics(self, include_pattern_details: bool = False) -> Dict[str, Any]:
        """Retrieve learning-loop improvement metrics."""
        total = len(self._feedbacks)
        approvals = sum(1 for f in self._feedbacks.values()
                        if f["decision"] in ("APPROVE", "APPROVE_WITH_EDITS"))
        rejections = sum(1 for f in self._feedbacks.values() if f["decision"] == "REJECT")

        metrics = {
            "total_feedbacks": total,
            "approvals": approvals,
            "rejections": rejections,
            "approval_rate": round(approvals / total, 3) if total > 0 else 0,
            "rejection_rate": round(rejections / total, 3) if total > 0 else 0,
            "failure_patterns_count": len(self._failure_patterns),
            "approved_patterns_count": len(self._approved_patterns),
        }
        if include_pattern_details:
            metrics["recent_failures"] = self._failure_patterns[-10:]
            metrics["recent_approvals"] = self._approved_patterns[-10:]
        return metrics

    def get_failure_patterns(
        self,
        module: Optional[str] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query learned failure patterns."""
        patterns = list(self._failure_patterns)
        # In production, filter by module/category in Neo4j
        # For Sprint 4, return all
        return patterns[-20:]  # Last 20

    # ── Review Analytics ───────────────────────────────────────────────

    def get_review_analytics(self) -> Dict[str, Any]:
        """Retrieve review-gate metrics."""
        total = len(self._reviews)
        by_decision: Dict[str, int] = {}
        for r in self._reviews.values():
            d = r["decision"]
            by_decision[d] = by_decision.get(d, 0) + 1

        return {
            "total_reviews": total,
            "by_decision": by_decision,
            "accuracy_rate": (
                by_decision.get("APPROVE", 0) + by_decision.get("APPROVE_WITH_EDITS", 0)
            ) / total if total > 0 else 0,
        }
