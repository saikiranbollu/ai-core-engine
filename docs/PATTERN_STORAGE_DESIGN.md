# GEST Pattern Storage & Confidence Scoring — Developer Reference

> **Audience:** Developers implementing the GEST pattern extraction and confidence scoring pipeline.
> **Not for LLM consumption** — the LLM uses `PATTERN_EXTRACTION_TEMPLATE.md` instead.
> **Domain:** GEST generates C test code for embedded hardware modules (AURIX TC3xx/TC4xx).

---

## 1. End-to-End Flow

### Phase A — GEST Generates Test Code (Pattern-Assisted)

```
User: "Generate communication test for SPI module"
       │
       ▼
  search_patterns(query="init channel send receive response", module="spi")
       │
       ▼
  Qdrant search:
    Collection: "spi", Filter: data_type = "test_pattern"
    Returns top 3-5 similar patterns (function_sequence + config_enums)
       │
       ▼
  GEST LLM prompt includes proven recipes as examples
    → LLM generates test code
       │
       ▼
  User sees: [ACCEPT]  [REJECT]  [REGENERATE]
```

### Phase B — User Accepts → Pattern Extracted & Stored

```
User presses [ACCEPT]
       │
       ▼
  submit_human_feedback(decision="APPROVE", response_context=<code>, module="spi")
       │
       ▼
  FeedbackSink → PatternExtractor.extract(code, module)
    1. Send template + code to LLM → structured JSON
    2. Validate JSON against schema
    3. Embed api_short names → PatternStore.store()
       │
       ▼
  Qdrant point stored in "{module}" collection (data_type="test_pattern")
  Pattern is now searchable for future GEST queries ✅
```

### Phase C — Confidence Scoring (Pattern-Boosted)

```
Before returning generated test code to user:
       │
       ▼
  PatternStore.find_similar(embedding, module)
       │
       ▼
  If approved pattern found (cosine ≥ 0.8):  pattern_match = True   → +10
  If rejected pattern found (cosine ≥ 0.75): no_failure_match = True → -15
  If nothing found: signal absent → +0
       │
       ▼
  ConfidenceCalculator.evaluate(signals)
  → Score determines: AUTO / QUICK / FULL review
```

---

## 2. Confidence Score — How It Works

The confidence score determines **how much human review** a GEST-generated test needs.
It is a **deterministic formula** — no LLM involved, purely math.

### 2.1 The Formula

```
Final Score = clamp(20 + Σ(weight × signal), 0, 100)
```

> **Why base = 20 (not 50)?** ISO 26262 principle: *assume failure until proven safe.*
> With no evidence, the score must land in FULL review territory (<50).
> A base of 50 would mean "no evidence = skip full review" — unacceptable for
> safety-critical embedded test code.

### 2.2 Signal Weights (7 signals)

| Signal | Weight | Type | Day-1? | What it validates |
|--------|--------|------|--------|-------------------|
| `api_verified` | **+25** | Quality | Yes | All API calls in generated test exist in the module's KG (ratio ≥ 0.95) |
| `call_order_valid` | **+25** | Quality | Yes | API call sequence follows DEPENDS_ON graph — init → channel → enable → send |
| `config_valid` | **+15** | Quality | Yes | Enum values and struct fields in generated test exist in KG (ratio ≥ 0.90) |
| `output_well_formed` | **+5** | Quality | Yes | Code parses as valid C, has required structure (validation score ≥ 80) |
| `pattern_match` | **+10** | Quality | No | Generated API sequence matches an approved test pattern (Qdrant cosine ≥ 0.8) |
| `is_safety_critical` | **-20** | Risk | Yes | ASIL-B+ or structurally complex (DMA, ISR, multi-channel) |
| `no_failure_match` | **-15** | Risk | No | Generated test resembles a previously rejected pattern (Qdrant cosine ≥ 0.75) |

> **Weight balance:** Positive total = +80, Negative total = -35.
> From base 20: all quality signals → 100 (AUTO). No signals → 20 (FULL).
> Worst case (ASIL + failure match, no quality): 20 - 20 - 15 = 0 (FULL).
> The system must **earn** its way out of FULL review with evidence.

#### Why These Weights

- **`call_order_valid` (+25) and `api_verified` (+25):** Biggest weights. Wrong init order
  in embedded test = hardware crash. Calling non-existent function = won't compile on target.
  Grounded in analysis of 24 real CXPI test files — all 24 follow strict `initModule →
  initChannel → enableReception → sendHeader` sequence.
- **`config_valid` (+15):** LLMs commonly hallucinate enum values and struct fields even when
  function names are correct. The KG has Enum → EnumValue and Struct → StructMember nodes
  from SWA parsing to validate against.
- **`output_well_formed` (+5):** Low weight intentionally. Real test code is messy — 16/24
  analyzed files never check return values, 19/24 have no formal pass/fail. Structural
  checks matter less for test code than for production firmware.
- **`pattern_match` (+10):** Future bonus. Not available day 1 (needs approved patterns).
  Grows as feedback loop fills the pattern library.
- **`is_safety_critical` (-20):** Heavy penalty. ASIL-rated module tests need expert review.
- **`no_failure_match` (-15):** Heavy penalty. Repeating a rejected test recipe on hardware
  is actively dangerous. Lower threshold (0.75) catches variations of known-bad patterns.

#### Design Principles

> **Day-1 principle:** The four day-1 signals (`api_verified`, `call_order_valid`,
> `config_valid`, `output_well_formed`) contribute +70 points — enough for AUTO routing
> without any patterns. The system never depends on a signal that doesn't exist yet.
>
> **Safety-critical gate:** For ASIL modules, all four day-1 signals give
> 20 + 70 - 20 = 70 (QUICK). Only when `pattern_match` adds +10 does the score
> reach 80 (AUTO). A human must have previously approved similar test code
> before the system auto-routes safety-critical output.
>
> **Failure memory:** If a human rejects test code and `no_failure_match` triggers on
> the next generation, it subtracts -15. For ASIL modules: 70 - 15 = 55 (QUICK).
>
> **Output-only principle:** Every signal validates the **output** (generated test code),
> not the **input** (whether the database had data). DB gaps are caught automatically —
> no SWA ingested → `api_verified` fails, no dependencies → `call_order_valid` fails,
> no enums/structs → `config_valid` fails.

### 2.3 Routing Thresholds

| Score Range | Review Type | Est. Time | Meaning |
|-------------|-------------|-----------|---------|
| **80–100** | AUTO | ~5 min | High confidence. Quick sanity check only. |
| **50–79** | QUICK | ~15-20 min | Medium confidence. Focused review needed. |
| **0–49** | FULL | ~1+ hour | Low confidence. Detailed expert review required. |

### 2.4 Scoring Examples

**Day 1 — all verified, non-ASIL:**
```
20 + api_verified(25) + call_order_valid(25) + config_valid(15) + output_well_formed(5) = 90 → AUTO
```

**Day 1 — all verified, ASIL module:**
```
20 + 25 + 25 + 15 + 5 - is_safety_critical(20) = 70 → QUICK
```

**Month 3+ — ASIL, pattern matched, all verified:**
```
20 + 25 + 25 + 15 + 5 + pattern_match(10) - 20 = 80 → AUTO (pattern unlocks AUTO for ASIL)
```

**ASIL, failure pattern matched, missing config:**
```
20 + 25 + 25 - 20 - no_failure_match(15) = 35 → FULL
```

**Nothing verified:** `20 = 20 → FULL`

**Worst case:** `20 - 20 - 15 = -15 → clamped to 0 → FULL`

### 2.5 How Patterns Affect the Score

| Scenario | Signal | Effect |
|----------|--------|--------|
| Approved pattern matched | `pattern_match = True` | **+10 points** |
| No approved match | Signal absent | **0 points** (neutral) |
| Rejected pattern matched | `no_failure_match = True` | **-15 points** |
| No rejected match | Signal absent | **0 points** (neutral) |

### 2.6 Special Signal Mappings

Three composite inputs are auto-mapped before scoring:

- **`validation_score`** (int 0–100) → if ≥ 80, sets `output_well_formed = True`
- **`api_match_ratio`** (float 0.0–1.0) → if ≥ 0.95, sets `api_verified = True`
- **`config_match_ratio`** (float 0.0–1.0) → if ≥ 0.90, sets `config_valid = True`

### 2.7 Weights as Code

```python
GEST_WEIGHTS = {
    # Quality signals (positive) — validate the generated test output
    "call_order_valid":     25,   # Init sequence follows correct hardware order
    "api_verified":         25,   # All API calls verified against module's KG
    "config_valid":         15,   # Enum values and struct fields verified against KG
    "output_well_formed":    5,   # Format + compile checks (less critical for tests)
    "pattern_match":        10,   # Matched approved test recipe in pattern library
    # Risk signals (negative)
    "is_safety_critical":  -20,   # ASIL-B+ or DMA/ISR/multi-channel complexity
    "no_failure_match":    -15,   # Resembles a previously rejected test pattern
}

# Usage:
# calculator = ConfidenceCalculator(weights=GEST_WEIGHTS)
# result = calculator.evaluate(signals)
```

### 2.8 The Pattern Flywheel Effect

```
Week 1:  No patterns exist → pattern_match absent → +0 (ASIL stays QUICK)
Week 4:  20 patterns approved → most tests match → +10 bonus
         3 patterns rejected → some failures caught → -15 penalty
Week 8:  50+ approved patterns → nearly everything matches → AUTO by default
         Failure library also grows → dangerous patterns actively avoided
```

### 2.9 Signal Provenance — Where Each Signal Comes From

Every signal is **deterministic** — no LLM, no guesswork. Each one is a boolean
computed from concrete data lookups.

#### Overview

```
  Neo4j (KG)                Qdrant               Validation Engine
  ──────────                ──────               ─────────────────
  Function nodes            Approved patterns     Syntax + structure
  DEPENDS_ON edges          Rejected patterns     checks
  Enum/EnumValue nodes      (data_type=
  Struct/StructMember        "test_pattern")
  Requirement/ASIL
       │                         │                      │
       ▼                         ▼                      ▼
  api_verified       = match ratio ≥ 0.95?        → bool
  call_order_valid   = topo-sort succeeded?       → bool
  config_valid       = enum/struct ratio ≥ 0.90?  → bool
  output_well_formed = val score ≥ 80?            → bool
  pattern_match      = Qdrant cosine ≥ 0.8?       → bool
  is_safety_critical = ASIL ≥ B or complex?       → bool
  no_failure_match   = rejected cosine ≥ 0.75?    → bool
       │
       ▼
  score = clamp(20 + Σ(weight × signal), 0, 100)
  route = AUTO if ≥80 | QUICK if ≥50 | FULL if <50
```

#### Signal Details

| Signal | Data Source | Computation | Threshold |
|--------|-----------|-------------|-----------|
| `api_verified` | Neo4j `Function` nodes | generated fn names ∩ KG fn names → ratio | ≥ 0.95 |
| `call_order_valid` | Neo4j `DEPENDS_ON` edges | Graph traversal + topological sort (Kahn's) | Sort succeeds + order respected |
| `config_valid` | Neo4j `Enum→EnumValue` + `Struct→StructMember` | generated identifiers ∩ KG identifiers → ratio | ≥ 0.90 |
| `output_well_formed` | Validation engine | Rule-based: valid C? Required structure? Template format? → score | ≥ 80 |
| `pattern_match` | Qdrant approved patterns | Embed api_short names (all-MiniLM-L6-v2), cosine search | ≥ 0.8 |
| `is_safety_critical` | Neo4j `Requirement` ASIL + keywords | ASIL ≥ B OR complexity keywords (dma, isr, multi-channel) | Either true |
| `no_failure_match` | Qdrant rejected patterns | Embed api_short names, cosine search rejected patterns | ≥ 0.75 |

**DB gaps caught automatically:** No SWA → `api_verified` fails. No deps → `call_order_valid` fails.
No enums/structs → `config_valid` fails. No rejection history → `no_failure_match` absent → +0.

**No LLM in scoring loop.** The only LLM use is upstream: GEST generating test code and
pattern extractor reading accepted code. Signal computation through routing is pure deterministic logic.

---

## 3. Qdrant Storage Design

### 3.1 Collection Strategy

Store patterns **in the same per-module collection** using a `data_type` payload filter.
No separate collections needed — Qdrant payload filters are O(1)-indexed.

### 3.2 Point Structure

Each test file = one Qdrant point. Payload size: 2–5 KB.

```
Collection: {module}

Point:
  ├── id:      "{module}_{uuid}"
  ├── vector:  384-dim embedding
  └── payload:
       ├── data_type          "test_pattern"     ← filter key
       ├── module             "{module}"
       ├── source_file        "{filename}.c"
       ├── test_name          "<human-readable>"
       ├── test_category      "<category>"
       ├── function_sequence  [{step, api, api_short, phase}, …]
       ├── config_enums       [{struct, field, value, type}, …]
       ├── confidence         0.0
       ├── usage_count        0
       └── created_at         "<iso-timestamp>"
```

### 3.3 Embedding Strategy

Build embedding text from `api_short` names joined with spaces:

```
"initModuleConfig initModule initChannelConfig initChannel enableReception
 sendHeader getChannelStatus receiveResponse clearAllInterrupts"
```

Optionally append enum values (prefix-stripped):

```
" | Loopback_fullDisconnect Mode_master Mode_slave"
```

Embed using `all-MiniLM-L6-v2` (384-dim). Cross-module similarity works automatically
because `api_short` strips the module prefix.

### 3.4 Query Example

```python
results = qdrant.search(
    collection_name=module,
    query_vector=embedder.encode("init channel send header receive response"),
    query_filter=Filter(must=[
        FieldCondition("data_type", match=MatchValue(value="test_pattern")),
    ]),
    limit=5,
)
```

---

## 4. tree-sitter Reference (Alternative Extractor)

| Approach | Accuracy | Needs headers? | Speed |
|----------|----------|---------------|-------|
| Regex | Low | No | Fast |
| clang/libclang | Very high | Yes | Slow |
| **tree-sitter** | High | **No** | **Fast** |

Install: `pip install tree-sitter tree-sitter-c`

**Key queries:**

```scheme
; Function calls
(call_expression function: (identifier) @api_name arguments: (argument_list) @args)

; Enum assignments (dot)
(assignment_expression
  left: (field_expression argument: (identifier) @struct field: (field_identifier) @field)
  right: (identifier) @enum_value)

; Enum assignments (arrow)
(assignment_expression
  left: (field_expression argument: (pointer_expression (identifier) @struct) field: (field_identifier) @field)
  right: (identifier) @enum_value)
```

**Algorithm:** Parse → auto-detect module prefix → find entry function → walk body
in source order → extract function_sequence + config_enums → output JSON.

---

## 5. Pattern Lifecycle

```
User accepts GEST output  ──►  LLM extraction      ──►  Pattern record (JSON)
(ACCEPT button)                (using template)
                                    │
                                    ▼
                              Embed api_short       Qdrant point in {module}
                              names            ──►  (data_type="test_pattern")
                                    │
                                    ▼
                              Next GEST query   ──►  Finds similar patterns
                                    │
                                    ▼
                              Generates better test code using proven recipes
                                    │
                                    ▼
                              If accepted → extract again → library grows
```
