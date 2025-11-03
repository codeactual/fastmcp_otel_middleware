"""Utilities for integrating FastMCP servers with OpenTelemetry.

This package exposes helpers for configuring OpenTelemetry tracing for FastMCP
applications that rely on `_meta` field propagation to forward client-provided
context across MCP boundaries.
"""

from .middleware import (
    FastMCPTracingMiddleware,
    MetaCarrierGetter,
    get_context_from_meta,
    instrument_fastmcp,
)

__all__ = [
    "FastMCPTracingMiddleware",
    "MetaCarrierGetter",
    "get_context_from_meta",
    "instrument_fastmcp",
]
