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
        """No signals → base score 20 → FULL."""
        result = self.calc.evaluate({})
        assert result["score"] == 20
        assert result["review_type"] == "FULL"

    def test_auto_routing_high_confidence(self):
        """All quality signals → score >= 80 → AUTO."""
        result = self.calc.evaluate({
            "pattern_match": True,            # +10
            "api_verified": True,             # +25
            "call_order_valid": True,         # +25
        })
        # 20 + 10 + 25 + 25 = 80
        assert result["score"] >= 80
        assert result["review_type"] == "AUTO"

    def test_full_routing_low_confidence(self):
        """Risk signals → score < 50 → FULL."""
        result = self.calc.evaluate({
            "is_safety_critical": True,      # -15
            "no_failure_match": True,        # -10
        })
        # 20 - 15 - 10 = -5 → clamped to 0
        assert result["score"] < 50
        assert result["review_type"] == "FULL"

    def test_quick_routing_medium(self):
        """Mixed signals → QUICK."""
        result = self.calc.evaluate({
            "api_verified": True,            # +25
            "call_order_valid": True,        # +25
            "is_safety_critical": True,      # -15
        })
        # 20 + 25 + 25 - 15 = 55 → QUICK
        assert 50 <= result["score"] < 80
        assert result["review_type"] == "QUICK"

    def test_score_capped_at_100(self):
        result = self.calc.evaluate({
            "pattern_match": True,
            "api_verified": True,
            "call_order_valid": True,
            "config_valid": True,
            "output_well_formed": True,
        })
        # 20 + 10 + 25 + 25 + 15 + 5 = 100
        assert result["score"] == 100

    def test_score_floored_at_0(self):
        result = self.calc.evaluate({
            "is_safety_critical": True,
            "no_failure_match": True,
        })
        # 20 - 15 - 10 = -5 → clamped to 0
        assert result["score"] == 0

    def test_breakdown_included(self):
        result = self.calc.evaluate({"pattern_match": True, "is_safety_critical": True})
        assert len(result["breakdown"]) == 2
        signals = {b["signal"] for b in result["breakdown"]}
        assert "pattern_match" in signals
        assert "is_safety_critical" in signals

    def test_validation_score_mapping(self):
        """validation_score >= 80 → output_well_formed = True."""
        result = self.calc.evaluate({"validation_score": 92})
        assert result["score"] == 25  # 20 + 5 (output_well_formed)

    def test_validation_score_low(self):
        """validation_score < 80 → output_well_formed not triggered."""
        result = self.calc.evaluate({"validation_score": 70})
        assert result["score"] == 20  # No change

    def test_api_verified_signal(self):
        """api_verified = True → +25."""
        result = self.calc.evaluate({"api_verified": True})
        assert result["score"] == 45  # 20 + 25

    def test_api_match_ratio_mapping(self):
        """api_match_ratio >= 0.95 → api_verified = True."""
        result = self.calc.evaluate({"api_match_ratio": 0.98})
        assert result["score"] == 45  # 20 + 25 (api_verified)

    def test_api_match_ratio_low(self):
        """api_match_ratio < 0.95 → no api_verified."""
        result = self.calc.evaluate({"api_match_ratio": 0.80})
        assert result["score"] == 20  # No change

    def test_config_valid_signal(self):
        """config_valid = True → +15."""
        result = self.calc.evaluate({"config_valid": True})
        assert result["score"] == 35  # 20 + 15

    def test_config_match_ratio_mapping(self):
        """config_match_ratio >= 0.90 → config_valid = True."""
        result = self.calc.evaluate({"config_match_ratio": 0.95})
        assert result["score"] == 35  # 20 + 15 (config_valid)

    def test_config_match_ratio_low(self):
        """config_match_ratio < 0.90 → no config_valid."""
        result = self.calc.evaluate({"config_match_ratio": 0.85})
        assert result["score"] == 20  # No change

    def test_no_failure_match_signal(self):
        """no_failure_match = True → -10."""
        result = self.calc.evaluate({"no_failure_match": True})
        assert result["score"] == 10  # 20 - 10

    def test_safety_critical_signal(self):
        """is_safety_critical alone → -15."""
        result = self.calc.evaluate({"is_safety_critical": True})
        assert result["score"] == 5  # 20 - 15

    def test_response_id_auto_generated(self):
        result = self.calc.evaluate({})
        assert result["response_id"].startswith("resp_")

    def test_response_id_custom(self):
        result = self.calc.evaluate({}, response_id="my_resp_001")
        assert result["response_id"] == "my_resp_001"

    def test_routing_info(self):
        result = self.calc.evaluate({"pattern_match": True, "api_verified": True})
        assert "routing" in result
        assert result["routing"]["threshold_auto"] == 80
        assert result["routing"]["threshold_quick"] == 50
        assert isinstance(result["routing"]["estimated_minutes"], int)

    def test_example_gest_high(self):
        """All quality signals present → AUTO."""
        result = self.calc.evaluate({
            "pattern_match": True,            # +10
            "api_verified": True,             # +25
            "call_order_valid": True,         # +25
            "config_valid": True,             # +15
            "output_well_formed": True,       # +5
        })
        # 20 + 10 + 25 + 25 + 15 + 5 = 100
        assert result["review_type"] == "AUTO"

    def test_example_safety_critical_with_apis(self):
        """APIs verified but safety-critical → QUICK."""
        result = self.calc.evaluate({
            "api_verified": True,             # +25
            "call_order_valid": True,         # +25
            "is_safety_critical": True,       # -15
        })
        # 20 + 25 + 25 - 15 = 55 → QUICK
        assert result["score"] == 55
        assert result["review_type"] == "QUICK"

    def test_custom_weights(self):
        calc = ConfidenceCalculator(weights={"pattern_match": 60})
        result = calc.evaluate({"pattern_match": True})
        assert result["score"] == 80  # 20 + 60


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
            "api_verified": True,
            "call_order_valid": True,
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
        assert metrics["approved_patterns_count"] == 1  # APPROVE_WITH_EDITS counts as approved (confidence=0.75)

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
            conf = calc.evaluate({"api_verified": True, "call_order_valid": True,
                                   "config_valid": True, "output_well_formed": True},
                                  response_id=f"auto_{i}")
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
