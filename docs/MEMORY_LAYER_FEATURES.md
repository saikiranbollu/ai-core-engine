# Memory Layer — Feature Status

> **Last updated:** March 24, 2026  
> **Architecture:** Two-tier memory (temporary working memory + persistent semantic memory),
> ontology-driven, MCP-integrated.
>
> Legend: **Working** = tested and functional | **Placeholder** = code exists but not wired / missing config | **Not Started** = no code yet

---

## 1. Working Memory (Temporary — 1hr TTL)

| Feature | Status | Details |
|---------|--------|---------|
| **Session lifecycle** | Working | Create, get, close, extend TTL, auto-purge expired sessions |
| **Ontology-validated creation** | Working | Module and node_type validated against `ontology.yaml` at creation time |
| **Scoped sessions** | Working | Each session is scoped to project + module + profile |
| **Context storage** | Working | Store typed context entries (`ContextEntry`) with node_type, source, relevance_score, query_text |
| **Filtered retrieval** | Working | Get context filtered by node_type, source, and minimum relevance score |
| **Generic key-value store** | Working | `store_data()` / `retrieve_data()` for arbitrary metadata |
| **Session summary** | Working | `get_session_summary()` returns counts, age, remaining TTL |
| **List active sessions** | Working | Filterable by project and module |
| **Bulk purge** | Working | `purge_expired_sessions()` cleans up all expired entries |
| **InMemoryBackend** | Working | Thread-safe (`threading.Lock`), used in dev/test and as the current MCP server backend |
| **RedisBackend** | Placeholder | Code is complete (real Redis calls: `setex`, `get`, `delete`, `keys`), but **no Redis instance is provisioned** and no env-var fallback (`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`) is wired into the constructor — must pass host/port/password manually |
| **Expired session auto-removal** | Working | `get_session()` deletes expired sessions from the backend and returns `None` |

> **Note:** The MCP server currently instantiates `InMemoryBackend` only. Sessions are lost on server restart.

---

## 2. Semantic Memory (Persistent — Qdrant)set PATH=%PATH%;C:\Users\AyubKhan\bin

| Feature | Status | Details |
|---------|--------|---------|
| **Pattern schema** | Working | `ApprovedPattern` dataclass with 11 fields (text, type, module, profile, confidence, approver, usage_count, etc.) |
| **Qdrant-backed CRUD** | Working | `store()`, `get()`, `query_by_module()`, `query_by_min_usage()` — all payloads stored in Qdrant |
| **Vector embeddings** | Working | `Embedder` class using `all-MiniLM-L6-v2` (384-dim, pinned HF revision `c9745ed...`), single + batch embedding |
| **Similarity search** | Working | `find_similar()` — cosine similarity with configurable threshold (default 0.8) and top-K |
| **Usage tracking** | Working | `increment_usage()` atomically bumps `usage_count` in Qdrant payload |
| **Module-filtered search** | Working | All queries support filtering by module |
| **Backward-compat layer** | Working | `PatternIndex` wraps `PatternStore` for existing callers |
| **Mandatory credentials** | Working | `QDRANT_URL` and `QDRANT_API_KEY` required via env vars — raises `ValueError` if missing |
| **Pinned model revision** | Working | Model name + revision loaded from `storage_config.yaml`; no fallback defaults, fails fast if config missing |

> **Note:** `PatternStore` is fully functional but is **not yet called by any production code path** (MCP tools, RAG pipeline, or domain assistants). It is only exercised from unit tests. Approved patterns need to be populated and wired into query enrichment.

---

## 3. Ontology Subsystem

| Feature | Status | Details |
|---------|--------|---------|
| **Ontology-driven config** | Working | Everything (node types, modules, relationships, extraction patterns, Qdrant data types) comes from `ontology.yaml` |
| **Multi-profile support** | Working | `illd` and `mcal` profiles with separate node types and modules |
| **Singleton loader** | Working | `get_ontology()` — reads YAML once, caches for process lifetime |
| **Rich introspection** | Working | 30+ methods for querying node types, properties, relationships, validation rules, query templates |

---

## 4. MCP Session Tools (5 tools exposed)

| Feature | Status | Details |
|---------|--------|---------|
| **`session_start`** | Working | Creates session via `WorkingMemoryManager` (or fallback dict). Ontology-validated, TTL-enforced |
| **`session_store`** | Working | Stores key-value data. Calls `_wm_manager.store_data()` on the real backend |
| **`session_retrieve`** | Working | Retrieves data by key. Calls `_wm_manager.retrieve_data()` |
| **`build_context`** | Working | Assembles token-budget-aware context from RAG results + conversation history, persists `_last_context` in session |
| **`session_end`** | Working | Closes session, returns audit stats (keys stored, context entries, timestamps) |

> **Note:** These 5 tools **work as standalone key-value operations** — an LLM agent can call them explicitly. However, they are **not automatically invoked** by `search_databases` or any other query tool. There is no auto-enrichment of queries from session context today.

---

## 5. Domain Session Adapter

| Feature | Status | Details |
|---------|--------|---------|
| **MCP-routed operations** | Placeholder | Code is complete — calls `session_start`, `session_end`, `session_store`, `session_retrieve` via a bridge object. **Not imported or used by any production code** (only tests) |
| **RAG result ingestion** | Placeholder | `store_rag_results()` batch-stores retrieval results with type mapping. Works in tests, not wired into the RAG pipeline |
| **KG result ingestion** | Placeholder | `store_kg_results()` batch-stores knowledge graph results with type mapping. Works in tests, not wired into the RAG pipeline |
| **Graceful degradation** | Working | Falls back to local tracking if MCP server is unavailable |
| **Assistant-scoped** | Working | Each adapter is named (e.g. `"GEST"`, `"REVA"`) for logging and routing |

> **Note:** `DomainSessionAdapter` was designed as the bridge between domain assistants and the memory layer. The class is fully coded and tested, but **no domain assistant imports it yet**.

---

## 7. Yet to Be Implemented

| Item | Category | Description |
|------|----------|-------------|
| **Redis provisioning** | Infrastructure | `RedisBackend` code is ready but no Redis instance exists. Need to provision Redis (Kubernetes or managed), then wire `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` env vars into the MCP server bootstrap so it creates `RedisBackend` instead of `InMemoryBackend` |
| **Redis env-var support in constructor** | Code | `RedisBackend.__init__()` requires explicit `host`/`port`/`password` params — add env-var fallback (`os.environ.get("REDIS_HOST", "localhost")` etc.) |
| **Query enrichment from session** | Code | `search_databases` (and other query tools) should accept an optional `session_id` param. If provided, retrieve stored context/prior results from the session and use them to enrich or rerank the current query |
| **Auto-write results to session** | Code | After `search_databases` returns results, automatically store top-K results + assembled context into the active session so future queries in the same session benefit from accumulated context |
| **Domain assistant integration** | Code | Wire `DomainSessionAdapter` into domain assistants (GEST, REVA, ACRA, CIA, SAGA, etc.) so they create/use/close sessions during their workflows |
| **Prompt workflow session lifecycle** | Code | Update all 8 prompt templates to include `session_start` as step 0 and `session_end` as the final step, with `session_store`/`session_retrieve` between query steps |
| **Pattern store production wiring** | Code | `PatternStore` (Qdrant-backed approved patterns) needs to be called from the query pipeline — e.g. before `search_databases`, retrieve similar approved patterns and inject them as additional context or reranking signals |
| **Pattern population pipeline** | Code | No mechanism exists to populate approved patterns into Qdrant. Need an admin tool or ingestion step that creates `ApprovedPattern` entries from curated data |
| **Session persistence across restarts** | Infrastructure | With `InMemoryBackend`, all sessions are lost on MCP server restart. Requires either Redis or a persistent backend to survive pod restarts |
| **Qdrant collection for patterns** | Infrastructure | `PatternStore` needs its own Qdrant collection (separate from RAG collections). Collection creation and env-var config (`QDRANT_URL`, `QDRANT_API_KEY`) must be set up in the deployment environment |
| **Cross-session learning** | Code | No mechanism to promote frequently useful session data into long-term semantic memory (patterns). A feedback loop from session usage → `PatternStore` would close this gap |
