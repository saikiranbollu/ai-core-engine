"""
AI Core Engine — Kubernetes Entrypoint
=======================================

Starts both the Cerbos PDP and the MCP server in the same pod.

Usage:
    python app.py

Environment variables:
    CERBOS_BIN          Path to cerbos binary (default: "cerbos")
    CERBOS_CONFIG       Path to .cerbos.yaml   (default: auth/.cerbos.yaml)
    CERBOS_HOST         PDP host for health check (default: localhost)
    CERBOS_HTTP_PORT    PDP HTTP port          (default: 3592)
    CERBOS_GRPC_PORT    PDP gRPC port          (default: 3593)
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("aice_app")

_APP_DIR = Path(__file__).resolve().parent  # mcp/


def _wait_for_cerbos(host: str, port: int, timeout: int = 30) -> bool:
    """Poll the Cerbos HTTP health endpoint until ready."""
    import http.client

    url = f"/_cerbos/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", url)
            resp = conn.getresponse()
            if resp.status == 200:
                logger.info("Cerbos PDP healthy at %s:%s", host, port)
                return True
            conn.close()
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> None:
    cerbos_bin = os.environ.get("CERBOS_BIN", "cerbos")
    cerbos_config = os.environ.get(
        "CERBOS_CONFIG",
        str(_APP_DIR / "auth" / ".cerbos.yaml"),
    )
    cerbos_host = os.environ.get("CERBOS_HOST", "localhost")
    cerbos_http_port = int(os.environ.get("CERBOS_HTTP_PORT", "3592"))

    # ── 1. Start Cerbos PDP ──
    logger.info("Starting Cerbos PDP: %s server --config=%s", cerbos_bin, cerbos_config)
    cerbos_proc = subprocess.Popen(
        [cerbos_bin, "server", f"--config={cerbos_config}"],
        stdout=sys.stderr,  # merge cerbos output into stderr
        stderr=sys.stderr,
    )

    # ── 2. Wait for Cerbos to be healthy ──
    if not _wait_for_cerbos(cerbos_host, cerbos_http_port, timeout=30):
        logger.error("Cerbos PDP did not become healthy within 30 s — aborting")
        cerbos_proc.terminate()
        sys.exit(1)

    # ── 3. Graceful shutdown handler ──
    def _shutdown(signum, frame):
        logger.info("Received signal %s — shutting down", signum)
        if cerbos_proc.poll() is None:
            cerbos_proc.terminate()
            try:
                cerbos_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cerbos_proc.kill()
        sys.exit(0)

    def _on_sigchld(signum, frame):
        # Cerbos child may have exited — check without blocking
        ret = cerbos_proc.poll()
        if ret is not None:
            logger.error("Cerbos child process exited unexpectedly with code %s — shutting down", ret)
            sys.exit(1)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGCHLD, _on_sigchld)

    # ── 4. Start APScheduler for periodic background jobs ──
    scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler(daemon=True)

        def _health_check_job():
            """Periodic health check for backend services."""
            try:
                from core.mcp_server import _get_neo4j, _get_qdrant, _get_redis
                checks = {}
                try:
                    neo4j = _get_neo4j()
                    checks["neo4j"] = "up" if neo4j else "down"
                except Exception:
                    checks["neo4j"] = "down"
                try:
                    qdrant = _get_qdrant()
                    checks["qdrant"] = "up" if qdrant else "down"
                except Exception:
                    checks["qdrant"] = "down"
                try:
                    r = _get_redis()
                    checks["redis"] = "up" if r and r.ping() else "down"
                except Exception:
                    checks["redis"] = "down"
                logger.info("[Scheduler] Health check: %s", checks)
            except Exception as e:
                logger.warning("[Scheduler] Health check failed: %s", e)

        def _cache_stats_job():
            """Log cache hit rates periodically."""
            try:
                from src.Configuration.cache_service import CacheService
                svc = CacheService()
                logger.info("[Scheduler] Cache stats: %s", svc.stats())
            except Exception as e:
                logger.debug("[Scheduler] Cache stats failed: %s", e)

        scheduler.add_job(_health_check_job, IntervalTrigger(minutes=5),
                          id="health_check", replace_existing=True)
        scheduler.add_job(_cache_stats_job, IntervalTrigger(minutes=30),
                          id="cache_stats", replace_existing=True)
        scheduler.start()
        logger.info("[Scheduler] APScheduler started with 2 periodic jobs")

        # Update shutdown handler to also stop scheduler
        _orig_shutdown = _shutdown
        def _shutdown_with_scheduler(signum, frame):
            if scheduler and scheduler.running:
                scheduler.shutdown(wait=False)
                logger.info("[Scheduler] APScheduler stopped")
            _orig_shutdown(signum, frame)
        signal.signal(signal.SIGTERM, _shutdown_with_scheduler)
        signal.signal(signal.SIGINT, _shutdown_with_scheduler)

    except ImportError:
        logger.info("[Scheduler] apscheduler not installed — periodic jobs disabled")
    except Exception as e:
        logger.warning("[Scheduler] Failed to start: %s", e)

    # ── 5. Start MCP server ──
    logger.info("Starting MCP server")
    try:
        from core.mcp_server import main as mcp_main
        mcp_main()
    except Exception:
        logger.exception("MCP server failed")
    finally:
        if cerbos_proc.poll() is None:
            cerbos_proc.terminate()
            try:
                cerbos_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cerbos_proc.kill()


if __name__ == "__main__":
    main()
