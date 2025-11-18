import asyncio
import os
from io import StringIO
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.context import get_current
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    StatusCode,
    TraceFlags,
    TraceState,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from fastmcp_otel_middleware.middleware import (
    FastMCPTracingMiddleware,
    MetaCarrierGetter,
    get_context_from_meta,
    instrument_fastmcp,
)


@pytest.fixture()
def tracer_provider():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


@pytest.fixture()
def parent_context():
    span_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    ctx = trace.set_span_in_context(NonRecordingSpan(span_context))
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier, context=ctx)
    return span_context, {"otel": carrier}


# Mock objects for testing the hook-based middleware pattern
class MockToolCallMessage:
    """Mock FastMCP tool call message."""

    def __init__(self, name: str, arguments: dict | None = None, meta: dict | None = None):
        self.name = name
        self.arguments = arguments
        self._meta = meta


class MockRequestContext:
    """Mock FastMCP request context."""

    def __init__(self, meta: dict | None = None):
        self.meta = meta


class MockContext:
    """Mock FastMCP Context."""

    def __init__(self, request_context: MockRequestContext | None = None):
        self.request_context = request_context


class MockMiddlewareContext:
    """Mock FastMCP middleware context."""

    def __init__(
        self,
        message: MockToolCallMessage,
        method: str = "tools/call",
        source: str = "client",
        fastmcp_context: MockContext | None = None,
    ):
        self.message = message
        self.method = method
        self.source = source
        # If no fastmcp_context provided, create one from message._meta for backward compatibility
        if fastmcp_context is None:
            request_ctx = MockRequestContext(meta=message._meta)
            self.fastmcp_context = MockContext(request_context=request_ctx)
        else:
            self.fastmcp_context = fastmcp_context


def test_meta_carrier_getter_reads_nested_fields(parent_context):
    _, meta = parent_context
    meta["otel"]["traceParent"] = meta["otel"].pop("traceparent")
    getter = MetaCarrierGetter()

    values = getter.get(meta, "traceparent")

    assert values
    assert getter.keys(meta)


def test_get_context_from_meta_returns_current_when_meta_missing():
    current = get_current()

    extracted = get_context_from_meta(None)

    assert extracted == current


def test_middleware_creates_span_with_parent(tracer_provider, parent_context):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    parent_span_context, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer, langfuse_compatible=True)

    # Create mock context with the tool call message
    message = MockToolCallMessage(name="my-tool", arguments={"arg1": "value1"}, meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        # The extracted context should be active while the handler runs
        assert trace.get_current_span().get_span_context().is_valid
        return "result"

    result = asyncio.run(middleware.on_call_tool(ctx, call_next))

    assert result == "result"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.name == "my-tool"
    assert span.parent is not None
    assert span.parent.span_id == parent_span_context.span_id

    # Check standard OpenTelemetry attributes
    assert span.attributes["fastmcp.tool.name"] == "my-tool"
    assert span.attributes["mcp.method"] == "tools/call"
    assert span.attributes["mcp.source"] == "client"
    assert span.attributes["fastmcp.tool.success"] is True

    # Check Langfuse-compatible attributes (prefixed for queryability)
    assert span.attributes["langfuse.observation.metadata.tool_name"] == "my-tool"
    assert span.attributes["langfuse.observation.metadata.mcp_method"] == "tools/call"
    assert span.attributes["langfuse.observation.metadata.mcp_source"] == "client"
    assert span.attributes["langfuse.observation.metadata.tool_success"] is True

    assert span.kind.name == "SERVER"


def test_middleware_omits_langfuse_attributes_by_default(tracer_provider, parent_context):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    _, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    message = MockToolCallMessage(name="default-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        return "result"

    asyncio.run(middleware.on_call_tool(ctx, call_next))

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.attributes["fastmcp.tool.name"] == "default-tool"
    assert not any(key.startswith("langfuse.") for key in span.attributes)


def test_middleware_records_exceptions(tracer_provider, parent_context):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    _, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context
    message = MockToolCallMessage(name="error-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(middleware.on_call_tool(ctx, call_next))

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["fastmcp.tool.success"] is False


def test_middleware_with_custom_configuration(tracer_provider):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(
        tracer=tracer,
        span_name_prefix="tool.",
        record_successful_result=False,
        include_arguments=True,
    )

    # Create mock context
    message = MockToolCallMessage(name="test_tool", arguments={"key": "value"})
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        return "ok"

    asyncio.run(middleware.on_call_tool(ctx, call_next))

    span = exporter.get_finished_spans()[0]
    assert span.name == "tool.test_tool"
    assert span.attributes["fastmcp.tool.arguments"] == "{'key': 'value'}"
    assert "fastmcp.tool.success" not in span.attributes


def test_instrument_fastmcp_registers_middleware():
    class App:
        def __init__(self):
            self.middleware_list = []

        def add_middleware(self, middleware):
            self.middleware_list.append(middleware)

    app = App()

    middleware = instrument_fastmcp(app, record_successful_result=False, span_name_prefix="mcp.")

    assert middleware in app.middleware_list
    assert middleware.record_successful_result is False
    assert middleware.span_name_prefix == "mcp."


def test_instrument_fastmcp_raises_on_incompatible_app():
    class IncompatibleApp:
        pass

    app = IncompatibleApp()

    with pytest.raises(TypeError, match="does not have an 'add_middleware' method"):
        instrument_fastmcp(app)


def test_middleware_call_dispatches_to_on_call_tool(tracer_provider, parent_context):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    _, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context for tool call
    message = MockToolCallMessage(name="test-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message, method="tools/call")

    async def call_next(context):
        return "tool-result"

    # Call the middleware using __call__
    result = asyncio.run(middleware(ctx, call_next))

    assert result == "tool-result"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.name == "test-tool"
    assert span.attributes["fastmcp.tool.name"] == "test-tool"


def test_middleware_call_passes_through_for_non_tool_methods(tracer_provider):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context for initialize method
    message = MockToolCallMessage(name="", meta=None)
    ctx = MockMiddlewareContext(message=message, method="initialize")

    async def call_next(context):
        return {"protocolVersion": "2025-06-18", "capabilities": {}}

    # Call the middleware using __call__
    result = asyncio.run(middleware(ctx, call_next))

    # Should return the result without creating spans
    assert result["protocolVersion"] == "2025-06-18"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 0


def test_middleware_call_passes_through_for_list_tools(tracer_provider):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context for list_tools method
    message = MockToolCallMessage(name="", meta=None)
    ctx = MockMiddlewareContext(message=message, method="tools/list")

    async def call_next(context):
        return []

    # Call the middleware using __call__
    result = asyncio.run(middleware(ctx, call_next))

    # Should return the result without creating spans
    assert result == []
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 0


def test_middleware_is_callable():
    """Test that middleware is callable (required for functools.partial)."""
    middleware = FastMCPTracingMiddleware()
    assert callable(middleware)


def test_middleware_works_with_functools_partial(tracer_provider):
    """Test that middleware works with functools.partial (as FastMCP uses it).

    This test simulates how FastMCP builds the middleware chain using
    functools.partial, which was failing before the __call__ method was added.
    """
    from functools import partial

    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context for tool call
    message = MockToolCallMessage(name="partial-tool", meta=None)
    ctx = MockMiddlewareContext(message=message, method="tools/call")

    async def final_handler(context):
        return "final-result"

    # Simulate how FastMCP builds the middleware chain
    # This would fail with "the first argument must be callable" before the fix
    chain = partial(middleware, call_next=final_handler)

    # Verify that partial worked (middleware is callable)
    assert callable(chain)

    # Call the chain
    result = asyncio.run(chain(ctx))

    assert result == "final-result"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.name == "partial-tool"


def test_debug_logging_when_enabled(tracer_provider, parent_context):
    """Test that debug logging outputs expected information when FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG=1."""
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    parent_span_context, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create mock context with tool call message that includes _meta with OTEL fields
    message = MockToolCallMessage(name="test-tool", arguments={"arg": "value"}, meta=meta)
    ctx = MockMiddlewareContext(message=message, method="tools/call", source="client")

    async def call_next(context):
        return "result"

    # Capture stderr output
    stderr_capture = StringIO()
    with patch.dict(os.environ, {"FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG": "1"}):
        with patch("sys.stderr", stderr_capture):
            asyncio.run(middleware.on_call_tool(ctx, call_next))

    debug_output = stderr_capture.getvalue()

    # Verify the debug output contains expected information
    assert "[FASTMCP OTEL DEBUG]" in debug_output
    assert "Tool Name: test-tool" in debug_output
    assert "Span Name: test-tool" in debug_output
    assert "MCP Method: tools/call" in debug_output
    assert "MCP Source: client" in debug_output
    assert "OTEL_FIELD_ALIASES propagated from _meta:" in debug_output
    assert "traceparent" in debug_output
    assert "Extracted OpenTelemetry Context:" in debug_output
    # Context extraction may succeed even if span details are unavailable
    assert "Trace ID:" in debug_output or "Context extracted successfully" in debug_output
    assert "Raw _meta information:" in debug_output
    assert "otel" in debug_output  # The _meta contains an 'otel' key


def test_debug_logging_when_disabled(tracer_provider, parent_context):
    """Test that no debug logging occurs when FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG is not set."""
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    _, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    message = MockToolCallMessage(name="test-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        return "result"

    # Capture stderr output with debug logging disabled
    stderr_capture = StringIO()
    with patch.dict(os.environ, {"FASTMCP_OTEL_MIDDLEWARE_DEBUG_LOG": "0"}, clear=True):
        with patch("sys.stderr", stderr_capture):
            asyncio.run(middleware.on_call_tool(ctx, call_next))

    debug_output = stderr_capture.getvalue()

    # Verify no debug output was produced
    assert "[FASTMCP OTEL DEBUG]" not in debug_output


def test_middleware_extracts_meta_from_request_context(tracer_provider, parent_context):
    """Test that middleware extracts _meta from ctx.request_context.meta."""
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    parent_span_context, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create context with _meta containing OTEL context
    message = MockToolCallMessage(name="test-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        return "result"

    result = asyncio.run(middleware.on_call_tool(ctx, call_next))

    assert result == "result"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    # Verify parent trace is propagated
    assert span.parent is not None
    assert span.parent.span_id == parent_span_context.span_id


def test_traceparent_extracts_trace_id_span_id_and_flags(tracer_provider):
    """Test that trace_id, span_id, trace_flags, and is_remote are extracted from traceparent."""
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    # Create a meta dict with only traceparent (no baggage or tracestate)
    expected_trace_id = 0x4BF92F3577B34DA6A3CE929D0E0E4736
    expected_span_id = 0x00F067AA0BA902B7
    traceparent = f"00-{expected_trace_id:032x}-{expected_span_id:016x}-01"
    meta = {"traceparent": traceparent}

    message = MockToolCallMessage(name="test-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        # Verify the parent context is active during tool execution
        current_span = trace.get_current_span()
        assert current_span.get_span_context().is_valid
        return "result"

    asyncio.run(middleware.on_call_tool(ctx, call_next))

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]

    # Verify parent span context contains the expected trace_id, span_id, and trace_flags
    assert span.parent is not None
    assert span.parent.trace_id == expected_trace_id
    assert span.parent.span_id == expected_span_id
    # trace_flags should be set (SAMPLED in the stub implementation)
    assert span.parent.trace_flags == TraceFlags.SAMPLED
    # is_remote should be True since context was propagated from external client
    assert span.parent.is_remote is True


def test_meta_carrier_getter_handles_dataclass_objects():
    """Test that MetaCarrierGetter can extract context from dataclass objects.

    This tests the fix for the issue where _meta is a dataclass (not a dict)
    and the getter needs to use __dict__ to access attributes.
    """
    from dataclasses import dataclass

    @dataclass
    class Meta:
        progressToken: int | None = None
        traceparent: str | None = None
        tracestate: str | None = None

    meta = Meta(
        progressToken=1,
        traceparent="00-3894bc47d5ebfd5771e669ed370972d4-d4908ee9316cf66b-01",
    )

    getter = MetaCarrierGetter()

    # Should be able to get values from dataclass attributes
    values = getter.get(meta, "traceparent")
    assert values == ["00-3894bc47d5ebfd5771e669ed370972d4-d4908ee9316cf66b-01"]

    # Should work with keys() too
    keys = list(getter.keys(meta))
    assert "traceparent" in keys


def test_middleware_extracts_context_from_dataclass_meta(tracer_provider):
    """Test that middleware can extract parent context from dataclass _meta.

    This tests the end-to-end flow when FastMCP provides _meta as a dataclass
    instead of a plain dict.
    """
    from dataclasses import dataclass

    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    @dataclass
    class Meta:
        progressToken: int | None = None
        traceparent: str | None = None

    meta = Meta(
        progressToken=1,
        traceparent="00-3894bc47d5ebfd5771e669ed370972d4-d4908ee9316cf66b-01",
    )

    message = MockToolCallMessage(name="test-tool", meta=meta)
    ctx = MockMiddlewareContext(message=message)

    async def call_next(context):
        return "result"

    asyncio.run(middleware.on_call_tool(ctx, call_next))

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]

    # Verify parent context was extracted correctly
    assert span.parent is not None
    assert span.parent.trace_id == 0x3894BC47D5EBFD5771E669ED370972D4
    assert span.parent.span_id == 0xD4908EE9316CF66B
    assert span.parent.is_remote is True
