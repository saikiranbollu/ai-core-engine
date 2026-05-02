# Grafana & Prometheus for Automotive-GraphRAG

Let me explain these tools and where they fit in your architecture.

## What Are They?

### Prometheus
**Time-series metrics database + monitoring system**
- Collects metrics from your applications (CPU, memory, requests, errors, etc.)
- Stores them with timestamps in a time-series database
- Provides query language (PromQL) to analyze metrics over time
- Scrapes endpoints (pulls metrics at regular intervals)

### Grafana
**Visualization & dashboarding platform**
- Reads data from Prometheus (or other sources)
- Creates beautiful dashboards with graphs, charts, alerts
- Shows trends, spikes, anomalies over time
- Can trigger alerts when metrics cross thresholds

### How They Work Together

```
Your Application
    ↓ (exposes metrics)
/metrics endpoint
    ↓ (Prometheus scrapes every 15s)
Prometheus Database
    ↓ (queries)
Grafana Dashboard
    ↓ (displays)
User sees: graphs, trends, alerts
```

## Example Metrics Flow

```
AI Core-Engine Application
├─ query_latency_ms: [245, 312, 198, ...] ← How long to query Neo4j+Qdrant
├─ neo4j_connection_pool: 8/10 active connections
├─ qdrant_search_time_ms: [145, 167, 132, ...]
├─ cache_hit_rate: 0.73 (73% of queries cached)
├─ embedding_time_ms: [234, 289, 201, ...]
├─ vector_search_results: 10 returned
└─ errors_total: 2 errors in last hour

↓ (Prometheus scrapes every 15 seconds)

Prometheus stores:
query_latency_ms{timestamp: 2026-03-10T14:30:00Z, value: 245}
query_latency_ms{timestamp: 2026-03-10T14:30:15Z, value: 312}
query_latency_ms{timestamp: 2026-03-10T14:30:30Z, value: 198}
...

↓ (Grafana queries)

Displays on dashboard:
[Graph showing query latency over the last 24 hours]
Average: 234ms | Min: 98ms | Max: 587ms | P95: 456ms
```

## Where Grafana/Prometheus Fits in Automotive-GraphRAG

### ✅ **Highly Useful For:**

1. **Query Performance Monitoring**
   ```
   Dashboard: AI Core-Engine Query Performance
   ├─ Query latency (ms) [graph]
   ├─ Neo4j search time vs Qdrant search time [comparison]
   ├─ Cache hit rate over time [percentage]
   ├─ Results per query [histogram]
   └─ Slow queries (>1000ms) [alerts]
   ```

2. **System Health Monitoring**
   ```
   Dashboard: Infrastructure Health
   ├─ Neo4j connection pool usage
   ├─ Qdrant memory usage
   ├─ Redis memory usage
   ├─ Celery task queue length (if using)
   ├─ PostgreSQL query latency (if using)
   └─ Disk I/O for /data volume
   ```

3. **API/MCP Server Monitoring**
   ```
   Dashboard: MCP Server Status
   ├─ HTTP requests per minute
   ├─ Error rate (4xx, 5xx)
   ├─ Response time percentiles (P50, P95, P99)
   ├─ Active connections
   └─ Tool invocation frequency
   ```

4. **Test Generation Workflow Metrics** (if using)
   ```
   Dashboard: Test Generation Pipeline
   ├─ Stage 1 timing: Requirements Analysis
   ├─ Stage 2 timing: Architecture Integration
   ├─ Stage 3 timing: Code Generation
   ├─ Stage 4 timing: Validation
   ├─ Stage 5 timing: VP Execution
   ├─ Total end-to-end time
   └─ Success/failure rates per stage
   ```

5. **Knowledge Graph Health**
   ```
   Dashboard: Neo4j Graph Stats
   ├─ Total nodes count (Functions, Registers, Requirements, etc.)
   ├─ Total relationships count
   ├─ Nodes by type (Functions, Registers, Structs, Requirements)
   ├─ Graph traversal time (HAS_MODULE queries)
   └─ Cross-module relationships
   ```

6. **Vector Search Performance**
   ```
   Dashboard: Qdrant Vector Search
   ├─ Search latency (HNSW index performance)
   ├─ Collection sizes (proj_a_cxpi, proj_a_can, etc.)
   ├─ Query count per collection
   ├─ Filter effectiveness (how many results filtered out)
   └─ Index rebuild frequency
   ```

7. **Celery Task Monitoring** (if using)
   ```
   Dashboard: Celery Workers
   ├─ Task queue length
   ├─ Active tasks count
   ├─ Task success/failure rate
   ├─ Average task duration
   ├─ Worker availability
   └─ Retry counts
   ```

8. **LLM API Monitoring**
   ```
   Dashboard: LLM Endpoint Health
   ├─ API response time
   ├─ Token usage (input/output)
   ├─ Error rate from LLM API
   ├─ Rate limit status
   └─ Cost per query (if tracking)
   ```

9. **User/Workspace Metrics** (multi-tenant)
   ```
   Dashboard: Tenant Usage
   ├─ Queries per workspace
   ├─ Tests generated per workspace
   ├─ Storage usage per workspace
   ├─ Active users per workspace
   └─ Cost allocation per workspace
   ```

### ❌ **Not Needed For:**

1. **One-time analysis** (just query manually)
2. **Development/local-only** (adds overhead)
3. **Very small scale** (< 10 daily queries)

## Docker-Compose Setup

```yaml
version: '3'

services:
  neo4j:
    image: neo4j:5.26.0
    ports:
      - "7687:7687"
      - "7474:7474"
  
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
  
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
  
  # NEW: Prometheus (collects metrics)
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
  
  # NEW: Grafana (visualizes metrics)
  grafana:
    image: grafana/grafana:latest
    environment