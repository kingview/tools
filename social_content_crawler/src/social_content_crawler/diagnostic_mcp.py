"""MCP-specific diagnostic boundary; vendored with diagnostics.py in Tool wheels.

Transport metadata is never an authorization source or a tool argument. Business
schemas and successful return values are left untouched.
"""
from __future__ import annotations

import uuid

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from .diagnostics import TRANSPORT_KEY, diagnostic_context, record_exception, safe_exception_message, transport_context


class DiagnosticFastMCP(FastMCP):
    def __init__(self, *args, diagnostic_component: str, accept_diagnostic_meta: bool = True, **kwargs):
        self._diagnostic_component = diagnostic_component
        self._accept_diagnostic_meta = accept_diagnostic_meta
        super().__init__(*args, **kwargs)

    async def call_tool(self, name, arguments):
        incoming = {}
        if self._accept_diagnostic_meta:
            try:
                meta = self.get_context().request_context.meta
            except (ValueError, LookupError):
                meta = None
            if meta is not None:
                payload = meta.model_dump() if hasattr(meta, "model_dump") else meta
                if isinstance(payload, dict):
                    incoming = transport_context(payload.get(TRANSPORT_KEY))
        call_id = incoming.setdefault("tool_call_id", f"call-{uuid.uuid4().hex}")
        incoming.setdefault("trace_id", call_id)
        with diagnostic_context(replace=True, **incoming, tool=name):
            try:
                return await super().call_tool(name, arguments)
            except Exception as exc:
                error_id = record_exception(self._diagnostic_component, f"mcp.{name}", exc)
                # Only an opaque error ID crosses the transport; stack and input
                # values stay out of protocol metadata and the GUI.
                return CallToolResult(isError=True,
                    content=[TextContent(type="text", text=safe_exception_message(exc))],
                    **({"_meta": {TRANSPORT_KEY: {"error_id": error_id}}} if error_id else {}))
