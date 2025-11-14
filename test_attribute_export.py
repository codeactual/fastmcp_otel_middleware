"""Test to verify span attributes are actually being set correctly."""

import asyncio
import json
import textwrap

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastmcp_otel_middleware.middleware import FastMCPTracingMiddleware


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


class MockMiddlewareContext:
    """Mock FastMCP middleware context."""

    def __init__(
        self,
        message: MockToolCallMessage,
        method: str = "tools/call",
        source: str = "client",
    ):
        self.message = message
        self.method = method
        self.source = source
        self.request_context = MockRequestContext(meta=message._meta)


def test_attribute_export():
    """Test that span attributes are properly set and exported."""

    async def run_test():
        """Execute the async portion of the attribute export test."""
        # Set up tracing
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer(__name__)

        # Create middleware
        middleware = FastMCPTracingMiddleware(
            tracer=tracer,
            include_arguments=True,
            langfuse_compatible=True,  # Opt-in (disabled by default)
        )

        # Create mock context
        message = MockToolCallMessage(
            name="get_temperature", arguments={"city": "Boston", "units": "celsius"}
        )
        ctx = MockMiddlewareContext(message=message)

        # Call the middleware
        async def call_next(context):
            return {"temperature": 22.5}

        result = await middleware.on_call_tool(ctx, call_next)

        # Get the exported spans
        spans = exporter.get_finished_spans()
        assert len(spans) == 1, f"Expected 1 span, got {len(spans)}"

        span = spans[0]

        # Print span details
        print("\n" + "=" * 80)
        print("SPAN EXPORT ANALYSIS")
        print("=" * 80)

        print(f"\nSpan Name: {span.name}")
        print(f"Span Kind: {span.kind.name}")
        print(f"Status: {span.status.status_code.name}")

        print("\n" + "-" * 80)
        print("SPAN ATTRIBUTES (what gets sent to Langfuse/OTLP):")
        print("-" * 80)

        # Group attributes
        standard_attrs = {}
        langfuse_attrs = {}

        for key, value in span.attributes.items():
            if key.startswith("langfuse."):
                langfuse_attrs[key] = value
            else:
                standard_attrs[key] = value

        print("\n📊 Standard OpenTelemetry Attributes:")
        print("(These appear in Langfuse under metadata.attributes - VISIBLE but NOT queryable)")
        for key, value in sorted(standard_attrs.items()):
            print(f"  • {key}: {value!r}")

        if langfuse_attrs:
            print("\n🔍 Langfuse-Compatible Attributes (with langfuse.* prefix):")
            print("(These appear in Langfuse as top-level metadata fields - VISIBLE AND queryable)")
            for key, value in sorted(langfuse_attrs.items()):
                print(f"  • {key}: {value!r}")
        else:
            print("\n⚠️  No Langfuse-specific attributes set (langfuse_compatible=False or not set)")

        print("\n" + "-" * 80)
        print("HOW TO VIEW IN LANGFUSE:")
        print("-" * 80)
        print(
            textwrap.dedent(
                """
                1. In Langfuse UI, go to the trace detail page
                2. Click on the span/observation (e.g., "get_temperature")
                3. Look at the "Metadata" section
                4. You should see:
                   - metadata.attributes.fastmcp.tool.name: "get_temperature"
                   - metadata.attributes.mcp.method: "tools/call"
                   - metadata.attributes.mcp.source: "client"
                   - metadata.attributes.fastmcp.tool.success: true
                   - metadata.attributes.fastmcp.tool.arguments: "{'city': 'Boston', 'units': 'celsius'}"

                5. If langfuse_compatible=True, you'll ALSO see top-level fields:
                   - metadata.tool_name: "get_temperature" (queryable!)
                   - metadata.mcp_method: "tools/call" (queryable!)
                   - etc.
                """
            ).strip()
        )

        print("\n" + "-" * 80)
        print("JSON EXPORT SIMULATION:")
        print("-" * 80)
        print("\nThis is roughly what would be sent to Langfuse (simplified):\n")

        export_data = {
            "name": span.name,
            "kind": span.kind.name,
            "metadata": {
                "attributes": {k: v for k, v in standard_attrs.items()},
                "langfuse_compatible_fields": {
                    k.replace("langfuse.observation.metadata.", ""): v
                    for k, v in langfuse_attrs.items()
                }
                if langfuse_attrs
                else None,
            },
        }

        print(json.dumps(export_data, indent=2))

        print("\n" + "=" * 80)
        print("✅ Test Complete!")
        print("=" * 80 + "\n")

        return result

    asyncio.run(run_test())


if __name__ == "__main__":
    print("\n🔬 Testing Span Attribute Export\n")
    test_attribute_export()
