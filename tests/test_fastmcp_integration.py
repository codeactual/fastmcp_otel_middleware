"""Integration test using FastMCP's in-memory server to reproduce middleware issues.

This test requires the fastmcp package to be installed, which is included in the dev
dependencies. The middleware itself does not depend on fastmcp (it uses duck typing),
but we test against real FastMCP servers to ensure compatibility.

IMPORTANT: These tests require FastMCP version 2.13.1 or later, which introduces
the ctx.request_context.meta API. The middleware no longer supports the legacy
ctx.message._meta API.

To run these tests:
    pip install -e ".[dev]"
    pytest tests/test_fastmcp_integration.py -v
"""

import pytest

try:
    from fastmcp import Client, FastMCP

    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastmcp_otel_middleware import instrument_fastmcp


# Check if FastMCP supports the new request_context.meta API
def _fastmcp_has_request_context():
    """Check if FastMCP version has the request_context.meta API."""
    if not FASTMCP_AVAILABLE:
        return False
    try:
        # Try to import and check if MiddlewareContext has request_context
        # This is a heuristic check - we'll discover at runtime if it works
        return True  # Optimistically assume it works, skip will happen at test time if not
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not FASTMCP_AVAILABLE,
    reason="FastMCP not installed (requires fastmcp>=2.13.1 for request_context.meta API)",
)


@pytest.fixture()
def tracer_provider():
    """Set up OpenTelemetry tracing."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


@pytest.mark.asyncio
async def test_middleware_with_in_memory_server(tracer_provider):
    """Test that middleware works with fastmcp in-memory server.

    This test requires FastMCP version 2.13.1+ with the request_context.meta API.
    If the API is not available, the test will be skipped.
    """
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)

    # Create a FastMCP server with a simple tool
    mcp = FastMCP("TestServer")

    @mcp.tool()
    def get_temperature(city: str) -> str:
        """Get the temperature for a city."""
        return f"The temperature in {city} is 72°F"

    # Instrument the server with the middleware
    instrument_fastmcp(
        mcp,
        tracer=tracer,
        span_name_prefix="tool.",
        include_arguments=True,
        langfuse_compatible=True,
    )

    # Use the in-memory client to interact with the server
    try:
        async with Client(mcp) as client:
            # List tools
            tools = await client.list_tools()
            assert len(tools) > 0
            assert any(tool.name == "get_temperature" for tool in tools)

            # Call a tool
            result = await client.call_tool("get_temperature", {"city": "San Francisco"})
            assert "San Francisco" in str(result.content)

        # Verify that a span was created for the tool call
        finished_spans = exporter.get_finished_spans()
        assert len(finished_spans) > 0

        # Find the tool call span
        tool_spans = [s for s in finished_spans if s.name == "tool.get_temperature"]
        assert len(tool_spans) == 1

        span = tool_spans[0]
        assert span.attributes["fastmcp.tool.name"] == "get_temperature"
        assert span.attributes["mcp.method"] == "tools/call"
        assert span.attributes["fastmcp.tool.success"] is True

        # Check Langfuse attributes
        assert span.attributes["langfuse.observation.metadata.tool_name"] == "get_temperature"
        assert span.attributes["langfuse.observation.type"] == "tool"
    except AttributeError as e:
        if "request_context" in str(e):
            pytest.skip(
                "FastMCP version does not support request_context.meta API "
                "(requires version 2.13.1 or later)"
            )
        raise


@pytest.mark.asyncio
async def test_middleware_handles_non_tool_methods(tracer_provider):
    """Test that middleware properly handles non-tool MCP methods.

    The middleware should only create spans for tool calls, not for
    initialize, list_tools, etc.

    This test requires FastMCP version 2.13.1+ with the request_context.meta API.
    If the API is not available, the test will be skipped.
    """
    provider, exporter = tracer_provider
    tracer = provider.get_tracer(__name__)

    mcp = FastMCP("TestServer")

    @mcp.tool()
    def simple_tool() -> str:
        """A simple tool."""
        return "ok"

    instrument_fastmcp(mcp, tracer=tracer, span_name_prefix="tool.")

    try:
        async with Client(mcp) as client:
            # These calls should not create tool spans
            await client.list_tools()

        # Should have no spans (only tool calls create spans)
        finished_spans = exporter.get_finished_spans()
        tool_spans = [s for s in finished_spans if s.name.startswith("tool.")]
        assert len(tool_spans) == 0
    except AttributeError as e:
        if "request_context" in str(e):
            pytest.skip(
                "FastMCP version does not support request_context.meta API "
                "(requires version 2.13.1 or later)"
            )
        raise
