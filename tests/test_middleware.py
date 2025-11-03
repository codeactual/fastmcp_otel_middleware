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
    default_attributes_factory,
    default_span_name_factory,
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

    async def call_next(*args, **kwargs):
        # The extracted context should be active while the handler runs.
        assert trace.get_current_span().get_span_context().is_valid
        return "result"

    result = asyncio.run(middleware(call_next, tool_name="my-tool", call_id="123", _meta=meta))

    assert result == "result"
    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.name == "my-tool"
    assert span.parent is not None
    assert span.parent.span_id == parent_span_context.span_id
    assert span.attributes["fastmcp.tool.name"] == "my-tool"
    assert span.attributes["fastmcp.tool.call_id"] == "123"
    assert span.attributes["fastmcp.tool.success"] is True
    assert span.kind.name == "SERVER"


def test_middleware_records_exceptions(tracer_provider, parent_context):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    _, meta = parent_context
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    async def call_next(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(middleware(call_next, tool_name="error-tool", _meta=meta))

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 1
    span = finished_spans[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["fastmcp.tool.success"] is False


def test_middleware_uses_factories(tracer_provider):
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)
    middleware = FastMCPTracingMiddleware(
        tracer=tracer,
        span_name_factory=lambda args, kwargs: "custom-span",
        attributes_factory=lambda args, kwargs: {"extra": "value"},
        record_successful_result=False,
    )

    async def call_next(*args, **kwargs):
        return "ok"

    asyncio.run(middleware(call_next, 123, name="tool", call_id="cid"))

    span = exporter.get_finished_spans()[0]
    assert span.name == "custom-span"
    assert span.attributes["extra"] == "value"
    assert "fastmcp.tool.success" not in span.attributes


def test_default_span_name_factory_prefers_kwargs():
    assert default_span_name_factory(tuple(), {"tool_name": "first"}) == "first"
    assert default_span_name_factory(tuple(), {"name": "second"}) == "second"

    class Obj:
        name = "attr"

    assert default_span_name_factory((Obj(),), {}) == "attr"


def test_default_attributes_factory_extracts_fields():
    attrs = default_attributes_factory(
        tuple(), {"tool_name": "tn", "call_id": "cid", "namespace": "ns"}
    )
    assert attrs == {
        "fastmcp.tool.name": "tn",
        "fastmcp.tool.call_id": "cid",
        "fastmcp.tool.namespace": "ns",
    }


def test_instrument_fastmcp_supports_various_registration_paths():
    class MiddlewareContainer:
        def __init__(self):
            self.added = []

        def add(self, middleware):
            self.added.append(middleware)

    class App:
        def __init__(self):
            self.middleware = MiddlewareContainer()
            self.add_middleware = self.middleware.add

    app = App()

    middleware = instrument_fastmcp(app, record_successful_result=False)

    assert middleware in app.middleware.added
    assert middleware.record_successful_result is False
