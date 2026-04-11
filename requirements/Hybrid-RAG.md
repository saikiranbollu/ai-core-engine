# Requirements

## 3.1 Core Components

### 3.1.1 Query Router

| Req ID | Requirement | Priority | Rationale |
| --- | --- | ---: | --- |
| AICE-HRAG-001 | The Query Router shall classify incoming queries into three types: STRUCTURAL, SEMANTIC, HYBRID. | Must | Routing decision |
| AICE-HRAG-002 | The Query Router shall classify queries containing structural keywords as STRUCTURAL. Structural keywords: trace, relationship, linked, connected, depends, implements, tests, calls, parent, child, references, derived. | Must | Keyword routing |
| AICE-HRAG-003 | The Query Router shall classify queries containing semantic keywords as SEMANTIC. Semantic keywords: similar, like, about, related, example, pattern, find code like, matching, resembling. | Must | Keyword routing |
| AICE-HRAG-004 | The Query Router shall classify queries containing both structural and semantic keywords as HYBRID. | Must | Combined retrieval |
| AICE-HRAG-005 | The Query Router shall default to SEMANTIC classification when no keywords are detected. | Must | Fallback behavior |
| AICE-HRAG-006 | The Query Router shall route STRUCTURAL queries to Graph-RAG only. | Must | Efficient routing |
| AICE-HRAG-007 | The Query Router shall route SEMANTIC queries to Vector-RAG only. | Must | Efficient routing |
| AICE-HRAG-008 | The Query Router shall route HYBRID queries to both Graph-RAG and Vector-RAG, then invoke Result Merger. | Must | Comprehensive retrieval |
| AICE-HRAG-009 | The Query Router shall support explicit query type override via request parameter (force_type: STRUCTURAL, SEMANTIC, HYBRID). | Must | Override support |
| AICE-HRAG-010 | The Query Router shall log classification decisions including: query_hash, detected_keywords, classification, timestamp. | Must | Observability |

### 3.1.2 Graph-RAG (Neo4j)

| Req ID | Requirement | Priority | Rationale |
| --- | --- | ---: | --- |
| AICE-HRAG-020 | Graph-RAG shall execute Cypher queries against Neo4j knowledge graph. | Must | Core retrieval |
| AICE-HRAG-021 | Graph-RAG shall support multi-hop traversal queries up to 5 hops depth. | Must | Traceability chains |
| AICE-HRAG-022 | Graph-RAG shall query the following node types: Requirement, Architecture, Module, Component, Design, Code, Function, Variable, TestSpec, TestCase, TestCode, TestResult, HWSpec, Register, Bitfield, SafetyReq, SecurityReq. | Must | Domain coverage |
| AICE-HRAG-023 | Graph-RAG shall query the following relationship types: TRACES_TO, IMPLEMENTS, CALLS, USES, TESTS, COVERS, PART_OF, DEPENDS_ON, DERIVED_FROM, CONFIGURES, AFFECTS, VERIFIES. | Must | Relationship coverage |
| AICE-HRAG-024 | Graph-RAG shall support full-text search using Neo4j full-text indexes on: title, description, content, name fields. | Should | Natural language |
| AICE-HRAG-025 | Graph-RAG shall return maximum 50 nodes per query unless explicitly overridden (max: 200). | Must | Performance protection |
| AICE-HRAG-026 | Graph-RAG shall include node metadata in results: id, source_file, version, ingested_at, node_type. | Must | Provenance |
| AICE-HRAG-027 | Graph-RAG shall use parameterized queries to prevent Cypher injection. | Must | Security |
| AICE-HRAG-028 | Graph-RAG shall support filtering by: project_id, module_name, node_type, date_range. | Must | Scoped retrieval |
| AICE-HRAG-029 | Graph-RAG shall assign graph_rank scores based on: path_length (shorter=higher), relationship_relevance, node_importance. | Should | Ranking |

### 3.1.3 Vector-RAG (Qdrant)

| Req ID | Requirement | Priority | Rationale |
| --- | --- | ---: | --- |
| AICE-HRAG-040 | Vector-RAG shall store and query 1536-dimensional embedding vectors. | Must | OpenAI Ada compatibility |
| AICE-HRAG-041 | Vector-RAG shall use cosine similarity for vector comparisons. | Must | Standard metric |
| AICE-HRAG-042 | Vector-RAG shall apply default similarity threshold of 0.7 for result filtering. | Must | Quality filtering |
| AICE-HRAG-043 | Vector-RAG shall support configurable similarity thresholds per query (range: 0.5 - 0.95). | Should | Flexibility |
| AICE-HRAG-044 | Vector-RAG shall return top-K results with K configurable (default: 10, max: 100). | Must | Result limiting |
| AICE-HRAG-045 | Vector-RAG shall support metadata filtering: node_type, project_id, module, file_path. | Must | Scoped retrieval |
| AICE-HRAG-046 | Vector-RAG shall maintain separate collections for iLLD and MCAL projects. | Must | Data isolation |
| AICE-HRAG-047 | Vector-RAG shall generate embeddings using OpenAI text-embedding-ada-002 model. | Must | Embedding model |
| AICE-HRAG-048 | Vector-RAG shall batch embedding generation (max 100 texts per API request). | Should | API efficiency |
| AICE-HRAG-049 | Vector-RAG shall cache generated embeddings to avoid re-computation. | Should | Cost optimization |
| AICE-HRAG-050 | Vector-RAG shall include similarity_score in all returned results. | Must | Ranking info |

### 3.1.4 Result Merger

| Req ID | Requirement | Priority | Rationale |
| --- | --- | ---: | --- |
| AICE-HRAG-060 | Result Merger shall combine results from Graph-RAG and Vector-RAG for HYBRID queries. | Must | Hybrid retrieval |
| AICE-HRAG-061 | Result Merger shall deduplicate results based on node_id. | Must | Avoid redundancy |
| AICE-HRAG-062 | Result Merger shall calculate unified relevance score using formula: score = α × graph_rank + (1-α) × vector_similarity where α is configurable (default: 0.5). | Must | Balanced ranking |
| AICE-HRAG-063 | Result Merger shall re-rank merged results by relevance score descending. | Must | Quality ordering |
| AICE-HRAG-064 | Result Merger shall apply diversity filtering: max 3 results from same source_file, max 5 results per node_type. | Should | Result diversity |
| AICE-HRAG-065 | Result Merger shall preserve source attribution (from_graph, from_vector, or both). | Must | Transparency |
| AICE-HRAG-066 | Result Merger shall limit final result set to max 30 items (configurable). | Must | Output bounds |
