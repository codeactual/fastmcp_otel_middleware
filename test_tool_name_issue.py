"""Test to reproduce the tool name issue with FastMCP middleware."""

import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastmcp_otel_middleware.middleware import (
    FastMCPTracingMiddleware,
)

# Set up tracing
provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
tracer = provider.get_tracer(__name__)


async def simulate_fastmcp_call_without_tool_name():
    """
    Simulate how FastMCP might call the middleware without passing tool_name in kwargs.

    This reproduces the issue where the tool name doesn't appear in Langfuse traces
    because FastMCP might pass the tool information differently (e.g., in a context
    object or with different parameter names).
    """
    middleware = FastMCPTracingMiddleware(tracer=tracer)

    async def call_next(*args, **kwargs):
        print(f"Handler called with args={args}, kwargs={kwargs}")
        return "result"

    # Scenario 1: Tool name in kwargs (works as expected)
    print("\n=== Scenario 1: tool_name in kwargs ===")
    await middleware(call_next, tool_name="my_tool", call_id="123")

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
    exporter.clear()

    # Scenario 2: Tool name in different parameter (doesn't work)
    print("\n=== Scenario 2: name parameter instead of tool_name ===")
    await middleware(call_next, name="my_tool", call_id="123")

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
    exporter.clear()

    # Scenario 3: No tool name at all (common issue)
    print("\n=== Scenario 3: No tool name in kwargs ===")
    await middleware(call_next, call_id="123", _meta={})

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
    exporter.clear()

    # Scenario 4: Tool name in args (object with name attribute)
    print("\n=== Scenario 4: Tool name in args as object attribute ===")

    class ToolContext:
        def __init__(self, name: str):
            self.name = name

    context = ToolContext("my_tool")
    await middleware(call_next, context, call_id="123")

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
    exporter.clear()


async def simulate_readme_example():
    """Test the README example with custom span_name_factory."""
    print("\n\n=== Testing README Example ===")

    custom_middleware = FastMCPTracingMiddleware(
        tracer=tracer,
        span_name_factory=lambda args, kwargs: f"tool:{kwargs.get('tool_name', 'unknown')}",
    )

    async def call_next(*args, **kwargs):
        return "result"

    # Case where tool_name is present
    print("\n--- With tool_name in kwargs ---")
    await custom_middleware(call_next, tool_name="my_tool", call_id="123")

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
    exporter.clear()

    # Case where tool_name is missing (the issue!)
    print("\n--- Without tool_name in kwargs (reproduces issue) ---")
    await custom_middleware(call_next, call_id="123")

    spans = exporter.get_finished_spans()
    if spans:
        span = spans[-1]
        print(f"Span name: {span.name}")
        print(f"Attributes: {dict(span.attributes)}")
        print("\nNOTE: Span name is 'tool:unknown' and 'fastmcp.tool.name' attribute is missing!")
    exporter.clear()


if __name__ == "__main__":
    print("Testing middleware behavior with different parameter patterns...\n")
    asyncio.run(simulate_fastmcp_call_without_tool_name())
    asyncio.run(simulate_readme_example())
