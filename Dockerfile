# ==============================================================
#  AI Core Engine MCP Server + Cerbos PDP — Multi-stage build
# ==============================================================
#  Stage 1: Copy Cerbos binary from official image
#  Stage 2: Python app with MCP server + auth policies
# ==============================================================

# ── Stage 1: Cerbos binary ──
FROM ghcr.io/cerbos/cerbos:latest AS cerbos

# ── Stage 2: Python application ──
FROM python:3.12-slim

LABEL maintainer="AI Core Engine Team"
LABEL description="AI Core Engine MCP Server with Cerbos authorization"

WORKDIR /app

# System dependencies (build-essential for C extensions, curl for healthcheck,
# git for pip VCS installs, libclang-dev for tree-sitter/docling)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential git curl libclang-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy Cerbos binary from stage 1
COPY --from=cerbos /cerbos /usr/local/bin/cerbos

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers embedding model into the image
# so the first search_database call doesn't block on a network download.
ENV HF_HOME=/app/.cache \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy application code (renamed to avoid shadowing the pip 'mcp' package)
COPY mcp/ ./aice_mcp/
COPY src/ ./src/

# Copy Cerbos policies into the container's policy directory
# (matches storage.disk.directory in .cerbos.yaml)
COPY mcp/auth/policies/ /policies/

# Redirect HuggingFace / sentence-transformers cache to a writable path
# (default /.cache is not writable in many pod configurations)
# Created after COPY steps and with open permissions so any runtime UID can write
RUN mkdir -p /app/.cache/sentence_transformers && chmod -R 777 /app/.cache

# Make all src packages importable
ENV PYTHONPATH="/app/src:/app/src/HybridRAG/code:/app/src/MemoryLayer:/app/aice_mcp"

# Environment defaults (HF_HOME and SENTENCE_TRANSFORMERS_HOME set earlier for model pre-download)
ENV CERBOS_BIN=/usr/local/bin/cerbos \
    CERBOS_CONFIG=/app/aice_mcp/auth/.cerbos.yaml \
    CERBOS_HOST=localhost \
    CERBOS_HTTP_PORT=3592 \
    CERBOS_GRPC_PORT=3593 \
    MCP_TRANSPORT=streamable-http \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=8000 \
    FASTMCP_STREAMABLE_HTTP_PATH=/mcp \
    API_KEY_REGISTRY_PATH=/app/aice_mcp/auth/api_keys.yaml \
    REDIS_URL=redis://:password@redis:6379/0 \
    PYTHONUNBUFFERED=1

# Expose MCP + Cerbos ports
EXPOSE 8000 3592 3593

# Entrypoint: app.py starts both Cerbos PDP and MCP server
CMD ["python", "aice_mcp/app.py"]
