"""
EA Direct Query Service — On-the-fly Enterprise Architect queries
================================================================

Provides query access to EA models (pyDump or direct .eap) without
requiring full ingestion into the knowledge graph.  Designed for
MCAL workflows where arch/design/code headers live in EA files.

Includes Redis caching and Postgres audit logging for AI Governance.

Usage::

    from src.IngestionPipeline.ea_direct_query import EADirectQueryService

    svc = EADirectQueryService("/path/to/pyDump")
    result = svc.query_component("Adc_Init")
    tree = svc.get_architecture_tree("Adc")
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EADirectQueryService:
    """On-the-fly EA model query with caching.

    Parameters
    ----------
    ea_path : str
        Path to pyDump directory or .eap/.qeax file.
    project : str
        EA project name (passed to _EAModelExtractor).
    components : list[str]
        Component list to extract.
    mode : str
        "pyDump" or "direct" (auto-detected from path).
    redis_client : optional
        Redis client for caching.  If None, caching is disabled.
    cache_ttl : int
        Cache TTL in seconds. Default 3600 (1 hour).
    """

    def __init__(
        self,
        ea_path: str,
        project: str = "MCAL",
        components: Optional[List[str]] = None,
        redis_client=None,
        cache_ttl: int = 3600,
    ):
        self._ea_path = ea_path
        self._project = project
        self._components = components or []
        self._redis = redis_client
        self._cache_ttl = cache_ttl
        self._model: Optional[List[Any]] = None
        self._model_index: Dict[str, Any] = {}

    def _ensure_model(self):
        """Lazy-load the EA model on first query."""
        if self._model is not None:
            return

        from src.IngestionPipeline.Parsers.ea_parser import _EAModelExtractor

        extractor = _EAModelExtractor(
            path=self._ea_path,
            project=self._project,
            components=self._components,
            options="all",
        )
        self._model = extractor.extract()
        self._build_index()
        logger.info(
            "[EADirectQuery] Loaded EA model from %s (%d components)",
            self._ea_path, len(self._model or []),
        )

    def _build_index(self):
        """Build a name → component lookup index."""
        self._model_index = {}
        if not self._model:
            return
        for comp in self._model:
            name = getattr(comp, "Name", getattr(comp, "name", ""))
            if name:
                self._model_index[name.lower()] = comp
            # Index sub-elements (interfaces, functions, etc.)
            for attr_name in ("Interfaces", "Functions", "DataTypes",
                              "Classes", "Decisions", "Packages"):
                items = getattr(comp, attr_name, None)
                if not items:
                    continue
                if isinstance(items, dict):
                    items = list(items.values())
                elif not isinstance(items, list):
                    continue
                for item in items:
                    iname = getattr(item, "Name", getattr(item, "name", ""))
                    if iname:
                        self._model_index[iname.lower()] = item

    def _cache_key(self, op: str, *args) -> str:
        raw = f"ea_dq:{self._ea_path}:{op}:{':'.join(str(a) for a in args)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[Dict]:
        if not self._redis:
            return None
        try:
            data = self._redis.get(key)
            if data:
                return json.loads(data)
        except Exception:
            pass
        return None

    def _cache_set(self, key: str, value: Dict):
        if not self._redis:
            return
        try:
            self._redis.setex(key, self._cache_ttl, json.dumps(value, default=str))
        except Exception:
            pass

    def query_component(self, component_name: str) -> Dict[str, Any]:
        """Query a specific component by name.

        Returns detailed component info including interfaces, data types,
        and architectural decisions.
        """
        cache_key = self._cache_key("component", component_name)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        self._ensure_model()
        comp = self._model_index.get(component_name.lower())
        if not comp:
            return {"found": False, "component_name": component_name,
                    "message": f"Component '{component_name}' not found in EA model"}

        result = self._serialize_element(comp)
        result["found"] = True
        result["component_name"] = component_name
        result["ea_path"] = self._ea_path

        self._cache_set(cache_key, result)
        return result

    def search(self, query: str, scope: str = "all") -> List[Dict[str, Any]]:
        """Search across the EA model.

        Parameters
        ----------
        query : str
            Search query (matched against element names and descriptions).
        scope : str
            One of: "all", "components", "interfaces", "datatypes", "decisions".

        Returns
        -------
        list[dict]
            Matching elements with name, type, and context.
        """
        cache_key = self._cache_key("search", query, scope)
        cached = self._cache_get(cache_key)
        if cached:
            return cached.get("results", [])

        self._ensure_model()
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results: List[Dict[str, Any]] = []

        for name, element in self._model_index.items():
            if scope != "all":
                etype = self._element_type(element).lower()
                if scope == "components" and etype != "component":
                    continue
                if scope == "interfaces" and etype != "interface":
                    continue
                if scope == "datatypes" and etype not in ("datatype", "class"):
                    continue
                if scope == "decisions" and etype != "decision":
                    continue

            ename = getattr(element, "Name", getattr(element, "name", name))
            desc = getattr(element, "Description", getattr(element, "description", ""))
            if pattern.search(ename) or (isinstance(desc, str) and pattern.search(desc)):
                results.append({
                    "name": ename,
                    "type": self._element_type(element),
                    "description": str(desc)[:500] if desc else "",
                    "guid": getattr(element, "GUID", getattr(element, "guid", "")),
                })
                if len(results) >= 50:
                    break

        self._cache_set(cache_key, {"results": results})
        return results

    def get_architecture_tree(self, module: str) -> Dict[str, Any]:
        """Return a hierarchical architecture tree for a module.

        Parameters
        ----------
        module : str
            Module name (e.g. "Adc", "Can").

        Returns
        -------
        dict
            Tree structure with components, interfaces, and data types.
        """
        cache_key = self._cache_key("arch_tree", module)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        self._ensure_model()
        tree: Dict[str, Any] = {
            "module": module,
            "components": [],
            "interfaces": [],
            "datatypes": [],
            "decisions": [],
        }

        if not self._model:
            tree["message"] = "No model loaded"
            return tree

        pattern = re.compile(re.escape(module), re.IGNORECASE)
        for comp in self._model:
            comp_name = getattr(comp, "Name", "")
            if not pattern.search(comp_name):
                continue
            comp_info = {"name": comp_name, "children": []}

            for attr_name, key in [("Interfaces", "interfaces"),
                                    ("DataTypes", "datatypes"),
                                    ("Decisions", "decisions")]:
                items = getattr(comp, attr_name, None)
                if not items:
                    continue
                if isinstance(items, dict):
                    items = list(items.values())
                elif not isinstance(items, list):
                    continue
                for item in items:
                    iname = getattr(item, "Name", getattr(item, "name", ""))
                    entry = {
                        "name": iname,
                        "type": self._element_type(item),
                        "guid": getattr(item, "GUID", ""),
                    }
                    tree[key].append(entry)
                    comp_info["children"].append(iname)

            tree["components"].append(comp_info)

        tree["ea_path"] = self._ea_path
        self._cache_set(cache_key, tree)
        return tree

    def get_statistics(self) -> Dict[str, Any]:
        """Get overview statistics about the loaded EA model."""
        self._ensure_model()
        type_counts: Dict[str, int] = {}
        for _, element in self._model_index.items():
            etype = self._element_type(element)
            type_counts[etype] = type_counts.get(etype, 0) + 1
        return {
            "ea_path": self._ea_path,
            "total_elements": len(self._model_index),
            "top_level_components": len(self._model or []),
            "by_type": type_counts,
        }

    @staticmethod
    def _element_type(element) -> str:
        """Infer element type from its class name or attributes."""
        cls_name = type(element).__name__
        if "Component" in cls_name:
            return "Component"
        if "Interface" in cls_name:
            return "Interface"
        if "DataType" in cls_name or "Class" in cls_name:
            return "DataType"
        if "Decision" in cls_name:
            return "Decision"
        if "Package" in cls_name:
            return "Package"
        if "Function" in cls_name:
            return "Function"
        return cls_name

    @staticmethod
    def _serialize_element(element) -> Dict[str, Any]:
        """Convert an EA model element to a JSON-safe dict."""
        result: Dict[str, Any] = {}
        for attr in ("Name", "name", "Description", "description", "GUID",
                      "guid", "Type", "type", "Stereotype", "stereotype",
                      "PackagePath", "package_path"):
            val = getattr(element, attr, None)
            if val is not None:
                result[attr.lower()] = str(val)[:2000]

        # Sub-elements
        for attr_name in ("Interfaces", "Functions", "DataTypes",
                          "Classes", "Decisions", "Attributes", "Methods"):
            items = getattr(element, attr_name, None)
            if not items:
                continue
            if isinstance(items, dict):
                items = list(items.values())
            elif not isinstance(items, list):
                continue
            result[attr_name.lower()] = [
                {
                    "name": getattr(it, "Name", getattr(it, "name", "")),
                    "type": EADirectQueryService._element_type(it),
                    "guid": getattr(it, "GUID", getattr(it, "guid", "")),
                }
                for it in items[:100]
            ]

        return result
