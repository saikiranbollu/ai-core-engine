"""
Centralized Configuration — MEG_SW-287
=======================================
Single source of truth for all MCP server settings.
Loads from environment variables with validated defaults via pydantic-settings.

Usage:
    from mcp.core.config import get_settings
    settings = get_settings()
    timeout = settings.tool_timeout_seconds
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All MCP server configuration in one validated place."""

    # Logging
    log_level: str = "INFO"

    # Auth
    mcp_api_key: str = ""
    api_key_registry_path: str = "auth/api_keys.yaml"
    cerbos_host: str = "localhost"
    cerbos_http_port: int = 3592

    # Neo4j — fallback only; workspace_id param takes priority
    mcp_neo4j_instance: str = "mcal"

    # Tools
    max_dependencies_depth: int = 3
    tool_timeout_seconds: int = 300
    strict_signature_validation: bool = False

    # Sessions
    session_ttl_seconds: int = 3600

    # Rate Limiting
    rate_limit_search: str = "60/minute"
    rate_limit_admin: str = "10/minute"
    rate_limit_ingestion: str = "5/minute"

    # Error handling
    sanitize_errors: bool = True

    model_config = {"env_prefix": "", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings singleton (created once per process)."""
    return Settings()
