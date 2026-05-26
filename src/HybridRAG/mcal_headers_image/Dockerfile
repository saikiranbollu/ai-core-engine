# Minimal image carrying pre-filtered MCAL C headers for sandbox clang parsing.
# Built once per MCAL release. Tagged by release identifier.
#
# Usage:
#   python prepare_headers.py --clean
#   docker build -f src/HybridRAG/mcal_headers_image/Dockerfile -t registry.intra.infineon.com/aice/mcal-headers:2026-Q2 .
#   docker push registry.intra.infineon.com/aice/mcal-headers:2026-Q2
#
# The MCP pod consumes this via an init-container that copies /headers/ into
# a shared emptyDir volume mounted at /app/include_headers.
FROM busybox:musl
COPY tests/include_headers/ /headers/
