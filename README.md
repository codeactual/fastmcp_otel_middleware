# FastMCP OpenTelemetry Middleware

This package contains lightweight utilities for wiring [OpenTelemetry](https://opentelemetry.io/) tracing into [FastMCP](https://github.com/jlowin/fastmcp) servers.  It is designed around the Model Context Protocol's `_meta` propagation field, allowing client applications to forward tracing information such as `traceparent` and `baggage` headers to the server.

## Features

- Extract OpenTelemetry context from `_meta` objects using the MCP basic protocol specification.
- Start server spans for each tool invocation handled by FastMCP.
- Attach useful span attributes (tool name, call ID, namespace) to provide richer trace data.
- Convenience helper for registering the middleware with FastMCP applications while remaining compatible with multiple FastMCP releases.

## Installation

Add the directory to your Python path or package it with your FastMCP server.  The package has no runtime dependency on `fastmcp` to keep it lightweight, but it does require the OpenTelemetry API package:

```bash
pip install opentelemetry-api
```

Depending on your exporter you may also want `opentelemetry-sdk` and the exporter implementation of your choice (OTLP, Jaeger, etc.).

## Usage

```python
from fastmcp import FastMCP
from fastmcp_otel_middleware import instrument_fastmcp

app = FastMCP("MyServer")

# Basic usage: attach the middleware with default configuration
instrument_fastmcp(app)

# Optionally customize span naming and other behavior
instrument_fastmcp(
    app,
    span_name_prefix="tool.",  # Creates spans like "tool.get_temperature"
    include_arguments=True,     # Include tool arguments in span attributes
    langfuse_compatible=True,   # Enable Langfuse-prefixed metadata (disabled by default)
)
```

When a client invokes a tool and includes tracing headers inside the `_meta`
object, the middleware extracts the headers, continues the trace, and wraps the
handler invocation with a server span. The span automatically includes:

- **Tool name**: `fastmcp.tool.name` attribute from `context.message.name`
- **MCP metadata**: `mcp.method` and `mcp.source` attributes
- **Success status**: `fastmcp.tool.success` indicating if the call succeeded
- **Arguments** (optional): `fastmcp.tool.arguments` if `include_arguments=True`

Enable the `langfuse_compatible` option when you need Langfuse-prefixed metadata
fields (for example, to make attributes queryable in the Langfuse UI). The
middleware leaves these attributes out by default to keep traces lean for other
exporters.

## Extracting Context Manually

The `get_context_from_meta` helper can be used independently of the middleware
for cases where you need to manually work with the propagated context:

```python
from fastmcp_otel_middleware import get_context_from_meta

meta = {
    "otel": {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "baggage": "userId=alice"
    }
}

ctx = get_context_from_meta(meta)
```

## Requirements

- **FastMCP 2.9+**: This middleware uses the hook-based middleware system introduced in FastMCP 2.9
- **Python 3.12+**: Required for proper type annotation support
- **OpenTelemetry API**: For tracing functionality

## How It Works

This middleware uses FastMCP's hook-based middleware pattern (introduced in v2.9) to reliably access tool names and MCP protocol information. The `on_call_tool` hook receives a `MiddlewareContext` object that contains:

- `context.message.name`: The tool name being invoked
- `context.message.arguments`: The tool arguments
- `context.message._meta`: Metadata sent by the client (including OTel headers)
- `context.method`: The MCP method (e.g., "tools/call")
- `context.source`: Source of the request ("client" or "server")

This approach ensures that tool names are always correctly captured in traces, unlike older callable-style middleware that relied on kwargs.

## References

- [_meta field specification](https://modelcontextprotocol.io/specification/2025-06-18/basic#meta)
- [FastMCP Middleware Documentation](https://gofastmcp.com/servers/middleware)
- [Langfuse MCP tracing example](https://github.com/langfuse/langfuse-examples/blob/main/applications/mcp-tracing/src/utils/otel_utils.py)
- [FastMCP OpenTelemetry example](https://github.com/jlowin/fastmcp/blob/2e6e8256d9b0865bf8ce93797be6480be0651685/examples/opentelemetry_example.py)
