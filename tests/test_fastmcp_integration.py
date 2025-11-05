"""Integration test using FastMCP's in-memory server to reproduce middleware issues."""

import pytest
from fastmcp import FastMCP
from mcp.client import Client
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastmcp_otel_middleware import instrument_fastmcp


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

    This test reproduces the issue where the middleware encounters:
    WARNING:root:Failed to validate request: the first argument must be callable

    when fastmcp tries to call the middleware for initialize and other methods.
    """
    provider, exporter = tracer_provider

    # Create a FastMCP server with a simple tool
    mcp = FastMCP("TestServer")

    @mcp.tool()
    def get_temperature(city: str) -> str:
        """Get the temperature for a city."""
        return f"The temperature in {city} is 72°F"

    # Instrument the server with the middleware
    instrument_fastmcp(
        mcp,
        span_name_prefix="tool.",
        include_arguments=True,
        langfuse_compatible=True,
    )

    # Use the in-memory client to interact with the server
    async with Client(mcp) as client:
        # This initialize call should work without errors
        # Previously it would fail with "the first argument must be callable"

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


@pytest.mark.asyncio
async def test_middleware_handles_non_tool_methods(tracer_provider):
    """Test that middleware properly handles non-tool MCP methods.

    The middleware should only create spans for tool calls, not for
    initialize, list_tools, etc.
    """
    provider, exporter = tracer_provider

    mcp = FastMCP("TestServer")

    @mcp.tool()
    def simple_tool() -> str:
        """A simple tool."""
        return "ok"

    instrument_fastmcp(mcp, span_name_prefix="tool.")

    async with Client(mcp) as client:
        # These calls should not create tool spans
        await client.list_tools()

    # Should have no spans (only tool calls create spans)
    finished_spans = exporter.get_finished_spans()
    tool_spans = [s for s in finished_spans if s.name.startswith("tool.")]
    assert len(tool_spans) == 0
