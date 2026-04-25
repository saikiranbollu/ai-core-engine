"""
Swagger UI & OpenAPI spec generation for MCP tools.
=====================================================

Generates an OpenAPI 3.0 catalogue from the registered MCP tool functions
and serves a Swagger UI page at ``/`` and ``/docs``.

This module is independent of the MCP server itself and is wired in by
``mcp_server._build_asgi_app``.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .tool_tiers import TOOL_TIERS

# ── Swagger UI HTML (CSS/JS loaded from CDN) ─────────────────────────────

SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AI Core Engine — MCP Tool Catalogue</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css">
  <style>body{margin:0;background:#fafafa}</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis],
      deepLinking: true,
      supportedSubmitMethods: [],
    });
  </script>
</body>
</html>"""

# Python → JSON Schema type mapping
_TYPE_MAP: Dict[type, str] = {
    str: "string", int: "integer", float: "number",
    bool: "boolean", list: "array", dict: "object",
}


def generate_openapi_spec(
    tool_functions: Dict[str, Callable],
) -> Dict[str, Any]:
    """Build an OpenAPI 3.0 spec by introspecting *tool_functions*.

    Parameters
    ----------
    tool_functions:
        Mapping of ``tool_name → callable`` for every registered MCP tool.
    """
    paths: Dict[str, Any] = {}
    schemas: Dict[str, Any] = {}

    tier_priority = {"public": 0, "developer": 1, "admin": 2}
    ordered_tools = sorted(
        TOOL_TIERS.items(),
        key=lambda item: (tier_priority.get(item[1], 99), item[0]),
    )

    for tool_name, tier in ordered_tools:
        func = tool_functions.get(tool_name)
        if func is None or not callable(func):
            continue

        sig = inspect.signature(func)
        docstring = (inspect.getdoc(func) or "").strip()
        parts = docstring.split("\n\n", 1)
        summary = parts[0].replace("\n", " ").strip() if parts else tool_name
        description = parts[1].strip() if len(parts) > 1 else ""

        # Build request-body schema from function parameters
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation
            origin = getattr(ann, "__origin__", None)

            # Resolve Optional[X] → X
            if origin is type(None):
                continue
            if hasattr(ann, "__args__") and type(None) in getattr(ann, "__args__", ()):
                inner_args = [a for a in ann.__args__ if a is not type(None)]
                ann = inner_args[0] if inner_args else str
                origin = getattr(ann, "__origin__", None)

            if origin is list or origin is List:
                prop: Dict[str, Any] = {"type": "array", "items": {"type": "string"}}
            elif origin is dict or origin is Dict:
                prop = {"type": "object"}
            else:
                prop = {"type": _TYPE_MAP.get(ann, "string")}

            if param.default is not inspect.Parameter.empty and param.default is not None:
                prop["default"] = param.default
            else:
                if param.default is inspect.Parameter.empty:
                    required.append(pname)

            properties[pname] = prop

        schema_name = f"{tool_name}_Request"
        schemas[schema_name] = {"type": "object", "properties": properties}
        if required:
            schemas[schema_name]["required"] = required

        tag = tier.capitalize()
        paths[f"/tool/{tool_name}"] = {
            "post": {
                "tags": [tag],
                "summary": summary,
                "description": description,
                "operationId": tool_name,
                "requestBody": {
                    "required": bool(required),
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Successful tool invocation",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error": {"type": "boolean"},
                                        "data": {"type": "object"},
                                    },
                                },
                            }
                        },
                    },
                    "403": {"description": "Permission denied (RBAC)"},
                },
                "security": [{"BearerAuth": []}],
            }
        }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AI Core Engine — MCP Tool Catalogue",
            "version": "1.0.0",
            "description": (
                "Documentation catalogue for the AI Core Engine MCP Server.\n\n"
                "**These are MCP tools, not REST endpoints.** They are invoked "
                "via the MCP protocol at **`/mcp`** (streamable-http), not via "
                "direct HTTP POST.\n\n"
                "To call a tool, connect an MCP client (e.g. VS Code Copilot) to "
                "`https://aice-mcswai.eu-de-7.icp.infineon.com/mcp` with a `Bearer` token in the "
                "`Authorization` header.\n\n"
                "**Access tiers:** public, developer, admin"
            ),
        },
        "servers": [],
        "tags": [
            {"name": "Public"},
            {"name": "Developer"},
            {"name": "Admin"},
        ],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "API key passed as Bearer token",
                },
            },
        },
    }


def swagger_routes(
    tool_functions: Dict[str, Callable],
) -> List[Route]:
    """Return Starlette ``Route`` objects for Swagger UI and OpenAPI spec.

    Parameters
    ----------
    tool_functions:
        Mapping of ``tool_name → callable`` for every registered MCP tool.
    """
    _cache: Dict[str, Any] = {}

    async def _openapi_handler(request):
        if "spec" not in _cache:
            _cache["spec"] = generate_openapi_spec(tool_functions)
        return JSONResponse(_cache["spec"])

    async def _swagger_ui_handler(request):
        return HTMLResponse(SWAGGER_HTML)

    return [
        Route("/", _swagger_ui_handler),
        Route("/docs", _swagger_ui_handler),
        Route("/openapi.json", _openapi_handler),
    ]
