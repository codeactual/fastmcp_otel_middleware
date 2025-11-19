"""FastMCP middleware helpers for OpenTelemetry integration.

The utilities in this module are intentionally lightweight and avoid depending on
implementation details of the `fastmcp` package so that they can be consumed
from both FastMCP itself and related test doubles.  The middleware focuses on
three responsibilities:

* Extract an OpenTelemetry context from the `_meta` field that MCP clients send
  according to the Model Context Protocol specification.
* Start a server span that represents the lifecycle of a tool invocation and
  decorate it with useful attributes about the tool call.
* Propagate the extracted context into the currently running task so that
  downstream OpenTelemetry instrumentation (database clients, HTTP requests,
  etc.) all share the same trace.

The implementation is inspired by the examples from the FastMCP project and the
Langfuse MCP tracing example referenced in the module README.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagators.textmap import Getter, TextMapPropagator
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

MetaMapping = Mapping[str, Any]


class ToolCallMessage(Protocol):
    """Protocol for FastMCP tool call messages."""

    name: str  # Tool name
    arguments: dict[str, Any] | None  # Tool arguments


class RequestContext(Protocol):
    """Protocol for FastMCP request context."""

    @property
    def meta(self) -> dict[str, Any] | None:
        """Optional metadata sent by the client."""
        ...


class FastMCPContext(Protocol):
    """Protocol for FastMCP Context object."""

    @property
    def request_context(self) -> RequestContext | None:
        """Access to the underlying request context."""
        ...


class MiddlewareContext(Protocol):
    """Protocol for FastMCP middleware context objects."""

    message: ToolCallMessage  # The MCP message being processed
    method: str | None  # The MCP method (e.g., "tools/call")
    source: str  # Source of the request ("client" or "server")
    fastmcp_context: FastMCPContext | None  # FastMCP context (contains request_context)


CallNext = Callable[[MiddlewareContext], Awaitable[Any]]


class MetaCarrierGetter(Getter[MetaMapping]):
    """Translate MCP meta dataclass objects into an OpenTelemetry carrier.

    MCP clients send a `_meta` dataclass object that may contain OpenTelemetry headers.
    FastMCP exposes this via ctx.fastmcp_context.request_context.meta as a dataclass.

    The Model Context Protocol specification intentionally mirrors HTTP
    propagation, so we expect to find the ``traceparent`` field either
    directly as a dataclass attribute or nested under an ``otel`` namespace.
    This getter extracts the dataclass attributes and normalises them for the OTel propagator.
    """

    OTEL_NAMESPACE_KEYS = ("otel", "opentelemetry")
    OTEL_FIELD_ALIASES = {
        "traceparent": ("traceparent", "traceParent", "TRACEPARENT"),
    }

    def get(self, carrier: MetaMapping | None, key: str) -> list[str]:
        if not carrier:
            return []
        normalized_key = key.lower()
        values: list[str] = []
        for source in self._candidate_sources(carrier):
            if normalized_key in source:
                value = source[normalized_key]
                values.extend(self._coerce_to_strings(value))
        return values

    def keys(self, carrier: MetaMapping | None) -> list[str]:
        if not carrier:
            return []
        keys: set[str] = set()
        for source in self._candidate_sources(carrier):
            keys.update(source.keys())
        return list(keys)

    # -- private helpers -------------------------------------------------

    def _candidate_sources(self, carrier: MetaMapping) -> Iterable[dict[str, Any]]:
        # FastMCP's _meta is a dataclass object, not a dict
        # Extract attributes using vars() to get the underlying __dict__
        if not hasattr(carrier, "__dict__"):
            return

        # Convert object attributes to a dict
        carrier_dict = vars(carrier)
        yield self._normalize_mapping(carrier_dict)

        # Also check for nested otel/opentelemetry namespaces
        for namespace_key in self.OTEL_NAMESPACE_KEYS:
            nested = carrier_dict.get(namespace_key)
            if nested and hasattr(nested, "__dict__"):
                yield self._normalize_mapping(vars(nested))

    def _normalize_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for canonical_key, aliases in self.OTEL_FIELD_ALIASES.items():
            for alias in aliases:
                if alias in mapping:
                    normalized[canonical_key] = mapping[alias]
                    break
        for key, value in mapping.items():
            lower_key = key.lower()
            if lower_key in normalized:
                continue
            normalized[lower_key] = value
        return normalized

    @staticmethod
    def _coerce_to_strings(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set, frozenset)):
            return [str(item) for item in value if item is not None]
        return [str(value)]


def get_context_from_meta(
    meta: MetaMapping | None,
    propagator: TextMapPropagator | None = None,
    getter: MetaCarrierGetter | None = None,
) -> Context:
    """Extract an OpenTelemetry context from an MCP `_meta` carrier."""

    if meta is None:
        return context.get_current()

    propagator = propagator or get_global_textmap()
    getter = getter or MetaCarrierGetter()
    return propagator.extract(carrier=meta, getter=getter)


def _debug_log_tool_call(
    tool_name: str,
    meta: MetaMapping | None,
    span_name: str,
    mcp_method: str | None,
    mcp_source: str,
    extracted_context: Context,
    meta_source: str | None = None,
) -> None:
    """Log debug information about tool calls when FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG=1.

    This function logs to stderr with the following information:
    - Timestamp (ISO 8601 format with timezone)
    - Tool name
    - Span name (how the tool name is transformed for tracing)
    - MCP method and source
    - Meta source (where the _meta was extracted from)
    - All OTEL_FIELD_ALIASES key/value pairs propagated from _meta
    - Extracted trace/span IDs from the context
    - Raw _meta keys present in the request

    Parameters
    ----------
    tool_name:
        Name of the tool being invoked.
    meta:
        The _meta dictionary from the MCP message.
    span_name:
        The generated span name.
    mcp_method:
        The MCP protocol method (e.g., "tools/call").
    mcp_source:
        Source of the request ("client" or "server").
    extracted_context:
        The OpenTelemetry context extracted from _meta.
    meta_source:
        Where the _meta was extracted from (e.g., "ctx.request_context.meta").
    """
    if os.environ.get("FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG") != "1":
        return

    timestamp = datetime.now(timezone.utc).isoformat()

    # Start building the debug output
    lines = [
        "=" * 80,
        f"[FASTMCP OTEL DEBUG] {timestamp}",
        f"Tool Name: {tool_name}",
        f"Span Name: {span_name}",
        f"MCP Method: {mcp_method or 'N/A'}",
        f"MCP Source: {mcp_source}",
        f"Meta Source: {meta_source or 'not found'}",
        "",
        "OTEL_FIELD_ALIASES propagated from _meta:",
    ]

    # Extract and log OTEL_FIELD_ALIASES values
    getter = MetaCarrierGetter()
    otel_fields_found = False

    for canonical_key, aliases in getter.OTEL_FIELD_ALIASES.items():
        values = getter.get(meta, canonical_key)
        if values:
            otel_fields_found = True
            # Show which alias was actually used and its value
            for alias in aliases:
                if meta and hasattr(meta, "__dict__"):
                    # Check root level dataclass attributes
                    if hasattr(meta, alias):
                        lines.append(f"  {canonical_key} (as '{alias}'): {getattr(meta, alias)}")
                        break
                    # Check nested otel/opentelemetry namespaces
                    for ns_key in getter.OTEL_NAMESPACE_KEYS:
                        if hasattr(meta, ns_key):
                            nested = getattr(meta, ns_key)
                            if hasattr(nested, "__dict__") and hasattr(nested, alias):
                                lines.append(
                                    f"  {canonical_key} (as '{ns_key}.{alias}'): {getattr(nested, alias)}"
                                )
                                break

    if not otel_fields_found:
        lines.append("  (none found)")

    # Extract trace/span info from context
    lines.append("")
    lines.append("Extracted OpenTelemetry Context:")
    try:
        # Get span from the extracted context
        span = trace.get_current_span(extracted_context)
        span_context = span.get_span_context()
        if span_context.is_valid:
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")
            lines.append(f"  Trace ID: {trace_id}")
            lines.append(f"  Span ID: {span_id}")
            lines.append(f"  Trace Flags: {span_context.trace_flags}")
        else:
            lines.append("  (no valid span context)")
    except Exception as e:
        # Context was extracted but span info unavailable (common in test stubs)
        lines.append(
            f"  Context extracted successfully (span details unavailable: {type(e).__name__})"
        )

    # Log raw _meta information
    lines.append("")
    lines.append("Raw _meta information:")
    if meta is None:
        lines.append("  _meta is None")
    else:
        lines.append(f"  _meta type: {type(meta).__name__}")
        lines.append(f"  _meta repr: {repr(meta)}")
        if hasattr(meta, "__dict__"):
            attrs = vars(meta)
            if attrs:
                lines.append("  _meta attributes:")
                for key in sorted(attrs.keys()):
                    lines.append(f"    - {key}: {attrs[key]}")
            else:
                lines.append("  _meta has no attributes")
        else:
            lines.append("  _meta is not a dataclass/object (primitive type)")

    lines.append("=" * 80)
    lines.append("")  # Empty line for readability

    # Write to stderr
    print("\n".join(lines), file=sys.stderr, flush=True)


@dataclass
class FastMCPTracingMiddleware:
    """FastMCP hook-based middleware that emits OpenTelemetry spans for tool calls.

    This middleware uses FastMCP's hook-based middleware system (introduced in v2.9)
    to provide reliable access to tool names and other MCP protocol information.
    It extracts OpenTelemetry context from the `_meta` field via
    ctx.fastmcp_context.request_context.meta, starts a server span for each tool
    invocation, and propagates the context through the call chain.

    Compatible with FastMCP 2.9+. Does not depend on the fastmcp package directly,
    using duck typing to remain lightweight and flexible.

    Parameters
    ----------
    tracer:
        Optional OpenTelemetry tracer to use. When omitted, the module's
        default tracer is used.
    span_name_prefix:
        Optional prefix for span names. Defaults to empty string.
        Example: "tool." will create spans like "tool.get_temperature"
    propagator:
        Optional custom propagator for context extraction.
    getter:
        Optional custom getter implementation for `_meta` carriers.
    span_kind:
        Kind of the span to emit. Defaults to SpanKind.SERVER which is
        appropriate for server-side handling of tool invocations.
    record_successful_result:
        When True, attach a "fastmcp.tool.success" attribute with value True
        when the tool handler returns without raising.
    record_tool_exceptions:
        When True (default), exceptions raised by tool handlers will be recorded
        on the span and the status will be marked as ERROR before re-raising.
    include_arguments:
        When True, include stringified tool arguments in span attributes as
        "fastmcp.tool.arguments". Default is False to avoid leaking sensitive data.
    langfuse_compatible:
        When True, also set attributes with "langfuse.observation.metadata."
        prefix to make them queryable in Langfuse UI. Disabled by default to keep
        the attribute set minimal; enable when exporting to Langfuse.
    """

    tracer: Tracer | None = None
    span_name_prefix: str = ""
    propagator: TextMapPropagator | None = None
    getter: MetaCarrierGetter | None = None
    span_kind: SpanKind = SpanKind.SERVER
    record_successful_result: bool = True
    record_tool_exceptions: bool = True
    include_arguments: bool = False
    langfuse_compatible: bool = False

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> Any:
        """Main entry point for FastMCP middleware.

        This method makes the middleware callable and dispatches to the appropriate
        hook method based on the MCP method being invoked. FastMCP's middleware
        system uses functools.partial to build the middleware chain, which requires
        middleware to be callable.

        For 'tools/call' methods, this dispatches to on_call_tool. For all other
        MCP methods (initialize, list_tools, etc.), it passes through without
        creating spans.

        Parameters
        ----------
        ctx:
            FastMCP middleware context containing the MCP message and metadata.
        call_next:
            Callable to invoke the next middleware or the final handler.

        Returns
        -------
        Any
            The result returned by the handler.
        """
        # Dispatch to on_call_tool for tool invocations
        if ctx.method == "tools/call":
            return await self.on_call_tool(ctx, call_next)

        # For all other methods (initialize, list_tools, etc.), pass through
        return await call_next(ctx)

    async def on_call_tool(self, ctx: MiddlewareContext, call_next: CallNext) -> Any:
        """Handle tool call requests with OpenTelemetry tracing.

        This method is called by FastMCP for each tool invocation. It:
        1. Extracts the tool name from context.message.name
        2. Extracts OpenTelemetry context from context.fastmcp_context.request_context.meta
        3. Creates a server span for the tool invocation
        4. Calls the next handler in the middleware chain
        5. Records success/failure and returns the result

        Parameters
        ----------
        ctx:
            FastMCP middleware context containing the MCP message and metadata.
        call_next:
            Callable to invoke the next middleware or the final tool handler.

        Returns
        -------
        Any
            The result returned by the tool handler.
        """
        # Extract tool name from the MCP message
        tool_name = ctx.message.name

        # Extract OpenTelemetry context from _meta field via fastmcp_context
        meta = None
        meta_source = "ctx.fastmcp_context.request_context.meta"
        if ctx.fastmcp_context is not None:
            request_ctx = ctx.fastmcp_context.request_context
            if request_ctx is not None:
                meta = request_ctx.meta

        parent_context = get_context_from_meta(meta, self.propagator, self.getter)

        # Early debug logging to see what _meta contains
        if os.environ.get("FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG") == "1":
            print(
                f"[FASTMCP OTEL DEBUG] Extracting _meta:\n"
                f"  meta source: {meta_source}\n"
                f"  _meta value: {repr(meta)}\n"
                f"  _meta type: {type(meta).__name__ if meta is not None else 'None'}\n"
                f"  parent_contexte: {parent_context}",
                file=sys.stderr,
                flush=True,
            )

        # Attach the extracted context to the current task
        token = context.attach(parent_context)

        # Get tracer and create span name
        tracer = self.tracer or trace.get_tracer(__name__)
        span_name = f"{self.span_name_prefix}{tool_name}"

        # Debug logging if enabled
        _debug_log_tool_call(
            tool_name=tool_name,
            meta=meta,
            span_name=span_name,
            mcp_method=ctx.method,
            mcp_source=ctx.source,
            extracted_context=parent_context,
            meta_source=meta_source,
        )

        try:
            with tracer.start_as_current_span(
                span_name, context=parent_context, kind=self.span_kind
            ) as span:
                # Add span attributes
                self._set_attribute(span, "fastmcp.tool.name", tool_name, langfuse_name="tool_name")

                if ctx.method:
                    self._set_attribute(span, "mcp.method", ctx.method, langfuse_name="mcp_method")

                self._set_attribute(span, "mcp.source", ctx.source, langfuse_name="mcp_source")

                if self.include_arguments and ctx.message.arguments:
                    args_str = str(ctx.message.arguments)
                    self._set_attribute(
                        span, "fastmcp.tool.arguments", args_str, langfuse_name="tool_arguments"
                    )

                try:
                    # Call the next middleware or tool handler
                    result = await call_next(ctx)

                    if self.record_successful_result:
                        self._set_attribute(
                            span, "fastmcp.tool.success", True, langfuse_name="tool_success"
                        )

                    return result

                except Exception as exc:
                    if self.record_tool_exceptions:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        self._set_attribute(
                            span, "fastmcp.tool.success", False, langfuse_name="tool_success"
                        )
                    raise
        finally:
            context.detach(token)

    def _set_attribute(
        self, span: Any, name: str, value: Any, langfuse_name: str | None = None
    ) -> None:
        """Set a span attribute, optionally with Langfuse-compatible prefix.

        Parameters
        ----------
        span:
            The OpenTelemetry span to set the attribute on.
        name:
            Standard attribute name (e.g., "fastmcp.tool.name").
        value:
            The attribute value.
        langfuse_name:
            Optional simplified name for Langfuse metadata (e.g., "tool_name").
            If provided and langfuse_compatible is True, also sets the attribute
            with "langfuse.observation.metadata." prefix for Langfuse queryability.
        """
        # Always set the standard attribute for compatibility with other OTel tools
        span.set_attribute(name, value)

        # Also set Langfuse-prefixed attribute if configured
        if self.langfuse_compatible and langfuse_name:
            span.set_attribute("langfuse.observation.type", "tool")
            span.set_attribute(f"langfuse.observation.metadata.{langfuse_name}", value)


def instrument_fastmcp(
    app: Any,
    *,
    middleware: FastMCPTracingMiddleware | None = None,
    **middleware_kwargs: Any,
) -> FastMCPTracingMiddleware:
    """Attach the tracing middleware to a FastMCP server instance.

    This function registers the hook-based middleware with FastMCP using the
    standard ``app.add_middleware()`` method. The middleware will automatically
    trace all tool invocations with OpenTelemetry spans.

    Compatible with FastMCP 2.13.1+ (requires request_context.meta API).

    Parameters
    ----------
    app:
        FastMCP server instance.
    middleware:
        Optional pre-constructed middleware. When omitted, one will be created
        using ``middleware_kwargs``.
    middleware_kwargs:
        Keyword arguments forwarded to :class:`FastMCPTracingMiddleware` when the
        middleware needs to be constructed by this helper.

    Returns
    -------
    FastMCPTracingMiddleware
        The middleware instance that was registered.

    Raises
    ------
    TypeError
        If the app doesn't have an ``add_middleware`` method.

    Examples
    --------
    Basic usage::

        from fastmcp import FastMCP
        from fastmcp_otel_middleware import instrument_fastmcp

        app = FastMCP("MyServer")
        instrument_fastmcp(app)

    With custom configuration::

        instrument_fastmcp(
            app,
            span_name_prefix="tool.",
            include_arguments=True
        )
    """
    tracing_middleware = middleware or FastMCPTracingMiddleware(**middleware_kwargs)

    # Use the standard FastMCP 2.9+ middleware registration method
    add_middleware = getattr(app, "add_middleware", None)
    if callable(add_middleware):
        add_middleware(tracing_middleware)
        return tracing_middleware

    raise TypeError(
        f"The provided app does not have an 'add_middleware' method. "
        f"This middleware requires FastMCP 2.13.1 or later. "
        f"Got app type: {type(app)}"
    )
