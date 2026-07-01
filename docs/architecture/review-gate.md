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
│  Signals: api_verified, call_order_valid,   │
│  config_valid, output_well_formed,          │
│  pattern_match, is_safety_critical, ...     │
│                                             │
│  base_score = 20                            │
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

Where `base_score = 20` (starts in FULL-review territory; positive output-quality signals must earn confidence upward).

### Signal Weights

Scoring uses a **7-signal output-quality model**. Every signal validates the *generated output*
(not whether the database had data) — if the KG was incomplete, the output signals catch it
(`api_verified` fails when the SWA was never ingested; `call_order_valid` fails when dependencies
are unknown). Two weight profiles ship: `DEFAULT_WEIGHTS` (code generation) and `GEST_WEIGHTS`
(test-code generation).

**Quality signals (positive)**:

| Signal | DEFAULT | GEST | Description |
|--------|---------|------|-------------|
| `api_verified` | +25 | +25 | All API calls exist in the module's KG |
| `call_order_valid` | +25 | +25 | Call/init sequence follows the `DEPENDS_ON` graph |
| `config_valid` | +15 | +15 | Enum values and struct fields verified against the KG |
| `pattern_match` | +10 | +10 | Generated sequence matches an approved pattern |
| `output_well_formed` | +5 | +5 | Code parses and has the required structure |

**Risk signals (negative)**:

| Signal | DEFAULT | GEST | Description |
|--------|---------|------|-------------|
| `is_safety_critical` | -15 | -20 | ASIL-B+ or DMA/ISR/multi-channel complexity |
| `no_failure_match` | -10 | -15 | Resembles a previously rejected pattern |

> Earlier input-quality signals (`has_context`, `missing_requirements`, `missing_hw_spec`) were
> removed as redundant with the output signals. There is **no MISRA signal** — the iLLD workspace
> is reference (non-productive) software.

**Composite signal mapping**: `evaluate()` also accepts scaled inputs — `validation_score`
(0–100, maps to `output_well_formed` when ≥ 80), `api_match_ratio` (maps to `api_verified` when
≥ 0.95), and `config_match_ratio` (maps to `config_valid` when ≥ 0.90).

### evaluate() Method

```python
def evaluate(self, signals: Dict[str, Any], response_id: str = None) -> Dict:
    base_score = 20
    score = base_score
    breakdown = []

    for signal_name, is_present in signals.items():
        if is_present and signal_name in self._weights:   # DEFAULT_WEIGHTS or GEST_WEIGHTS
            weight = self._weights[signal_name]
            score += weight
            breakdown.append({signal_name: weight})

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

- **EDA / CIA** (code generation): `api_verified`, `call_order_valid`, `config_valid`, `output_well_formed`, plus `pattern_match` (uses `DEFAULT_WEIGHTS`)
- **GEST** (test generation): the same 7 signals re-weighted via `GEST_WEIGHTS` (heavier `is_safety_critical` / `no_failure_match` penalties)
- Any DA may raise the `is_safety_critical` and `no_failure_match` risk signals

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

`ResultProcessor` (728 lines) ingests test and analysis results from CI/CD tools and converts them into a unified `TestResult` model.

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
       │  • pattern_match signal (+10 if an approved pattern matches)
       │  • no_failure_match signal (-10 / -15 if it resembles a rejected pattern)
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
| `confidence.py` | 465 | `ConfidenceCalculator` (scoring), `FeedbackSink` (persistence) |
| `result_processors.py` | 728 | `ResultProcessor`, `JUnitParser`, `VPParser`, `PolyspaceParser`, `CoverageParser`, `CompilerParser` |
