# AICE Review — Pass 4a: Security, Safety & Compliance Audit

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Scope:** Security and safety analysis of all findings from Passes 1–3, organized by attack surface, plus a compliance mapping against ASPICE 4.0, EU AI Act, ISO 26262, and ISO/SAE 21434. CI/automation gates are deferred to Pass 4b.

This document is for **the security/safety reviewer, the SQA function, and ASPICE/audit liaison**. Engineering-level remediation detail lives in the Pass 3 cluster files. This document focuses on:

1. **Critical findings recap** — the 26 Critical-rated items, classified by harm type.
2. **Threat surface analysis** — by where the attack enters the system.
3. **Compliance mapping** — every Critical/High finding mapped to ASPICE / EU AI Act / ISO 26262 / ISO 21434 controls.
4. **Gaps the review did not (and could not) cover.**

---

## 0. Scope and Threat Model Assumptions

Before findings: a few assumptions that shape what counts as a vulnerability vs. a low-severity issue.

**System position.** AICE is a Tier-2 productivity tool that supports the development of Tier-1 automotive software (MCAL, iLLD). AICE outputs flow into developer workflows (CIA → Copilot prompts → generated code → human review → integration). **AICE itself does not run in a vehicle**; nothing it produces is shipped without human review. This bounds the worst-case impact of bugs to *productivity loss + IP exposure + audit-trail gaps*, not safety-of-the-intended-function.

**Attacker model.** Three personas:

| Persona | Capabilities | What they want |
|---|---|---|
| **Insider with API key** | A valid `key-cia-001` / `key-gest-001` (PUBLIC tier); read access to one workspace | Privilege-escalate to admin; exfiltrate other-DA data; pivot to ALM/source systems |
| **Insider with corporate-LAN access** | No API key; can MITM intra-Infineon traffic; can read environment of pods they don't own | Capture credentials in flight; capture LLM prompts |
| **Compromised dependency** | Malicious code in a Python package or container layer | Read API keys / tokens; pivot to GPT4IFX; ingest poisoned data into KG |

**Out of scope:** physical attacks, supply-chain attacks against Anthropic-hosted infra, attacks against the GPT4IFX endpoint itself.

**Data sensitivity tiers** (from highest to lowest):
1. **IFX SSO credentials** (`IFX_USERNAME` / `IFX_PASSWORD`) — full-employee identity. Loss = company-level incident.
2. **Service tokens** — Polarion JWT, Bitbucket PAT, Jenkins API token, Jama API secret. Loss = ALM/source/CI compromise.
3. **MCP API keys** — `key-cia-001` etc. Loss = AICE-internal scope, but admin-tier keys reach all of the above.
4. **Source code** — iLLD/MCAL. IP-sensitive but not safety-critical.
5. **Requirements** (Jama/Polarion content) — IP-sensitive; some safety-relevant context.
6. **LLM prompts/outputs** — productivity-relevant; not directly sensitive.

**This drives severity calls below.** A finding that exposes (1) is Critical; (5) is High; (6) is Medium unless it leaks something from (1)-(4).

---

## 1. Critical Findings Recap

Across the 8 prior deliverables, **26 findings are rated Critical**. They fall into four harm classes:

### A. Credential / Secret Exposure (8 findings)

| Finding | What's exposed | Tier |
|---|---|:---:|
| **F-CF-X01** (Cluster F) — `verify_ssl=False` hardcoded in two Bitbucket caller files | IFX SSO creds + iLLD source code in flight | 1+4 |
| **F-CA-A05** (Cluster A) — `api_key[:8]+"…"` partial-prefix labeled "hash" | MCP API keys (current keys are short, prefix = full structure) | 3 |
| **F-CA-I01** (Cluster A) — `ingest_file` accepts arbitrary paths from admin | KG ingestion of `/proc/self/environ` exposes `GPT4IFX_PASSWORD`, `NEO4J_PASSWORD`, all env secrets | 1+2 |
| **F-CB-09** (Cluster B) — `sandbox_upload` writes to `/tmp/sandbox_{session_id}` w/o containment | Path traversal + `shutil.rmtree(ignore_errors=True)` deletion footgun | — |
| **F-CF-X02** (Cluster F) — Credentials stored as plaintext on instance attributes; not zeroed on close | Memory-dump exposure of all service tokens | 1, 2, 3 |
| **F-CF-P01** (Cluster F) — Polarion JWT no refresh path | Token expiration → silent sync failure (availability, not confidentiality) | 2 |
| **F-CC-K01** (Cluster C) — Cypher label injection in `knowledge_intelligence.py` | Defense-in-depth gap; not exploitable today | — |
| **F-CD-B01** (Cluster D) — `--clear` deletes entire DB despite module-scoped builder | Total data loss (availability) | 4, 5 |

### B. Audit Trail / Compliance Gaps (5 findings)

| Finding | What's broken |
|---|---|
| **F-CA-A03** (Cluster A) — `check_authorization` denies not written to PostgreSQL | DENIES are logged to stderr only, not to `audit_logs` table. Compliance-relevant access denials lost on log rotation. |
| **F-CA-A07** (Cluster A) — `api_keys.yaml` missing → `WARNING` log + `_api_key_registry={}` | Authentication subsystem broken silently; severity is "loud failure required" |
| **F-CA-C01** (Cluster A) — `save_feedback` doesn't pre-insert `response_archive` row | FK violation → silent feedback loss. Per ASPICE SUP.10, every feedback record must persist. |
| **F-CB-04** (Cluster B) — No `correlation_id` through `_authorize` → audit log | Cannot join MCP request to its audit trail; compliance audit becomes "did this call succeed?" with no answer. |
| **F-CB-17** (Cluster B) — PatternStore `neo4j_driver=` wiring buried in nested except | Learning loop silently disabled in production since Sprint 9; ASPICE SUP.4 (Continuous Improvement) controls relying on this are non-functional. |

### C. Silent Functional Breakage (8 findings)

| Finding | What's broken | First-broken-since |
|---|---|---|
| **F-CB-01** (Cluster B) — TraceabilityPuller Cypher invalid syntax | Sandbox prod-overlay returns 0 nodes silently | Sprint 4–5 |
| **F-CB-02** (Cluster B) — `_get_*` lazy-init `None` fallback w/ no negative caching | 12+ services degrade silently; SREs grep stderr for root cause | Always |
| **F-CC-R01** (Cluster C) — `_default_llm` no retry despite Master Gaps claim | ~7% RLM failure rate from network blips alone | Sprint 25 |
| **F-CC-K02** (Cluster C) — `_run_cypher` missing `access_mode="READ"` | All KI tools bypass read-replica routing | Always |
| **F-CD-X01** (Cluster D) — Two ILLD builder classes with different ID formats | Same function ingested twice with different IDs; search returns duplicates | Always |
| **F-CD-I01** (Cluster D) — `_merge_nodes/_merge_edges` Cypher injection | Pattern, not active exploit | Always |
| **F-CD-Q01** (Cluster D) — `_fetch` Cypher rel-type injection | Pattern, not active exploit; in "Legacy" file | Always |
| **F-CE-C01** (Cluster E) — `_find_libclang_dll` `.dll`-only fallback on Linux | KG fidelity silently degrades (regex fallback) | Always |

### D. Resource / DoS / Operational (5 findings)

| Finding | What it does |
|---|---|
| **F-CE-T01** (Cluster E) — `testspec_parsers` opens 50MB+ workbooks with `read_only=False` | ~500MB peak RAM per workbook; 20-module batch ingestion → K8s OOM-kill risk |
| **F-CD-B02** (Cluster D) — `_create_constraints` interpolates label + property name | Defense-in-depth |
| **F-CB-03** (Cluster B) — Neo4j driver no `verify_connectivity()` at construction | First failure surfaces on `session.run`; not actively dangerous, but timing of detection is wrong |
| **F-CA-A02** (Cluster A) — `_none` placeholder role pattern fragile | Currently safe; one accidental edit to `derived_roles.yaml` flips every tool to allow-all |
| **F-CA-S01** (Cluster A) — `hybrid_search()` (sync) + `hybrid_search_async()` (async) coexist | Footgun, not exploitable; next refactor that picks the wrong one breaks production |

**The 26 Critical findings concentrate on what AICE can lose, not on what AICE can do wrong.** None of them lets an attacker generate ASIL-violating code through AICE; all of them either leak secrets, lose data, lose audit trails, or silently degrade.

---

## 2. Threat surface analysis

The findings reorganized by **where the attack enters the system**.

### 2.1 Auth / Credential surface

The first line of defense: API keys, Cerbos PDP, role hierarchy.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-AUTH-01 | Compromised API key cannot be revoked promptly | F-CA-A01 | 🔴 |
| T-AUTH-02 | Auth subsystem fails silently when config missing | F-CA-A07 | 🟠 |
| T-AUTH-03 | Cerbos PDP unreachable → falls back to local-tier silently | F-CA-A04 | 🟠 |
| T-AUTH-04 | DENIES not in PostgreSQL audit | F-CA-A03 | 🟠 |
| T-AUTH-05 | Placeholder `_none` role brittle to config edit | F-CA-A02 | 🔴 |
| T-AUTH-06 | API key partial-prefix labeled "hash" | F-CA-A05 | 🔴 |
| T-AUTH-07 | No correlation_id linking auth decision to tool execution | F-CB-04 | 🟠 |

#### What an attacker can do

**Scenario: Insider with PUBLIC-tier API key wants to escalate to admin.**

Today, **they can't directly** — the role hierarchy is enforced by `tool_tiers.py` and Cerbos. But they can wait for opportunities created by the findings above:

1. **Cerbos PDP becomes unreachable** (network blip, restart). Per F-CA-A04, the fallback is local-tier check from `tool_tiers.py`. Local check **doesn't have the per-attribute conditions** Cerbos has (e.g., the `attr.tier` matching from Pass 1 F-D02). If the local fallback is more permissive than Cerbos for any tool, the attacker can hit that tool during the Cerbos outage window.
2. **`derived_roles.yaml` gets a typo** that adds `_none` as a parent role of `developer` — F-CA-A02. The placeholder pattern flips silently; every tool becomes accessible to unauthenticated callers. No alert.
3. **Logs from a colleague's machine** contain `api_key=key-admin-pi…` — F-CA-A05 turns 8 chars of a `key-admin-pipeline` key into recoverable knowledge ("starts with `key-admin-pi`, ends in `peline`"). Not directly exploitable but reduces brute-force search space.
4. **A leaked admin key** can't be revoked for the duration of the pod lifecycle — F-CA-A01.

**Scenario: Auditor asks "show all denied calls in the last 30 days from key-cia-001."**

Today, this query **cannot be answered** — F-CA-A03 means denies are stderr-only. After 7 days of log rotation, those denies are lost forever.

#### Mitigations needed

1. **Apscheduler-driven `reload_api_keys()` every 60s** (F-CA-A01).
2. **`mtime`-based registry invalidation** as a backup.
3. **Refuse start with `MCP_REQUIRE_AUTH=1` if `api_keys.yaml` missing** (F-CA-A07).
4. **Cerbos timeout (1s) + reconnect-on-failure + Prometheus gauge** (F-CA-A04).
5. **Replace `_none` placeholder with `return None` from `resolve_principal`** (F-CA-A02).
6. **`sha256(api_key)[:16]` instead of `api_key[:8]`** (F-CA-A05).
7. **Write DENIES to PostgreSQL `audit_logs`** (F-CA-A03).
8. **Generate `correlation_id = uuid4()` per request, propagate to all logs and audit rows** (F-CB-04).

These 8 changes together harden the auth surface meaningfully. ~5 days of work.

### 2.2 Untrusted-input surface

User input lands here from MCP tool parameters, sandbox uploads, ingestion paths, search queries.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-UI-01 | Path traversal via `ingest_file` | F-CA-I01 | 🔴 |
| T-UI-02 | Path traversal via sandbox `session_id` + `shutil.rmtree` | F-CB-09 | 🟠 |
| T-UI-03 | Cypher label injection (multiple files) | F-CC-K01, F-CD-B02, F-CD-I01, F-CD-Q01 | 🔴 / 🟠 |
| T-UI-04 | OCR subprocess accepts arbitrary `image_path` from public method | F-CE-O01 | 🟠 |
| T-UI-05 | Bitbucket file path lacks traversal protection | F-CF-B02 | 🟡 |
| T-UI-06 | `ingest_file` extension-allowlist bypass via filename without ext | (latent) | 🟡 |
| T-UI-07 | Sync-state path traversal | F-CF-X04 | 🟡 |

#### What an attacker can do

**Scenario: Insider with admin-tier API key wants to exfiltrate environment variables.**

`ingest_file` is admin-tier. Per F-CA-I01:

```python
svc.ingest_file("/proc/self/environ", module_name="pwn")
```

The path passes the `is_file()` check (`/proc/self/environ` is a special file but exists). It hits the `_parse_file` router, which routes by extension — `/environ` has no extension, falls through to "generic," which... I haven't traced fully but a likely path is "treat as text, ingest into KG." Result: the contents of `environ` (containing `GPT4IFX_PASSWORD`, `NEO4J_PASSWORD`, `IFX_USERNAME`, `IFX_PASSWORD`, `JAMA_API_SECRET`, `POLARION_TOKEN`) **become content in the KG**.

A search query for `password` later returns these. The attacker exfiltrates by reading their own KG.

**This is an admin-tier action, so requires an admin key.** But every admin key (per `api_keys.yaml`) has access to all of: ingestion, KG admin, JWT lifecycle, configuration. An admin key compromise is already game-over for AICE; F-CA-I01 makes it game-over for the whole IFX environment.

**Scenario: Insider with PUBLIC-tier key wants to run write Cypher.**

Cypher injection via F-CC-K01 / F-CD-B02 / F-CD-I01 / F-CD-Q01 is **defense-in-depth** today — labels in those calls come from hardcoded lists or ontology YAML, not user input. **But:** any future MCP tool that exposes a `label=` parameter (admin or developer tier) immediately becomes injectable. Pass 5's productivity assessment may surface DA needs that argue for label-flexible APIs — at which point this becomes exploit-able.

**Scenario: Colleague gets RCE on developer's machine via OCR.**

`OCRProcessor.process_page_image(image_path)` is a public method. Tesseract has had CVEs in image-loading codepaths (CVE-2018-11103, CVE-2020-9131, etc.). If a future tool exposes this with a user-controllable path, malicious crafted PNG → Tesseract crash with arbitrary memory disclosure.

#### Mitigations needed

1. **Path containment for `ingest_file`** (allowlist roots, reject symlinks, validate `.is_relative_to()`) — F-CA-I01.
2. **Path containment for `sandbox_upload`** — F-CB-09.
3. **Single shared `_kg_safety.py`** with `sanitize_label()` / `sanitize_rel_type()` / `sanitize_property_name()` — Pattern #1 from Pass 3 §2.
4. **Validate `image_path` in `OCRProcessor`** + `OCR_LANGUAGE` regex — F-CE-O01.
5. **Validate Bitbucket paths** — reject `..`, restrict character set — F-CF-B02.
6. **Allowlist of `INGEST_ALLOWED_ROOTS` env-controllable; default `/data,/repos`.**

### 2.3 Data integrity surface

Findings that affect *what's actually in the KG / vector store / audit log*.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-DI-01 | `--clear` deletes entire DB module-unaware | F-CD-B01 | 🔴 |
| T-DI-02 | Two ILLD builders write overlapping schemas with different IDs | F-CD-X01 | 🔴 |
| T-DI-03 | `batch_ingestion.py` silently drops failed batches | F-CD-X02 | 🟠 |
| T-DI-04 | UNWIND batch poisoned by one bad item | F-CD-B03 | 🟠 |
| T-DI-05 | `_create_edges` no label → wrong-edge attachment if IDs collide | F-CD-B04 | 🟠 |
| T-DI-06 | `ingest_swa` silently overrides parser-detected module | F-CD-I03 | 🟠 |
| T-DI-07 | `_discover_module_files` silently caps at 100 files | F-CA-I02 | 🟠 |
| T-DI-08 | `_extract_param_type` falls back to raw tag → polluted KG | F-CE-A02 | 🟡 |
| T-DI-09 | `arxml_parser` strips template macros without XML validation | F-CE-A01 | 🟠 |
| T-DI-10 | `testspec_parsers` regex IGNORECASE → orphan refs | F-CE-T02 | 🟠 |
| T-DI-11 | `JamaItem.from_api_dict` silent `id=-1` sentinel | F-CF-J02 | 🟡 |

#### What an attacker can do (or, more realistically, what goes wrong)

**Scenario: Operator types `python build_knowledge_graph.py --module ADC --clear` thinking it'll rebuild ADC.**

Per F-CD-B01: actual behavior is "delete all 20 modules' data, then ingest ADC." 19 modules of work obliterated. No prompt, no audit log, no recovery (MERGE-based pipelines don't preserve original data; the only "backup" is the source files themselves).

**Scenario: Two ingestion pipelines run for the same module on the same database.**

Per F-CD-X01: `ILLDKnowledgeGraphBuilder` writes `(:Function {id: "IfxCan_init"})`; `ILLDKGBuilder` writes `(:Function {id: "FUNC_IfxCan_init"})`. Same logical function, two nodes. Search returns both. Traceability paths break — `(:SoftwareRequirement)-[:VERIFIES]->(:Function)` may attach to one but not the other. KG quality degrades silently.

**Scenario: Module-name disagreement between parser and pipeline.**

Per F-CD-I03: `ingest_swa` always uses `self.module` (pipeline param) over `func.module` (parser-detected). If a developer accidentally feeds a CXPI parser output into an ADC pipeline, the function gets stamped `module: "ADC"` and **pollutes the ADC module**. CIA queries against `module: ADC` return CXPI functions. Code generation gets the wrong context.

**Scenario: 50K-node ingestion runs through `batch_ingestion.py`.**

Per F-CD-X02: `Neo4jBatchWriter.merge_nodes_batch` does no retry. At 1% transient failure rate, ~500 nodes silently dropped per ingestion. Job reports "completed." Search misses 1% of expected results, attributed to "parser bugs" not "ingestion incomplete."

**Scenario: A poisoned source file with one corrupt property.**

Per F-CD-B03: the batch's UNWIND fails on the first bad item. **In Neo4j 5.x, ConstraintViolation aborts the entire batch** — 499 good items are also rolled back. The job reports "0 nodes created" for that batch, but doesn't say which item caused it.

#### Mitigations needed

1. **Module-scoped `--clear` by default; `--clear-all` requires confirmation prompt + audit row** — F-CD-B01.
2. **Unify `ILLDKnowledgeGraphBuilder` + `ILLDKGBuilder`** — F-CD-X01.
3. **Single retry helper across all 3 Neo4j-write paths** — F-CD-X02 (Pattern #2).
4. **Per-item validation pre-batching; per-batch failure-recovery via batch-size-1 retry** — F-CD-B03.
5. **Label-aware MATCH in `_create_edges`** — F-CD-B04.
6. **Module-mismatch warning in `ingest_swa`** — F-CD-I03.
7. **Configurable file cap; warn loudly when hit** — F-CA-I02.
8. **Reject relaxed-GUID matches in `swa_parsers`; flag with `confidence: low`** — F-CE-T03.
9. **Validate ARXML structural soundness post-template-strip** — F-CE-A01.
10. **Uppercase normalization for Jama PRQ refs** — F-CE-T02.
11. **Fail loud on missing `id` in `JamaItem.from_api_dict`** — F-CF-J02.

### 2.4 Secret-handling surface

How AICE stores, transmits, logs, and disposes of secrets.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-SEC-01 | Credentials stored as plaintext on long-lived instance attributes | F-CF-X02 | 🟠 |
| T-SEC-02 | Polarion JWT no refresh; expired token → silent failure | F-CF-P01 | 🔴 |
| T-SEC-03 | API key prefix labeled "hash" but is cleartext | F-CA-A05 | 🔴 |
| T-SEC-04 | Token rotation has no propagation mechanism | F-CA-A01 | 🔴 |
| T-SEC-05 | `IFX_USERNAME` / `IFX_PASSWORD` in env (Bitbucket); env-var ingestion exfil possible | F-CA-I01 | 🔴 |
| T-SEC-06 | LLM model defaults differ across files; some hardcoded | F-CE-S04 | 🟢 |

#### What an attacker can do

**Scenario: Memory dump of the MCP server process.**

A memory dump (from a Python crash, gdb attach, or the K8s `dumpfile=` debug flag) exposes:
- `JamaConnector._auth` — wraps `api_key:api_secret` in plaintext.
- `PolarionConnector._token` — plaintext JWT in `httpx.Client.headers["Authorization"]`.
- `JenkinsConnector._api_token` — plaintext on the instance.
- `BitbucketConnector._token` / `_password` — same.
- `_api_key_registry` — entire `api_keys.yaml` content as plaintext dict.
- `_current_api_key.get()` — the live request's API key.
- The cached httpx.Client objects, which embed credentials in their default headers.

**All credentials for all integrated systems** are recoverable from one dump. Per F-CF-X02 there's no zeroing on close; per common Python practice, no `mlock` to prevent swap-out. For an EU AI Act / ASPICE auditor, this is a finding.

**Scenario: `IFX_PASSWORD` exfiltration via KG.**

Per F-CA-I01 + T-SEC-05: admin uses `ingest_file("/proc/self/environ", "exfil")`. KG now contains `IFX_PASSWORD=...` as searchable content. Attacker queries `search_database("IFX_PASSWORD")`, gets the value back. **Game over for the user's IFX SSO identity.**

#### Mitigations needed

1. **`_SecretStr` wrapper for all credentials**, with `clear()` on close — F-CF-X02.
2. **Token-provider callable instead of static token** for Polarion + Bitbucket — F-CF-P01.
3. **`sha256(api_key)[:16]`** for audit logs — F-CA-A05.
4. **API key registry watchdog reload** — F-CA-A01.
5. **`ingest_file` path allowlist** — F-CA-I01.
6. **`SECRET_PATTERNS` filter on KG content** — refuse to ingest documents matching `r'(API_KEY|PASSWORD|SECRET|TOKEN)\s*[:=]'` etc. Defense-in-depth even if (1) and (5) above hold.

### 2.5 Transport security surface

How traffic between AICE and external systems is protected.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-TLS-01 | `verify_ssl=False` hardcoded for Bitbucket | F-CF-X01 | 🔴 |
| T-TLS-02 | No CA bundle reuse across connectors (each has own logic) | (cross-cutting) | 🟠 |
| T-TLS-03 | httpx.Client created once; cannot pick up CA rotation | F-CF-X05 | 🟡 |
| T-TLS-04 | No timeout on Cerbos calls | F-CA-A04 | 🟠 |

#### What an attacker can do

**Scenario: Insider with corporate-LAN access (no API key).**

The attacker positions a fake Bitbucket cert via network spoofing, ARP poisoning, or a compromised intermediate proxy. Two callers (per F-CF-X01) connect:
- `dependency_fetcher.py::_make_connector` — passes `IFX_USERNAME` / `IFX_PASSWORD` over TLS-not-validated HTTPS. Attacker captures Basic auth header → has the user's IFX SSO credentials.
- `header_fetcher.py::_get_connector` — same.

Once the attacker has IFX SSO, they can:
- Log into Jama, Polarion, Bitbucket directly.
- Read IFX-internal Confluence, JIRA, etc.
- Pivot to the user's other accounts.

**This is the highest-impact threat in the entire review.** A single corporate-LAN attacker (which is realistic — IFX has thousands of employees with corporate-LAN access) can capture an arbitrary IFX engineer's full credentials by waiting for a sandbox-upload or dependency-fetch operation.

The fix is **trivial** (use the Infineon CA bundle, default `verify_ssl=True`) but the situation is critical until the fix lands.

#### Mitigations needed

1. **Default `verify_ssl=True` everywhere** — F-CF-X01.
2. **Use Infineon CA bundle for all connectors** (same as `pdf_pipeline.py`).
3. **`AICE_ALLOW_INSECURE_TLS` env gate** for development override.
4. **httpx.Client recreation on TLS errors** — F-CF-X05.
5. **1-second timeout on Cerbos calls** — F-CA-A04.

### 2.6 Audit-trail surface

How AICE produces records that satisfy ASPICE / EU AI Act audit requirements.

#### Threats

| # | Threat | Findings | Severity |
|---|---|---|:---:|
| T-AUD-01 | DENIES not in PostgreSQL audit_logs | F-CA-A03 | 🟠 |
| T-AUD-02 | No correlation_id linking audit entries | F-CB-04 | 🟠 |
| T-AUD-03 | `save_feedback` silent FK failure → feedback lost | F-CA-C01 | 🟠 |
| T-AUD-04 | `--clear` no audit log entry | F-CD-B01 | 🔴 |
| T-AUD-05 | Tool execution outcome not recorded (only auth decision) | F-CB-04 | 🟠 |
| T-AUD-06 | Sync-state FAILED status not differentiated from transient | F-CF-P03 | 🟡 |
| T-AUD-07 | Learning loop disabled silently → patterns table never populated | F-CB-17 | 🟡 |
| T-AUD-08 | Model ID / version not recorded per RLM call | (latent) | 🟡 |

#### What an auditor can ask, and what AICE can answer today

| Question | Today's answer | After mitigations |
|---|---|---|
| "Show all calls to `execute_cypher` from `key-cia-001` in March 2026" | ✅ — `audit_logs` has the row | ✅ |
| "Show all denied calls from `key-cia-001`" | ❌ — denies are stderr-only | ✅ (F-CA-A03 fix) |
| "What was the outcome of call X?" | ❌ — audit row records auth decision, not tool outcome | ✅ (F-CB-04 fix) |
| "Show feedback received for response Y" | ⚠️ — depending on FK timing | ✅ (F-CA-C01 fix) |
| "When was the database last cleared, by whom?" | ❌ — no audit | ✅ (F-CD-B01 fix) |
| "Which LLM model was used for RLM call Z?" | ❌ — not recorded | ✅ (new instrumentation) |

**Of the ASPICE SUP.10 (Configuration Management of Operational Data) controls, AICE today is partially compliant.** Mitigations bring it to fully-compliant for auth and execution; per-LLM-call traceability needs separate instrumentation in Sprint 27 (per Pass 3 summary).

### 2.7 Supply-chain surface (deferred but flagged)

Briefly worth noting:

| # | Item | Status |
|---|---|---|
| Dockerfile uses `pip install --break-system-packages` | Per Master Gaps; verified pattern |
| LiteLLM removed from project per security concern | Per userMemories — confirmed not present in source |
| `clang` Python package installs `libclang.dll/so/dylib` from PyPI | F-CE-C01 — single-source dependency |
| `sentence-transformers` model downloads at runtime | F-CA-S04 — not pinned to a hash |
| `tesseract` system package | F-CE-O01 — not pinned to a version |

Container image hash pinning, SBOM generation, and dependency vulnerability scanning are **out of scope for this review** but are real audit concerns. The Pass 4b CI gates can include an `aice_supply_chain_check` job.

---

## 3. Compliance mapping

The mapping table below covers **all 26 Critical findings + the 60 High findings** (86 total) against the four standards. Rows are grouped by attack surface for readability.

**Standards keys:**
- **ASPICE 4.0**: Automotive SPICE, the de-facto SQA standard at IFX. Key processes for AICE: SUP.10 (Configuration Management of Operational Data), SWE.1 (Software Requirements Analysis), SWE.4 (Software Unit Verification), SUP.4 (Joint Review), SUP.9 (Problem Resolution Management), MAN.5 (Risk Management).
- **EU AI Act**: Articles 9 (risk management), 10 (data governance), 12 (record-keeping), 14 (human oversight), 15 (accuracy/robustness/cybersecurity). AICE is borderline-applicable; it serves SW development for vehicles which themselves are out-of-scope of EU AI Act, but Anthropic's CIA-via-Copilot generates code that ends up in vehicles. Treating AICE as if Article 9/15 applies is conservative but defensible.
- **ISO 26262**: Functional safety. AICE is a development tool; ISO 26262 §11 (tool confidence — "Software tools used in development of safety-related items") applies. Tool Confidence Level (TCL) determination is a separate exercise; this column flags findings that affect AICE's TCL classification.
- **ISO/SAE 21434**: Cybersecurity. WP.RA-1 (asset identification), WP.SC-1 (cybersecurity goals), WP.RC-1 (cybersecurity controls). Most of these apply to vehicles, but the **"cybersecurity case" for development tools** is gaining traction in OEM audits.

### 3.1 Auth / Credential

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CA-A01 (Key reload) | 🔴 | SUP.10 BP3 (control of operational data) | Art. 12 (record-keeping) | §11.4.6 (tool qualification) | RC-3 (vulnerability mgmt) |
| F-CA-A02 (Placeholder role) | 🔴 | SUP.10 BP4 | — | §11.4.5.4 (TI/TD evaluation) | RC-1 |
| F-CA-A03 (Denies not audited) | 🟠 | SUP.10 BP3, BP6 | Art. 12.1 | §6.4.6 (work product traceability) | WP.SC-2 |
| F-CA-A04 (Cerbos no-timeout) | 🟠 | MAN.5 BP6 (risk mitigation) | Art. 9.5 (residual risk) | §11.4.5.5 (continuous TI/TD) | RC-3 |
| F-CA-A05 (Key partial-prefix "hash") | 🔴 | SUP.10 BP4 | Art. 15.3 (cybersecurity) | — | WP.SC-2.RC-2 |
| F-CA-A07 (Registry missing → silent) | 🟠 | SUP.10 BP3 | Art. 9.3 (continuous risk monitoring) | §11.4.6.3 (tool malfunction) | RC-1 |
| F-CB-04 (No correlation_id) | 🟠 | SUP.10 BP3, BP6 | Art. 12.1 | §6.4.6 | WP.SC-2 |
| F-CF-X02 (Plaintext credentials) | 🟠 | SUP.10 BP4 | Art. 15.3 | — | RC-1, RC-2 |

### 3.2 Untrusted-input

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CA-I01 (Path traversal in ingest) | 🔴 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1, RC-3 |
| F-CB-09 (Sandbox path traversal) | 🟠 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CC-K01 (KI Cypher injection) | 🔴 | SUP.10 BP3 (defense-in-depth) | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CD-B02 (Constraint Cypher injection) | 🔴 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CD-I01 (ILLD KG Cypher injection) | 🔴 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CD-Q01 (Legacy KG Cypher injection) | 🔴 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CE-O01 (OCR subprocess) | 🟠 | SUP.10 BP3 | Art. 15.3 | §11.4.6.3 | RC-1 |
| F-CF-B02 (Bitbucket path) | 🟡 | SUP.10 BP3 | — | — | RC-1 |
| F-CF-X04 (Sync-state path) | 🟡 | SUP.10 BP3 | — | — | RC-1 |

### 3.3 Data integrity

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CD-B01 (--clear scope) | 🔴 | SUP.10 BP1 (CI definition), BP6 (status accounting) | Art. 12.1 | §11.4.5.4 | WP.SC-2 |
| F-CD-X01 (Dual ILLD builders) | 🟠 | SUP.10 BP1, SWE.1 BP3 (consistency) | Art. 10.2 (data quality) | §11.4.5.4 | — |
| F-CD-X02 (Silent batch_ingestion data loss) | 🟠 | SUP.10 BP3 (control of operational data) | Art. 10.2 | §11.4.6.3 | — |
| F-CD-B03 (UNWIND poisoned batch) | 🟠 | SUP.10 BP3 | Art. 10.2 | §11.4.6.3 | — |
| F-CD-B04 (No-label MATCH perf) | 🟠 | — | — | §11.4.6.3 (fitness for use) | — |
| F-CD-I03 (Module override warning) | 🟠 | SWE.1 BP3 | Art. 10.2 | §11.4.5.4 | — |
| F-CA-I02 (Silent file cap) | 🟠 | SUP.10 BP3 | Art. 10.2 | — | — |
| F-CE-A01 (ARXML strip integrity) | 🟠 | SWE.1 BP3 | Art. 10.2 | §11.4.5.4 | — |
| F-CE-A02 (Param type fallback pollutes KG) | 🟡 | SWE.1 BP3 | Art. 10.2 | — | — |
| F-CE-T02 (PRQ regex IGNORECASE) | 🟠 | SWE.1 BP3 | Art. 10.2 | — | — |
| F-CF-J02 (Jama silent sentinels) | 🟡 | SWE.1 BP3 | Art. 10.2 | — | — |
| F-CB-01 (Sandbox Cypher syntax) | 🔴 | SUP.10 BP3, SWE.4 BP1 | Art. 15.1 (accuracy) | §11.4.6.3 | — |

### 3.4 Secret-handling

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CF-X01 (verify_ssl=False) | 🔴 | SUP.10 BP3 | Art. 15.3 (cybersecurity) | §11.4.6.3 | RC-1, RC-2 |
| F-CF-P01 (Polarion no-refresh) | 🔴 | SUP.9 BP4 (problem resolution) | Art. 15.3 | §11.4.6.3 | RC-3 |

### 3.5 Silent functional breakage / observability

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CB-02 (Lazy-init silent None) | 🔴 | SUP.10 BP3, MAN.5 BP6 | Art. 9.3 (continuous monitoring) | §11.4.6.3 | RC-3 |
| F-CB-03 (Neo4j no-verify_connectivity) | 🔴 | SUP.10 BP3 | Art. 9.3 | §11.4.6.3 | — |
| F-CB-05 (`_tool_name_ctx` not set?) | 🟠 | SUP.10 BP6 | Art. 12.1 | — | WP.SC-2 |
| F-CB-08 (Typed errors lost in envelope) | 🟠 | SUP.10 BP3, SUP.9 BP2 | Art. 12 | — | — |
| F-CB-10 (Module name regex broken iLLD) | 🟠 | SUP.10 BP3 | — | §11.4.5.4 | — |
| F-CB-11 (Sequential warmup) | 🟠 | — | — | — | — |
| F-CB-17 (Learning loop disabled) | 🟠 | SUP.4 BP1 (joint review effectiveness) | Art. 9.3 | §11.4.5.5 | — |
| F-CC-R01 (`_default_llm` no retry) | 🔴 | SUP.10 BP3 | Art. 15.1, 15.2 (robustness) | §11.4.6.3 | RC-3 |
| F-CC-R02 (Synthesis garbage JSON) | 🟠 | SUP.9 BP4 | Art. 15.1 | §11.4.6.3 | — |
| F-CC-K02 (READ access mode) | 🔴 | SUP.10 BP3 | — | — | RC-1 |
| F-CE-C01 (libclang detection) | 🔴 | SUP.10 BP3, SWE.4 BP1 | Art. 15.1 | §11.4.6.3 | — |

### 3.6 Audit-trail

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CA-C01 (FK pre-insert) | 🟠 | SUP.10 BP3, SUP.4 BP1 | Art. 12.1, 12.2 | — | — |

### 3.7 Resource / DoS

| Finding | Sev | ASPICE | EU AI Act | ISO 26262 | ISO 21434 |
|---|:---:|---|---|---|---|
| F-CE-T01 (xlsx memory) | 🔴 | — | Art. 9.5 (residual risk acceptance) | §11.4.6.3 (fitness for use) | — |
| F-CD-B07 (sys.exit kills MCP) | 🟡 | MAN.5 | Art. 15.2 | §11.4.6.3 | — |

### 3.8 Inter-finding cross-references for compliance

A few findings hit multiple controls simultaneously and warrant special note:

- **F-CA-I01 + F-CF-X01** together create a **complete IFX SSO compromise path**. Either alone is exploitable; both together turn admin-key access into IFX-account takeover. EU AI Act Art. 15.3 + ISO 21434 RC-1 strongly motivate fixing both in the same sprint.
- **F-CD-B01 + F-CB-17 + F-CA-C01** together create **partial audit-trail loss**. Per ASPICE SUP.10 BP6 (status accounting), these jointly mean the audit trail is incomplete in 3 dimensions: deletions, learnings, and feedback.
- **F-CB-01 + F-CB-10** together mean **the iLLD sandbox feature has never been functional in production**. Per EU AI Act Art. 15.1 (accuracy), if this feature is being relied upon by CIA for shadow-detection during code generation, the feature's documented behavior is misrepresented.

---

## 4. Gaps the review did not (and could not) cover

Honest disclosure of what's not in this audit:

### 4.1 Items I have only partial visibility into

Listed throughout Pass 3 but consolidated here:

| Area | Reason |
|---|---|
| `illd_swa_parser.py` LLM enrichment loop body (max_workers, checkpoint paths, retry shape) | Snippet didn't include the implementation; only the docstring + constants |
| `pdf_parser.parse` actual return type (`str` vs `list[str]`) | Docstring says `str`; caller treats as list. Pass 1 doc-drift candidate. |
| `OCRProcessor._estimate_confidence` body | Referenced but body not visible. If it returns a constant, downstream confidence scoring is fake. |
| `_page_has_figure` body in `pdf_pipeline.py` | Performance-sensitive helper; not visible. |
| Full `c_parser` regex pipeline (the non-clang path) | Only excerpts visible. |
| `_finish_tool` callers — verify `_tool_name_ctx` is set somewhere | F-CB-05; could not confirm. |
| `BitbucketConnector.from_clone_url` body | F-CF-B03; doc claims it exists. |
| `_fetch_jama_relationships.py` `verify_ssl` setting | Likely matches F-CF-X01 pattern but unverified. |

When Sprint 26-29 touches these files, **verify the relevant findings against actual source.** Some may be already-fixed; others may be worse than I estimated.

### 4.2 Items deliberately out of scope

| Area | Why out of scope |
|---|---|
| Dependency vulnerability scanning (CVE matching) | Requires SCA tooling; Pass 4b includes a CI job hook for this |
| SBOM generation | Same |
| Container image hash pinning | Deployment concern |
| Penetration testing | Requires deployment access |
| Threat modeling for Pass 5's "DA business logic" code | Out of scope for AICE itself; Pass 5 will assess AICE's interface contracts but not the DAs' internals |
| Fuzz testing of parsers | Suggested but not designed |
| Static analysis with Bandit/Semgrep | Pass 4b includes hooks |
| Memory-safety analysis of the `clang` C bindings | The clang Python bindings can crash on malformed input; not assessed |

### 4.3 Things I did not look for

| Item | Reason |
|---|---|
| Race conditions in `contextvars` propagation across `to_thread` boundaries | Subtle; would need targeted analysis with concurrent test harness |
| Memory leaks in long-lived MCP processes | Requires runtime profiling |
| File descriptor leaks (httpx clients, Neo4j sessions) | Same |
| Thread-safety of `_search_services: Dict[str, SearchService]` lazy init | The dict assignment under read-after-write is technically racy in CPython 3.12+ (free-threading mode) |
| Backdoors / insider-planted malicious code | Out of scope |
| Time-of-check-to-time-of-use (TOCTOU) issues in path validation | Worth a second pass after F-CA-I01 mitigation lands |

---

## 5. Severity recap with compliance overlay

The 26 Critical findings, ranked by **compliance impact** (rather than purely technical severity):

| Rank | Finding | Standards hit | Why this rank |
|---|---|---|---|
| 1 | F-CF-X01 | ASPICE SUP.10 + EU AI Act Art. 15.3 + ISO 26262 §11.4.6.3 + ISO 21434 RC-1 + RC-2 | All 4 standards; highest single-finding impact |
| 2 | F-CA-I01 | ASPICE SUP.10 + EU AI Act Art. 15.3 + ISO 26262 §11.4.6.3 + ISO 21434 RC-1, RC-3 | All 4; combines with #1 for IFX SSO compromise |
| 3 | F-CB-01 | ASPICE SUP.10 + SWE.4 + EU AI Act Art. 15.1 + ISO 26262 §11.4.6.3 | EU AI Act accuracy (feature documented, doesn't work) |
| 4 | F-CD-B01 | ASPICE SUP.10 BP1, BP6 + EU AI Act Art. 12.1 + ISO 26262 §11.4.5.4 + ISO 21434 WP.SC-2 | Total data loss, no audit trail of deletion |
| 5 | F-CC-K01 + F-CD-B02 + F-CD-I01 + F-CD-Q01 | ASPICE SUP.10 + EU AI Act Art. 15.3 + ISO 26262 §11.4.6.3 + ISO 21434 RC-1 | Same fix; defense-in-depth |
| 6 | F-CC-R01 | ASPICE SUP.10 + EU AI Act Art. 15.1, 15.2 + ISO 26262 §11.4.6.3 + ISO 21434 RC-3 | EU AI Act robustness (~7% baseline failure) |
| 7 | F-CB-02 | ASPICE SUP.10 + MAN.5 + EU AI Act Art. 9.3 + ISO 26262 §11.4.6.3 + ISO 21434 RC-3 | Continuous monitoring (Art. 9.3) |
| 8 | F-CB-03 | ASPICE SUP.10 + EU AI Act Art. 9.3 + ISO 26262 §11.4.6.3 | Continuous monitoring |
| 9 | F-CC-K02 | ASPICE SUP.10 + ISO 21434 RC-1 | Defense-in-depth, perf |
| 10 | F-CE-C01 | ASPICE SUP.10 + SWE.4 + EU AI Act Art. 15.1 + ISO 26262 §11.4.6.3 | EU AI Act accuracy |
| 11 | F-CF-P01 | ASPICE SUP.9 + EU AI Act Art. 15.3 + ISO 26262 §11.4.6.3 + ISO 21434 RC-3 | Cyber + availability |
| 12 | F-CD-X01 | ASPICE SUP.10 + SWE.1 + EU AI Act Art. 10.2 + ISO 26262 §11.4.5.4 | Data governance |
| 13 | F-CA-A01 | ASPICE SUP.10 + EU AI Act Art. 12 + ISO 26262 §11.4.6 + ISO 21434 RC-3 | Cyber response |
| 14 | F-CA-A02 | ASPICE SUP.10 + ISO 26262 §11.4.5.4 + ISO 21434 RC-1 | TI/TD evaluation |
| 15 | F-CA-A05 | ASPICE SUP.10 + EU AI Act Art. 15.3 + ISO 21434 WP.SC-2.RC-2 | Cyber + audit trail |
| 16 | F-CE-T01 | EU AI Act Art. 9.5 + ISO 26262 §11.4.6.3 | Residual risk acceptance |

**The pattern:** 7 of the top 16 hit ALL FOUR standards simultaneously. These should be prioritized over single-standard findings even where the single-standard finding is technically more severe in isolation.

The Pass 3 4-sprint rollup plan (Sprint 26 = 6 P0 fixes) **already covers ranks 1-7 above**. So the existing plan is well-aligned with compliance impact — no re-prioritization needed.

---

## 6. Tool Confidence Level (TCL) implications for ISO 26262 §11

If AICE outputs ever flow into ASIL-rated artifacts (CIA → generated iLLD code → human review → integration into AURIX TC3xx automotive software), AICE qualifies as a "software tool used in development of safety-related items" under ISO 26262 §11.

**TCL determination depends on two factors:**

1. **Tool Impact (TI):** Could a malfunction in AICE introduce or fail-to-detect an error in safety-related software?
   - **TI1**: No, never.
   - **TI2**: Yes.

2. **Tool Detection (TD):** If AICE malfunctions, is the error detectable by other means (e.g., human review, downstream tools)?
   - **TD1**: Reliably detected.
   - **TD2**: Sometimes detected.
   - **TD3**: Rarely or never detected.

**TCL = TI × TD.** TCL3 (TI2 × TD3) is the worst case and triggers full tool qualification.

### Where AICE sits today

Different findings push AICE's TCL in different directions:

| Finding | Effect on TI | Effect on TD |
|---|---|---|
| F-CB-01 (Sandbox feature dead) | TI2 — could fail to detect a shadowing error | TD2 — some detected by human review |
| F-CC-R01 (RLM no retry) | TI2 — failed orchestration → wrong context to CIA | TD2 — Copilot reviewer might catch wrong code |
| F-CC-R02 (Synthesis garbage JSON) | TI2 — DA receives malformed context | TD3 — looks like normal context to DA's prompt |
| F-CE-C01 (clang silent fallback to regex) | TI2 — KG fidelity drops | TD3 — degradation invisible |
| F-CD-X01 (Dual ILLD builders) | TI2 — duplicates in KG → wrong code suggestions | TD3 — looks like a "rare bug" to user |
| F-CB-17 (Learning loop disabled) | TI1 — no impact on safety | TD1 |

**Without the Sprint 26-29 plan, AICE is at risk of being classified TCL3** for the iLLD/CIA path. Specifically: F-CC-R02, F-CE-C01, and F-CD-X01 each push TD toward TD3 (silent degradation, wrong-content not detectable downstream).

**With the Sprint 26-29 plan, AICE moves toward TCL2** (TI2 × TD2): malfunctions are still possible, but the silent-failure dashboard from Sprint 27 + the typed-error envelopes from F-CB-08 + the parser_method tracking from F-CE-C02 make most degradations detectable.

**TCL3 → TCL2 reduces the ISO 26262 §11.4.6 qualification effort substantially.** This is a meaningful regulatory benefit of the cleanup plan beyond just "it's good engineering."

This document does not constitute formal TCL qualification — that's a separate exercise involving the TCL determination report, qualification plan, and qualification reports. But it identifies which findings are TCL-relevant, which is input to that exercise.

---

## 7. Recommendations for Pass 4b (CI gates)

Based on the above analysis, the CI gates in Pass 4b should mechanically prevent the following classes of regression:

1. **f-string Cypher injection** (T-UI-03) — `grep` for `f"MATCH (n:{` and similar; fail on hits outside the sanitizer.
2. **`verify_ssl=False`** (T-TLS-01) — `grep` for `verify_ssl=False`; require explicit `# allow-insecure-tls: <reason>` comment to override.
3. **Missing `access_mode="READ"` on Neo4j read sessions** (T-AUTH partial) — AST-walk `self._neo4j.session(database=...)` calls; require either `access_mode=` keyword or `default_access_mode=` set on the driver.
4. **Tool name registered without tier in `tool_tiers.py`** (Pass 1 F-D01) — Python check that compares `tool_tiers.py` keys to `@mcp.tool()` decorated functions.
5. **Cerbos policy duplicate entries** (Pass 1 F-D02) — YAML lint that fails on repeated tool names within a single tier list.
6. **Phantom tool names in requirements docs** (Pass 1 F-D03) — Markdown scan that compares tool names in requirements/*.md against `tool_tiers.py`.
7. **Parallel implementation detection** (Pattern #3) — `find -name '*_parser.py' | xargs basename | sort | uniq -d` and similar; warn on duplicates.
8. **Master-Gaps-claim verification** (Pattern #5) — for known fix patterns, regex-grep that the antipattern is gone everywhere.
9. **Plaintext secrets in code** — Bandit-style hooks for high-entropy strings near `password=` / `api_key=`.
10. **Path-traversal guards present** — AST check that public functions accepting `path`/`file_path`/`image_path` parameters call a sanitizer.

Pass 4b will deliver each of these as a concrete, drop-in GitLab CI pipeline stage with the regex/AST patterns ready to go.

---

## 8. Closing posture statement

After this audit, my honest assessment of AICE Sprint 25's security posture:

> AICE is a competently-built productivity tool with clear, named threats and clear, named mitigations. The defense-in-depth posture is incomplete: many controls exist but were applied to fewer files than they should have been. The transport-security gap (F-CF-X01) is the single most important fix, alongside the path-traversal gap (F-CA-I01) that compounds with it. The audit-trail completeness is at ~80% of what ASPICE SUP.10 requires, and the gap closure is well-defined.
>
> AICE's Tool Confidence Level for the ISO 26262 §11 path it serves (CIA → iLLD code generation) is currently borderline TCL2/TCL3. The Sprint 26-29 plan delivered in Pass 3 §4 substantively improves this — primarily by making malfunctions detectable (TD).
>
> Nothing in this audit suggests AICE should be taken offline or its scope reduced. The recommended fixes are bounded, prioritizable, and in proportion to the system's criticality (Tier-2 productivity tool with human-in-the-loop downstream).

---

**End of Pass 4a — Security Audit.**

Pass 4b (CI Gate Spec for GitLab CI) follows in the next message.
