"""
Sprint 4 Integration Tests — Review Gate + Feedback & Learning
===============================================================
Tests:
  1. ConfidenceCalculator: deterministic scoring, routing thresholds
  2. FeedbackSink: submit, complete, override, metrics, patterns
  3. Full continuous learning loop simulation
  4. PPTX slide 25 scoring examples
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.ReviewGate.confidence import ConfidenceCalculator, FeedbackSink


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: ConfidenceCalculator — Deterministic Scoring
# ═════════════════════════════════════════════════════════════════════════

class TestConfidenceCalculator:

    def setup_method(self):
        self.calc = ConfidenceCalculator()

    def test_base_score_no_signals(self):
        """No signals → base score 50 → QUICK."""
        result = self.calc.evaluate({})
        assert result["score"] == 50
        assert result["review_type"] == "QUICK"

    def test_auto_routing_high_confidence(self):
        """All quality signals → score >= 80 → AUTO."""
        result = self.calc.evaluate({
            "has_kg_context": True,          # +30
            "has_dependency_order": True,     # +20
            "format_correct": True,          # +10
        })
        # 50 + 30 + 20 + 10 = 110 → capped at 100
        assert result["score"] >= 80
        assert result["review_type"] == "AUTO"

    def test_full_routing_low_confidence(self):
        """Risk signals → score < 50 → FULL."""
        result = self.calc.evaluate({
            "missing_requirements": True,    # -30
            "is_safety_critical": True,      # -15
        })
        # 50 - 30 - 15 = 5
        assert result["score"] < 50
        assert result["review_type"] == "FULL"

    def test_quick_routing_medium(self):
        """Mixed signals → QUICK."""
        result = self.calc.evaluate({
            "has_kg_context": True,          # +30
            "complex_logic": True,           # -10
            "novel_pattern": True,           # -15
        })
        # 50 + 30 - 10 - 15 = 55 → QUICK
        assert 50 <= result["score"] < 80
        assert result["review_type"] == "QUICK"

    def test_score_capped_at_100(self):
        result = self.calc.evaluate({
            "has_kg_context": True,
            "high_relevance": True,
            "has_proven_patterns": True,
            "format_correct": True,
            "misra_compliant": True,
            "similar_approved": True,
            "has_dependency_order": True,
        })
        assert result["score"] == 100

    def test_score_floored_at_0(self):
        result = self.calc.evaluate({
            "missing_requirements": True,
            "low_relevance": True,
            "novel_pattern": True,
            "compliance_warnings": True,
            "complex_logic": True,
            "is_safety_critical": True,
        })
        assert result["score"] == 0

    def test_breakdown_included(self):
        result = self.calc.evaluate({"has_kg_context": True, "is_safety_critical": True})
        assert len(result["breakdown"]) == 2
        signals = {b["signal"] for b in result["breakdown"]}
        assert "has_kg_context" in signals
        assert "is_safety_critical" in signals

    def test_validation_score_mapping(self):
        """validation_score >= 80 → format_correct = True."""
        result = self.calc.evaluate({"validation_score": 92})
        assert result["score"] == 60  # 50 + 10 (format_correct)

    def test_validation_score_low(self):
        """validation_score < 80 → format_correct not triggered."""
        result = self.calc.evaluate({"validation_score": 70})
        assert result["score"] == 50  # No change

    def test_relevance_score_high(self):
        """relevance_score >= 0.9 → high_relevance."""
        result = self.calc.evaluate({"relevance_score": 0.95})
        assert result["score"] == 70  # 50 + 20

    def test_relevance_score_low(self):
        """relevance_score < 0.7 → low_relevance."""
        result = self.calc.evaluate({"relevance_score": 0.5})
        assert result["score"] == 30  # 50 - 20

    def test_response_id_auto_generated(self):
        result = self.calc.evaluate({})
        assert result["response_id"].startswith("resp_")

    def test_response_id_custom(self):
        result = self.calc.evaluate({}, response_id="my_resp_001")
        assert result["response_id"] == "my_resp_001"

    def test_routing_info(self):
        result = self.calc.evaluate({"has_kg_context": True, "has_dependency_order": True})
        assert "routing" in result
        assert result["routing"]["threshold_auto"] == 80
        assert result["routing"]["threshold_quick"] == 50
        assert isinstance(result["routing"]["estimated_minutes"], int)

    def test_pptx_example_gest_high(self):
        """PPTX slide 25 example: GEST generates tests → HIGH → AUTO."""
        result = self.calc.evaluate({
            "has_kg_context": True,          # +30
            "has_proven_patterns": True,     # +15
            "high_relevance": True,          # +20
            "misra_compliant": True,         # +10
            "similar_approved": True,        # +5
        })
        # 50 + 30 + 15 + 20 + 10 + 5 = 130 → capped 100
        assert result["review_type"] == "AUTO"

    def test_pptx_example_cia_complex(self):
        """PPTX slide 25 example: CIA complex algorithm → LOW → FULL."""
        result = self.calc.evaluate({
            "has_kg_context": True,          # +30
            "novel_pattern": True,           # -15
            "complex_logic": True,           # -10
            "low_relevance": True,           # -20
        })
        # 50 + 30 - 15 - 10 - 20 = 35
        assert result["score"] == 35
        assert result["review_type"] == "FULL"

    def test_custom_weights(self):
        calc = ConfidenceCalculator(weights={"has_kg_context": 50})
        result = calc.evaluate({"has_kg_context": True})
        assert result["score"] == 100  # 50 + 50


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: FeedbackSink
# ═════════════════════════════════════════════════════════════════════════

class TestFeedbackSink:

    def setup_method(self):
        self.sink = FeedbackSink()

    def test_submit_approve(self):
        result = self.sink.submit_feedback("resp_001", "APPROVE", reviewer_id="dev_a")
        assert result["recorded"] is True
        assert result["accuracy_assessment"] is True
        assert result["feedback_id"].startswith("fb_")

    def test_submit_reject(self):
        result = self.sink.submit_feedback(
            "resp_002", "REJECT", issues_found=3,
            correction_notes="Missing error handling for timeout case")
        assert result["recorded"] is True
        assert result["accuracy_assessment"] is False

    def test_submit_invalid_decision(self):
        with pytest.raises(ValueError, match="Invalid decision"):
            self.sink.submit_feedback("resp_003", "INVALID")

    def test_complete_review(self):
        result = self.sink.complete_review(
            "resp_001", "APPROVE", reviewer_id="dev_a",
            rationale="Correct init sequence, MISRA compliant")
        assert result["review_id"].startswith("rv_")
        assert result["feedback_recorded"] is True

    def test_override_routing(self):
        result = self.sink.override_routing(
            "resp_001", "FULL", "High safety impact", previous_type="AUTO")
        assert result["applied"] is True
        assert result["previous"] == "AUTO"
        assert result["current"] == "FULL"

    def test_override_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid review_type"):
            self.sink.override_routing("resp_001", "INVALID", "reason")

    def test_learning_metrics(self):
        self.sink.submit_feedback("r1", "APPROVE")
        self.sink.submit_feedback("r2", "APPROVE_WITH_EDITS", issues_found=1)
        self.sink.submit_feedback("r3", "REJECT", issues_found=5)

        metrics = self.sink.get_learning_metrics()
        assert metrics["total_feedbacks"] == 3
        assert metrics["approvals"] == 2  # APPROVE + APPROVE_WITH_EDITS
        assert metrics["rejections"] == 1
        assert metrics["approval_rate"] == pytest.approx(0.667, abs=0.01)

    def test_learning_metrics_with_details(self):
        self.sink.submit_feedback("r1", "REJECT", correction_notes="Bad init order")
        metrics = self.sink.get_learning_metrics(include_pattern_details=True)
        assert "recent_failures" in metrics
        assert len(metrics["recent_failures"]) == 1

    def test_failure_patterns(self):
        self.sink.submit_feedback("r1", "REJECT", correction_notes="Missing polling")
        self.sink.submit_feedback("r2", "REJECT", correction_notes="Wrong register")
        patterns = self.sink.get_failure_patterns()
        assert len(patterns) == 2

    def test_review_analytics(self):
        self.sink.complete_review("r1", "APPROVE")
        self.sink.complete_review("r2", "APPROVE_WITH_EDITS")
        self.sink.complete_review("r3", "REJECT")
        analytics = self.sink.get_review_analytics()
        assert analytics["total_reviews"] == 3
        assert analytics["by_decision"]["APPROVE"] == 1
        assert analytics["by_decision"]["REJECT"] == 1


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Full Continuous Learning Loop (PPTX slide 26)
# ═════════════════════════════════════════════════════════════════════════

class TestContinuousLearningLoop:
    """Simulate: generate → evaluate → review → feedback → learn."""

    def test_full_loop(self):
        calc = ConfidenceCalculator()
        sink = FeedbackSink()

        # Step 1: DA generates output, evaluate confidence
        conf = calc.evaluate({
            "has_kg_context": True,
            "has_dependency_order": True,
            "validation_score": 85,
        }, response_id="gen_001")
        assert conf["review_type"] in ("AUTO", "QUICK", "FULL")

        # Step 2: Human reviews — APPROVE_WITH_EDITS
        review = sink.complete_review(
            "gen_001", "APPROVE_WITH_EDITS", reviewer_id="dev_sai",
            issues_found=1, rationale="Fixed init order for DMA dependency")

        # Step 3: Human submits feedback
        feedback = sink.submit_feedback(
            "gen_001", "APPROVE_WITH_EDITS", reviewer_id="dev_sai",
            issues_found=1, correction_notes="DMA init must precede SPI init")

        # Step 4: Verify metrics updated
        metrics = sink.get_learning_metrics(include_pattern_details=True)
        assert metrics["total_feedbacks"] == 1
        assert metrics["approved_patterns_count"] == 0  # APPROVE_WITH_EDITS != APPROVE

        # Step 5: Simulate a REJECT to build failure patterns
        sink.submit_feedback(
            "gen_002", "REJECT", issues_found=3,
            correction_notes="Missing status polling after async IfxCxpi_sendHeader")
        patterns = sink.get_failure_patterns()
        assert len(patterns) == 1
        assert "polling" in patterns[0]["notes"].lower()

    def test_confidence_accuracy_tracking(self):
        """Track if AUTO predictions are actually correct."""
        calc = ConfidenceCalculator()
        sink = FeedbackSink()

        # Generate 5 outputs with HIGH confidence → AUTO
        for i in range(5):
            conf = calc.evaluate({"has_kg_context": True, "has_dependency_order": True,
                                   "misra_compliant": True}, response_id=f"auto_{i}")
            assert conf["review_type"] == "AUTO"

        # 4 approved, 1 rejected → 80% accuracy for AUTO routing
        for i in range(4):
            sink.submit_feedback(f"auto_{i}", "APPROVE")
            sink.complete_review(f"auto_{i}", "APPROVE")
        sink.submit_feedback("auto_4", "REJECT", issues_found=2)
        sink.complete_review("auto_4", "REJECT")

        analytics = sink.get_review_analytics()
        assert analytics["total_reviews"] == 5
        # 4 APPROVE out of 5 = 0.8 accuracy
        assert analytics["accuracy_rate"] == pytest.approx(0.8, abs=0.01)
