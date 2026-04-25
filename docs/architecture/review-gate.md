# Review Gate Architecture

**Component**: `src/ReviewGate/`
**Primary classes**: `ConfidenceCalculator`, `FeedbackSink`, `ResultProcessor`
**Backing stores**: PostgreSQL (feedback/evidence), Qdrant (pattern store)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Confidence Calculator](#2-confidence-calculator)
3. [Review Routing](#3-review-routing)
4. [Feedback Sink](#4-feedback-sink)
5. [Result Processors](#5-result-processors)
6. [Learning Loop](#6-learning-loop)
7. [File Map](#7-file-map)

---

## 1. Overview

The Review Gate is the quality assurance subsystem of AICE. It provides **deterministic** (not LLM-based) confidence scoring for DA outputs, routes responses to appropriate review levels, collects human feedback, and processes test/analysis results.

```
DA generates response
         │
         ▼
┌─ ConfidenceCalculator ─────────────────────┐
│                                             │
│  Signals: has_kg_context, high_relevance,   │
│  has_dependency_order, missing_requirements, │
│  is_safety_critical, ...                    │
│                                             │
│  base_score = 50                            │
│  + positive signals                         │
│  - negative signals                         │
│  = final_score                              │
└───────────────┬─────────────────────────────┘
                │
         ┌──────┼───────┐
         │      │       │
         ▼      ▼       ▼
       AUTO   QUICK    FULL
       (≥80)  (50-79)  (<50)
         │      │       │
         ▼      ▼       ▼
     ~5 min  ~15-20   ≥1 hr
     spot    min      deep
     check   review   review
                │
                ▼
       Human Feedback
       (APPROVE / APPROVE_WITH_EDITS / REJECT / ESCALATE)
                │
                ▼
       FeedbackSink → PostgreSQL + PatternStore
```

---

## 2. Confidence Calculator

### Design Principle

Scoring is **deterministic and explainable**: given the same signals, the same score is always produced. No LLM is involved. This is a conscious design choice for automotive domain compliance (ASPICE requires auditable, repeatable quality processes).

### Scoring Formula

```
final_score = clamp(base_score + Σ(signal_weight), 0, 100)
```

Where `base_score = 50` (neutral starting point).

### Signal Weights

**Quality signals (positive)**:

| Signal | Weight | Description |
|--------|--------|-------------|
| `has_kg_context` | +30 | Response backed by knowledge graph data |
| `high_relevance` | +20 | Search results had high relevance scores |
| `has_dependency_order` | +20 | Correct dependency/init ordering present |
| `has_proven_patterns` | +15 | Uses patterns from approved examples |
| `format_correct` | +10 | Output follows expected format/structure |
| `misra_compliant` | +10 | No MISRA C:2012 violations detected |
| `similar_approved` | +5 | Matches a previously approved pattern |

**Risk signals (negative)**:

| Signal | Weight | Description |
|--------|--------|-------------|
| `missing_requirements` | -30 | Referenced requirements not found in KG |
| `low_relevance` | -20 | Search results had low relevance scores |
| `compliance_warnings` | -20 | AUTOSAR/MISRA compliance issues detected |
| `novel_pattern` | -15 | No similar approved patterns found |
| `is_safety_critical` | -15 | ASIL-B or higher safety context |
| `complex_logic` | -10 | High cyclomatic complexity or multi-path logic |

### evaluate() Method

```python
def evaluate(self, signals: Dict[str, bool], response_id: str) -> Dict:
    score = self.base_score  # 50
    breakdown = {}

    for signal_name, is_present in signals.items():
        if is_present and signal_name in self.weights:
            weight = self.weights[signal_name]
            score += weight
            breakdown[signal_name] = weight

    score = max(0, min(100, score))  # clamp to [0, 100]

    review_type = self._route(score)

    return {
        "score": score,
        "review_type": review_type,   # "AUTO", "QUICK", or "FULL"
        "routing": self._routing_details(review_type),
        "breakdown": breakdown,        # explains contribution of each signal
        "response_id": response_id,
    }
```

### Signal Detection

Signals are computed by the DA before calling `evaluate_confidence`. DAs determine signals based on their domain:

- **CIA** (code generator): checks for KG-backed API signatures, MISRA compliance, dependency ordering
- **GEST** (test generator): checks for requirement coverage, test structure validity
- **ACRA** (code reviewer): checks for compliance warnings, complexity metrics

The Review Gate does not compute signals itself — it only scores and routes based on the signals provided.

---

## 3. Review Routing

### Thresholds

| Score Range | Route | Expected Review Time | Action |
|-------------|-------|---------------------|--------|
| **≥ 80** | `AUTO` | ~5 minutes | Auto-approve with spot check |
| **50 – 79** | `QUICK` | ~15-20 minutes | Quick manual review |
| **< 50** | `FULL` | ≥1 hour | Full deep review by domain expert |

### Routing Details

Each route includes suggested review actions:

```python
{
    "AUTO": {
        "action": "auto_approve",
        "reviewer_level": "any",
        "checklist": ["spot_check_output", "verify_format"]
    },
    "QUICK": {
        "action": "manual_review",
        "reviewer_level": "developer",
        "checklist": ["verify_correctness", "check_completeness", "validate_compliance"]
    },
    "FULL": {
        "action": "deep_review",
        "reviewer_level": "domain_expert",
        "checklist": ["full_correctness", "safety_analysis", "requirement_coverage", "compliance_audit"]
    }
}
```

---

## 4. Feedback Sink

`FeedbackSink` collects and persists human review decisions:

### Feedback Types

| Verdict | Meaning |
|---------|---------|
| `APPROVE` | Response is correct and complete |
| `APPROVE_WITH_EDITS` | Response is mostly correct, minor edits applied |
| `REJECT` | Response is incorrect or inadequate |
| `ESCALATE` | Requires higher-level review |

### Storage

Feedback is written to:
1. **PostgreSQL** `feedback_records` table (durable) — includes `response_id`, verdict, reviewer, timestamp, edits applied
2. **PostgreSQL** `review_evidence` table — stores the full response and review artifacts as ASPICE work products

Write is best-effort: if PostgreSQL is unavailable, feedback is logged but not persisted.

### Pattern Extraction

When a response is `APPROVE`d, the `FeedbackSink` extracts an `ApprovedPattern` and stores it in the `PatternStore` (Qdrant). This feeds the learning loop (see [Section 6](#6-learning-loop)).

---

## 5. Result Processors

`ResultProcessor` (709 lines) ingests test and analysis results from CI/CD tools and converts them into a unified `TestResult` model.

### Supported Formats

| Parser | Input Format | Source Tool |
|--------|-------------|-------------|
| `JUnitParser` | JUnit XML | Any JUnit-compatible runner |
| `VPParser` | VectorCAST VP format | VectorCAST test results |
| `PolyspaceParser` | CSV / SARIF | Polyspace Bugfinder / CodeProver |
| Coverage parser | Coverage reports | Various coverage tools |
| Compiler log parser | Text logs | GCC, Tasking, GHS compilers |

### Unified TestResult Model

```python
@dataclass
class TestResult:
    test_id: str
    test_name: str
    status: str            # "pass", "fail", "error", "skip"
    duration_ms: float
    module: str
    requirement_id: str    # traceability link
    error_message: str     # if failed
    source_tool: str       # "junit", "vectorcast", "polyspace"
    timestamp: datetime
```

### Processing Pipeline

```
Raw result file (XML/JSON/text)
    │
    ▼
Format detection → select parser
    │
    ▼
Parse → list of TestResult
    │
    ├── Write to FeedbackSink (PostgreSQL)
    │     • failure_patterns table — recurring failures
    │     • auto-detect failure trends
    │
    └── Write to Neo4j
          • Create/update TestResult nodes
          • Link to requirements (VERIFIED_BY)
          • Link to functions (TESTS)
```

---

## 6. Learning Loop

The Review Gate implements a continuous learning loop:

```
1. DA generates response
       │
2. ConfidenceCalculator scores + routes
       │
3. Reviewer evaluates + provides feedback
       │
4. FeedbackSink persists feedback
       │
5. If APPROVE → extract ApprovedPattern → PatternStore (Qdrant)
       │
6. Next time: ConfidenceCalculator checks PatternStore
       │  • similar_approved signal (+5 if match found)
       │  • novel_pattern signal (-15 if no match)
       │
7. Loop: scoring improves as pattern library grows
```

### Failure Pattern Detection

`ResultProcessor` tracks recurring test failures in the `failure_patterns` PostgreSQL table:
- Aggregates failures by test name, module, and error signature
- Detects trends (e.g., "ADC tests failing 3x more this week")
- Surfaces patterns to DAs for proactive debugging (via `get_learning_summary`)

---

## 7. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `confidence.py` | 436 | `ConfidenceCalculator` (scoring), `FeedbackSink` (persistence) |
| `result_processors.py` | 709 | `ResultProcessor`, `JUnitParser`, `VPParser`, `PolyspaceParser` |
