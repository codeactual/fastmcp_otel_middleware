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
from fastmcp_otel_middleware import FastMCPTracingMiddleware, instrument_fastmcp

app = FastMCP()

# Attach the middleware.  The helper attempts to detect the correct registration
# mechanism, but you can provide a custom ``register`` callback if your FastMCP
# version uses a different API.
instrument_fastmcp(app)

# Optionally customise how spans are named or which attributes are added.
custom_middleware = FastMCPTracingMiddleware(
    span_name_factory=lambda args, kwargs: f"tool:{kwargs.get('tool_name', 'unknown')}",
)
instrument_fastmcp(app, middleware=custom_middleware)
```

When a client invokes a tool and includes tracing headers inside the `_meta`
object, the middleware extracts the headers, continues the trace, and wraps the
handler invocation with a server span.  The span contains useful metadata such
as the tool name, call identifier, and whether the invocation completed
successfully.

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

## References

- [_meta field specification](https://modelcontextprotocol.io/specification/2025-06-18/basic#meta)
- [Langfuse MCP tracing example](https://github.com/langfuse/langfuse-examples/blob/main/applications/mcp-tracing/src/utils/otel_utils.py)
- [FastMCP OpenTelemetry example](https://github.com/jlowin/fastmcp/blob/2e6e8256d9b0865bf8ce93797be6480be0651685/examples/opentelemetry_example.py)
