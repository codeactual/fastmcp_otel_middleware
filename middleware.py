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

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagators.textmap import Getter, TextMapPropagator
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

MetaMapping = Mapping[str, Any]
MiddlewareCallable = Callable[..., Awaitable[Any]]
SpanNameFactory = Callable[[tuple[Any, ...], MutableMapping[str, Any]], str]
AttributesFactory = Callable[[tuple[Any, ...], MutableMapping[str, Any]], Mapping[str, Any]]


class MetaCarrierGetter(Getter[MetaMapping]):
    """Translate MCP meta dictionaries into an OpenTelemetry carrier.

    MCP clients send a `_meta` object that may contain OpenTelemetry headers.
    The Model Context Protocol specification intentionally mirrors HTTP
    propagation, so we expect to find fields like ``traceparent`` and
    ``baggage`` either directly on the meta dictionary or nested under an
    ``otel`` namespace.  This getter normalises the structure for the OTel
    propagator.
    """

    OTEL_NAMESPACE_KEYS = ("otel", "opentelemetry")
    OTEL_FIELD_ALIASES = {
        "traceparent": ("traceparent", "traceParent", "TRACEPARENT"),
        "tracestate": ("tracestate", "traceState", "TRACESTATE"),
        "baggage": ("baggage", "Baggage", "BAGGAGE"),
    }

    def get(self, carrier: MetaMapping | None, key: str) -> Sequence[str]:
        if not carrier:
            return []
        normalized_key = key.lower()
        values: list[str] = []
        for source in self._candidate_sources(carrier):
            if normalized_key in source:
                value = source[normalized_key]
                values.extend(self._coerce_to_strings(value))
        return values

    def keys(self, carrier: MetaMapping | None) -> Iterable[str]:
        if not carrier:
            return []
        keys: set[str] = set()
        for source in self._candidate_sources(carrier):
            keys.update(source.keys())
        return keys

    # -- private helpers -------------------------------------------------

    def _candidate_sources(self, carrier: MetaMapping) -> Iterable[dict[str, Any]]:
        if not isinstance(carrier, Mapping):
            return []
        yield self._normalize_mapping(carrier)
        for namespace_key in self.OTEL_NAMESPACE_KEYS:
            nested = carrier.get(namespace_key)
            if isinstance(nested, Mapping):
                yield self._normalize_mapping(nested)

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


def default_span_name_factory(args: tuple[Any, ...], kwargs: MutableMapping[str, Any]) -> str:
    """Guess a sensible span name based on FastMCP call metadata."""

    if "tool_name" in kwargs and isinstance(kwargs["tool_name"], str):
        return kwargs["tool_name"]
    if "name" in kwargs and isinstance(kwargs["name"], str):
        return kwargs["name"]
    for value in args:
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return name
    return "fastmcp.tool"


def default_attributes_factory(
    args: tuple[Any, ...],
    kwargs: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """Return a stable set of OpenTelemetry attributes for a tool invocation."""

    attributes: dict[str, Any] = {}
    tool_name = kwargs.get("tool_name") or kwargs.get("name")
    if isinstance(tool_name, str):
        attributes["fastmcp.tool.name"] = tool_name
    tool_call_id = kwargs.get("call_id") or kwargs.get("id")
    if isinstance(tool_call_id, str):
        attributes["fastmcp.tool.call_id"] = tool_call_id
    namespace = kwargs.get("namespace") or kwargs.get("tool_namespace")
    if isinstance(namespace, str):
        attributes["fastmcp.tool.namespace"] = namespace
    return attributes


@dataclass
class FastMCPTracingMiddleware:
    """An awaitable FastMCP middleware that emits OpenTelemetry spans.

    The middleware is compatible with the callable interface used by the
    ``fastmcp`` package: it receives the ``call_next`` handler as the first
    argument followed by the standard tool invocation arguments.  The middleware
    extracts OpenTelemetry context from any `_meta` field present in the call
    arguments, starts a server span that wraps the downstream handler, and makes
    the extracted context current for the duration of the invocation.

    Parameters
    ----------
    tracer:
        Optional OpenTelemetry tracer to use.  When omitted, the module's
        default tracer is used.
    span_name_factory:
        Callable that produces the span name.  Defaults to
        :func:`default_span_name_factory`.
    attributes_factory:
        Callable that produces span attributes.  Defaults to
        :func:`default_attributes_factory`.
    propagator:
        Optional custom propagator for context extraction.
    getter:
        Optional custom getter implementation for `_meta` carriers.
    span_kind:
        Kind of the span to emit.  Defaults to :class:`SpanKind.SERVER` which is
        appropriate for server-side handling of tool invocations.
    record_successful_result:
        When ``True`` the middleware will attach a ``fastmcp.tool.success``
        attribute with value ``True`` when the downstream handler returns
        without raising.
    record_tool_exceptions:
        When ``True`` (default) exceptions raised by the downstream handler will
        be recorded on the span and the status will be marked as ERROR before
        re-raising the exception.
    """

    tracer: Tracer | None = None
    span_name_factory: SpanNameFactory = default_span_name_factory
    attributes_factory: AttributesFactory = default_attributes_factory
    propagator: TextMapPropagator | None = None
    getter: MetaCarrierGetter | None = None
    span_kind: SpanKind = SpanKind.SERVER
    record_successful_result: bool = True
    record_tool_exceptions: bool = True

    async def __call__(self, call_next: MiddlewareCallable, *args: Any, **kwargs: Any) -> Any:
        meta = self._extract_meta(args, kwargs)
        parent_context = get_context_from_meta(meta, self.propagator, self.getter)
        token = context.attach(parent_context)
        tracer = self.tracer or trace.get_tracer(__name__)
        span_name = self.span_name_factory(args, kwargs)
        attributes = dict(self.attributes_factory(args, kwargs) or {})

        try:
            with tracer.start_as_current_span(
                span_name, context=parent_context, kind=self.span_kind
            ) as span:
                self._apply_attributes(span, attributes)
                try:
                    result = await call_next(*args, **kwargs)
                    if self.record_successful_result:
                        span.set_attribute("fastmcp.tool.success", True)
                    return result
                except (
                    Exception
                ) as exc:  # pragma: no cover - defensive, fastmcp handles this upstream
                    if self.record_tool_exceptions:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        span.set_attribute("fastmcp.tool.success", False)
                    raise
        finally:
            context.detach(token)

    # -- private helpers -------------------------------------------------

    def _extract_meta(
        self, args: tuple[Any, ...], kwargs: MutableMapping[str, Any]
    ) -> MetaMapping | None:
        if "_meta" in kwargs and isinstance(kwargs["_meta"], Mapping):
            return kwargs["_meta"]
        if "meta" in kwargs and isinstance(kwargs["meta"], Mapping):
            return kwargs["meta"]
        for value in args:
            if (
                isinstance(value, Mapping)
                and "_meta" in value
                and isinstance(value["_meta"], Mapping)
            ):
                return value["_meta"]
            if hasattr(value, "_meta"):
                candidate = value._meta
                if isinstance(candidate, Mapping):
                    return candidate
        return None

    def _apply_attributes(self, span: Span, attributes: Mapping[str, Any]) -> None:
        for key, value in attributes.items():
            span.set_attribute(key, value)


def instrument_fastmcp(
    app: Any,
    *,
    middleware: FastMCPTracingMiddleware | None = None,
    register: Callable[[FastMCPTracingMiddleware], None] | None = None,
    **middleware_kwargs: Any,
) -> FastMCPTracingMiddleware:
    """Attach the tracing middleware to a FastMCP server instance.

    The concrete FastMCP API is evolving quickly.  To avoid hard-coding
    implementation details we accept an optional ``register`` callback that is
    responsible for wiring the middleware into the application.  When the
    callback is omitted the function will attempt a few common registration
    patterns used by FastMCP versions released at the time of writing:

    * An ``app.middleware.add`` callable attribute.
    * An ``app.add_middleware`` method that accepts instantiated middleware.

    Parameters
    ----------
    app:
        FastMCP server instance.
    middleware:
        Optional pre-constructed middleware.  When omitted one will be created
        using ``middleware_kwargs``.
    register:
        Optional callback responsible for adding the middleware to ``app``.
    middleware_kwargs:
        Keyword arguments forwarded to :class:`FastMCPTracingMiddleware` when the
        middleware needs to be constructed by this helper.
    """

    tracing_middleware = middleware or FastMCPTracingMiddleware(**middleware_kwargs)

    if register is not None:
        register(tracing_middleware)
        return tracing_middleware

    add_attr = getattr(app, "middleware", None)
    if callable(add_attr):
        add_attr(tracing_middleware)
        return tracing_middleware
    if hasattr(add_attr, "add") and callable(add_attr.add):
        add_attr.add(tracing_middleware)
        return tracing_middleware

    add_middleware = getattr(app, "add_middleware", None)
    if callable(add_middleware):
        add_middleware(tracing_middleware)
        return tracing_middleware

    raise TypeError(
        "Unable to determine how to register middleware with the provided app. "
        "Pass a `register` callback or upgrade FastMCP to a supported version."
    )
