"""
Domain Session Adapter
======================
Generic MCP-routed session management for any Domain Assistant.

Routes **all** session lifecycle operations through the MCP tool layer.
No direct import of ``WorkingMemoryManager`` — session management is
delegated to the MCP session tools which internally use the real
Memory Layer ``WorkingMemoryManager`` (with TTL, ontology validation, etc.).

Architecture (strictly MCP-routed)::

    DomainApp (e.g. GEST)
      └─ DomainSessionAdapter.create_session()
           └─ MCPBridge.session_start()
                └─ GestMCPClient.session_start()   (async JSON-RPC)
                     └─ MCP Server subprocess
                          └─ session_start tool
                               └─ WorkingMemoryManager.create_session()

Lives in Memory Layer because session management is a Memory Layer concern,
not a Domain Assistant concern.  Domain Assistants only import this adapter
and the MCPBridge.

Usage from any Domain Assistant
-------------------------------
>>> from src.MemoryLayer.memory.domain_session_adapter import DomainSessionAdapter
>>> adapter = DomainSessionAdapter(bridge=mcp_bridge, assistant_name="GEST")
>>> sid = adapter.create_session(module="cxpi", metadata={"description": "..."})
>>> adapter.store_rag_results(sid, rag_functions, "function")
>>> adapter.store_kg_results(sid, kg_deps, "dependency")
>>> summary = adapter.close_session(sid)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass  # MCPBridge is injected; no hard dependency

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Node-type mapping tables — shared across all domain assistants
# ─────────────────────────────────────────────────────────────────────────────

RAG_TYPE_MAP: Dict[str, str] = {
    "function":        "APIFunction",
    "struct":          "DataStructure",
    "enum":            "EnumValue",
    "requirement":     "SoftwareRequirement",
    "register":        "Register",
    "hardware":        "Register",
    "macro":           "DataStructure",
    "typedef":         "Typedef",
    "source":          "Document",
    "pattern_library": "PatternLibrary",
}

KG_TYPE_MAP: Dict[str, str] = {
    "dependency":    "APIFunction",
    "function":      "APIFunction",
    "struct_member":  "StructMember",
    "enum_value":    "EnumValue",
    "parameter":     "Parameter",
    "requirement":   "SoftwareRequirement",
    "register":      "Register",
    "bitfield":      "BitField",
    "switch_case":   "EnumValue",
}


class DomainSessionAdapter:
    """MCP-routed session management for Domain Assistants.

    All session state lives in the MCP server subprocess (backed by
    ``WorkingMemoryManager``).  This class provides convenience methods
    (``store_rag_results``, ``store_kg_results``, etc.) and delegates
    storage to MCP ``session_store`` / ``session_retrieve`` calls.

    A lightweight ``_local_sessions`` dict tracks session IDs in-process
    so that ``list_active_sessions`` works without a round-trip.

    Parameters
    ----------
    bridge : MCPBridge
        Shared sync bridge to the MCP server subprocess.
    assistant_name : str
        Name of the calling domain assistant (e.g. "GEST", "REVA").
    default_ttl : int
        Default session TTL in seconds (default 3600).
    """

    def __init__(self, bridge: Any, assistant_name: str = "DOMAIN", default_ttl: int = 3600):
        self._bridge = bridge
        self._assistant_name = assistant_name
        self.default_ttl = default_ttl
        self._local_sessions: Dict[str, Dict[str, Any]] = {}
        logger.info("[SESSION] MCP-routed %s session adapter initialized", assistant_name)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        module: str = "cxpi",
        project: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> str:
        """Create a new session via MCP ``session_start``. Returns session_id."""
        ttl = ttl_seconds or self.default_ttl
        meta = metadata or {}
        meta.setdefault("assistant", self._assistant_name)
        meta.setdefault("created_ts", time.time())

        sid = f"{self._assistant_name.lower()}_{uuid.uuid4().hex[:12]}"
        try:
            self._bridge.session_start(
                session_id=sid,
                assistant_name=self._assistant_name,
                module_context=module.lower(),
                ttl_seconds=ttl,
            )
            self._bridge.session_store(sid, "metadata", meta)
            self._bridge.session_store(sid, "module", module.lower())
            self._bridge.session_store(sid, "project", project.lower())
        except Exception as e:
            logger.warning("[SESSION] MCP session_start failed: %s — using local tracking only", e)

        self._local_sessions[sid] = {
            "module": module.lower(),
            "project": project.lower(),
            "metadata": meta,
            "context_count": 0,
        }
        logger.info("[SESSION] Created session %s (module=%s)", sid, module)
        return sid

    def close_session(self, session_id: str) -> Dict[str, Any]:
        """Close session via MCP ``session_end`` and return summary."""
        summary: Dict[str, Any] = {}
        try:
            summary = self._bridge.session_end(session_id) or {}
        except Exception as e:
            logger.warning("[SESSION] MCP session_end failed: %s", e)

        local = self._local_sessions.pop(session_id, {})
        summary.setdefault("session_id", session_id)
        summary.setdefault("module", local.get("module", ""))
        summary.setdefault("context_count", local.get("context_count", 0))
        logger.info("[SESSION] Closed session %s", session_id)
        return summary

    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        local = self._local_sessions.get(session_id)
        if not local:
            return None
        return {
            "session_id": session_id,
            "module": local.get("module", ""),
            "context_count": local.get("context_count", 0),
        }

    # ------------------------------------------------------------------
    # Context storage — convenience methods for pipeline stages
    # ------------------------------------------------------------------

    def store_rag_results(
        self,
        session_id: str,
        results: List[Dict[str, Any]],
        result_type: str,
        query_text: str = "",
    ) -> int:
        """Store a batch of RAG results in the session via MCP ``session_store``."""
        if not results:
            return 0

        entries = []
        for item in results:
            node_type = self._resolve_node_type(item, result_type, RAG_TYPE_MAP, "DataNode")
            name = (
                item.get("metadata", {}).get("function")
                or item.get("metadata", {}).get("name")
                or item.get("name", "")
                or f"rag_{result_type}_{len(entries)}"
            )
            score = item.get("similarity", 0.8)
            entries.append({
                "node_type": node_type,
                "node_id": name,
                "source": "qdrant",
                "score": float(score),
                "query_text": query_text,
            })

        key = f"rag_{result_type}"
        try:
            self._bridge.session_store(session_id, key, entries)
        except Exception as e:
            logger.debug("[SESSION] session_store failed for %s: %s", key, e)

        local = self._local_sessions.get(session_id)
        if local:
            local["context_count"] = local.get("context_count", 0) + len(entries)

        return len(entries)

    def store_kg_results(
        self,
        session_id: str,
        results: Any,
        result_type: str,
        query_text: str = "",
    ) -> int:
        """Store KG query results in the session via MCP ``session_store``."""
        if not results:
            return 0

        items: List[Dict] = results if isinstance(results, list) else [results] if isinstance(results, dict) else []
        if not items:
            return 0

        entries = []
        for item in items:
            node_type = self._resolve_node_type(item, result_type, KG_TYPE_MAP, "DataNode")
            name = (
                item.get("function_name")
                or item.get("name")
                or item.get("node_id", f"kg_{result_type}_{len(entries)}")
            )
            entries.append({
                "node_type": node_type,
                "node_id": str(name),
                "source": "neo4j",
                "query_text": query_text,
            })

        key = f"kg_{result_type}"
        try:
            self._bridge.session_store(session_id, key, entries)
        except Exception as e:
            logger.debug("[SESSION] session_store failed for %s: %s", key, e)

        local = self._local_sessions.get(session_id)
        if local:
            local["context_count"] = local.get("context_count", 0) + len(entries)

        return len(entries)

    def store_key_value(
        self,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Store arbitrary key-value data in the session via MCP ``session_store``."""
        try:
            self._bridge.session_store(session_id, key, value)
        except Exception as e:
            logger.debug("[SESSION] session_store kv failed for %s: %s", key, e)

    def get_context(
        self,
        session_id: str,
        node_type: Optional[str] = None,
    ) -> List[Any]:
        """Retrieve context entries from the session via MCP ``session_retrieve``."""
        all_entries: List[Any] = []
        for suffix in ("function", "struct", "enum", "requirement", "register",
                        "hardware", "dependency", "parameter"):
            try:
                data = self._bridge.session_retrieve(session_id, f"rag_{suffix}")
                if isinstance(data, list):
                    all_entries.extend(data)
            except Exception:
                continue
            try:
                data = self._bridge.session_retrieve(session_id, f"kg_{suffix}")
                if isinstance(data, list):
                    all_entries.extend(data)
            except Exception:
                continue

        if node_type:
            all_entries = [e for e in all_entries if isinstance(e, dict) and e.get("node_type") == node_type]
        return all_entries

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_real_memory_layer(self) -> bool:
        """Always True — MCP session tools are backed by WorkingMemoryManager."""
        return True

    def list_active_sessions(self) -> List[Dict[str, Any]]:
        return [
            {"session_id": sid, "module": s.get("module", "")}
            for sid, s in self._local_sessions.items()
        ]

    @staticmethod
    def _resolve_node_type(
        item: Dict[str, Any],
        result_type: str,
        type_map: Dict[str, str],
        fallback: str,
    ) -> str:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if isinstance(metadata, dict) and metadata.get("node_type"):
            return str(metadata["node_type"])
        if isinstance(item, dict) and item.get("node_type"):
            return str(item["node_type"])
        return type_map.get(result_type, fallback)
