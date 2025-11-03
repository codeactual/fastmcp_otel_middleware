import asyncio

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


class MockMiddlewareContext:
    """Mock FastMCP middleware context."""

    def __init__(
        self, message: MockToolCallMessage, method: str = "tools/call", source: str = "client"
    ):
        self.message = message
        self.method = method
        self.source = source


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
    middleware = FastMCPTracingMiddleware(tracer=tracer)

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
    assert span.attributes["fastmcp.tool.name"] == "my-tool"
    assert span.attributes["mcp.method"] == "tools/call"
    assert span.attributes["mcp.source"] == "client"
    assert span.attributes["fastmcp.tool.success"] is True
    assert span.kind.name == "SERVER"


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
