# Investigation: Tool Name Missing from Langfuse Traces

## Issue Summary

The custom middleware example in the README does not add the tool name to the trace as visible in Langfuse. Specifically, when using a custom `span_name_factory`, the `fastmcp.tool.name` attribute is missing from the span attributes.

## Root Cause Analysis

### 1. Middleware Architecture Mismatch

FastMCP supports **two distinct middleware patterns**:

#### a) Hook-Based Middleware (Current FastMCP Standard)
```python
class MyMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name  # ✅ Direct access to tool name
        # ... create spans with tool name
```

**Characteristics:**
- Extends `Middleware` base class
- Implements hook methods (`on_call_tool`, `on_message`, etc.)
- Receives `MiddlewareContext` with structured access to MCP protocol details
- Tool name available via `context.message.name`
- This is the pattern used in FastMCP's official OpenTelemetry example

#### b) Callable Middleware (Starlette-Style)
```python
class MyMiddleware:
    async def __call__(self, call_next: MiddlewareCallable, *args, **kwargs):
        tool_name = kwargs.get("tool_name")  # ❌ May not be present
        # ... create spans
```

**Characteristics:**
- Implements `__call__` method
- Receives `*args` and `**kwargs` from the caller
- **No guaranteed structure** for how parameters are passed
- Operates at HTTP/ASGI layer, not MCP protocol layer
- Tool name availability depends on how FastMCP calls the middleware

**This repository's middleware uses the callable pattern**, which is problematic because:
1. It doesn't receive a structured `MiddlewareContext` object
2. The tool name may not be passed in `kwargs` as expected
3. It operates at the wrong layer to reliably access MCP protocol information

### 2. Inconsistency Between Factories

Even when tool name information is available, there's an inconsistency between `default_span_name_factory` and `default_attributes_factory`:

**`default_span_name_factory` (lines 123-134 in middleware.py):**
```python
def default_span_name_factory(args, kwargs):
    if "tool_name" in kwargs:
        return kwargs["tool_name"]
    if "name" in kwargs:
        return kwargs["name"]
    for value in args:  # ✅ Checks args for objects with name attribute
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return name
    return "fastmcp.tool"
```

**`default_attributes_factory` (lines 137-153 in middleware.py):**
```python
def default_attributes_factory(args, kwargs):
    attributes = {}
    tool_name = kwargs.get("tool_name") or kwargs.get("name")  # ❌ Only checks kwargs
    if isinstance(tool_name, str):
        attributes["fastmcp.tool.name"] = tool_name
    # ... other attributes
    return attributes
```

**Problem:** `default_span_name_factory` can extract the tool name from args (line 130-133), but `default_attributes_factory` only checks kwargs. This means:
- The span name might be correct
- But the `fastmcp.tool.name` attribute will be missing
- Langfuse (and other tracing backends) rely on attributes, not just span names

### 3. README Example Issue

The README example shows:
```python
custom_middleware = FastMCPTracingMiddleware(
    span_name_factory=lambda args, kwargs: f"tool:{kwargs.get('tool_name', 'unknown')}",
)
```

**Problems:**
1. Only customizes `span_name_factory`, not `attributes_factory`
2. Uses `kwargs.get('tool_name', 'unknown')` which returns 'unknown' if tool_name is missing
3. Since `attributes_factory` uses the default, it also won't find the tool name
4. Result: Span name is "tool:unknown" and `fastmcp.tool.name` attribute is missing

## Reproduction Test Results

Running `test_tool_name_issue.py` confirmed:

```
=== Scenario 3: No tool name in kwargs ===
Span name: fastmcp.tool
Attributes: {'fastmcp.tool.call_id': '123', 'fastmcp.tool.success': True}
# ❌ fastmcp.tool.name is missing

=== Scenario 4: Tool name in args as object attribute ===
Span name: my_tool  # ✅ Span name is correct
Attributes: {'fastmcp.tool.call_id': '123', 'fastmcp.tool.success': True}
# ❌ fastmcp.tool.name attribute is STILL missing!

=== README Example: Without tool_name in kwargs ===
Span name: tool:unknown
Attributes: {'fastmcp.tool.call_id': '123', 'fastmcp.tool.success': True}
# ❌ Both span name and attribute are wrong
```

## Why This Matters for Langfuse

Langfuse and other tracing backends rely on **span attributes** to:
1. Filter and search traces
2. Aggregate metrics by tool name
3. Display structured trace information
4. Link spans to specific operations

Having the tool name only in the span name is insufficient because:
- Span names are meant to be human-readable labels
- Attributes provide structured, queryable metadata
- Many observability tools filter by attributes, not span names

## Recommended Solutions

### Solution 1: Switch to Hook-Based Middleware (Recommended)

Rewrite the middleware to use FastMCP's hook-based pattern:

```python
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

class FastMCPTracingMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = context.message.name  # ✅ Reliable access

        # Extract OTel context from _meta
        meta = getattr(context.message, "_meta", None)
        parent_context = get_context_from_meta(meta)

        # Create span with proper attributes
        with tracer.start_as_current_span(tool_name, context=parent_context) as span:
            span.set_attribute("fastmcp.tool.name", tool_name)
            span.set_attribute("mcp.method", context.method)
            # ... other attributes

            result = await call_next(context)
            return result
```

**Advantages:**
- Direct access to tool name via `context.message.name`
- Access to all MCP protocol information
- Follows FastMCP's current best practices
- Matches the official OpenTelemetry example

### Solution 2: Fix the Attributes Factory

If keeping the callable pattern, make `default_attributes_factory` consistent with `default_span_name_factory`:

```python
def default_attributes_factory(args, kwargs):
    attributes = {}

    # Check kwargs first
    tool_name = kwargs.get("tool_name") or kwargs.get("name")

    # If not found in kwargs, check args (like span_name_factory does)
    if not tool_name:
        for value in args:
            name = getattr(value, "name", None)
            if isinstance(name, str):
                tool_name = name
                break

    if isinstance(tool_name, str):
        attributes["fastmcp.tool.name"] = tool_name
    # ... rest of the function
    return attributes
```

**Advantages:**
- Minimal code change
- Maintains backward compatibility
- Makes factories consistent

**Disadvantages:**
- Still relies on callable pattern which may not receive tool name reliably
- Doesn't solve the underlying architecture mismatch

### Solution 3: Update README Example

If fixing the factories, update the README to also customize `attributes_factory`:

```python
custom_middleware = FastMCPTracingMiddleware(
    span_name_factory=lambda args, kwargs: f"tool:{kwargs.get('tool_name', 'unknown')}",
    attributes_factory=lambda args, kwargs: {
        "fastmcp.tool.name": kwargs.get("tool_name", "unknown"),
        **default_attributes_factory(args, kwargs)
    }
)
```

## Impact Assessment

**Current Impact:**
- Tool names are missing from Langfuse traces
- Traces cannot be filtered or aggregated by tool name
- Observability is significantly reduced
- Users following the README example will have incomplete traces

**Affected Use Cases:**
- Any user following the README's custom middleware example
- Any deployment where tool names are not passed in kwargs as expected
- Multi-tool servers where tool-level observability is important

## Additional Notes

### FastMCP Middleware Evolution

FastMCP introduced the hook-based middleware system in version 2.9 (released as "Stuck in the Middleware With You"). The callable pattern appears to be:
1. Either from an older FastMCP version
2. Or intended for Starlette HTTP middleware (not MCP protocol middleware)

### Related FastMCP Issues

- GitHub issue #1459: "middleware doesnt work when mounted from fastapi" - Shows confusion between Starlette and FastMCP middleware
- GitHub discussion #732: "Sharing middleware between FastMCP and FastAPI" - Discusses the difference between the two middleware types
- GitHub issue #396: "Allow passing middleware directly to FastMCP app builders" - Shows interest in easier middleware integration

## Conclusion

The tool name is missing from Langfuse traces because:

1. **Primary cause**: The middleware uses a callable pattern instead of FastMCP's hook-based pattern, so it doesn't receive structured access to the tool name
2. **Secondary cause**: Even when the tool name is available in args, `default_attributes_factory` doesn't check args (unlike `default_span_name_factory`)
3. **Tertiary cause**: The README example only customizes `span_name_factory`, leaving `attributes_factory` unable to set the tool name attribute

The recommended solution is to rewrite the middleware using FastMCP's hook-based pattern (`on_call_tool` method with `MiddlewareContext`), which provides direct, reliable access to the tool name via `context.message.name`.
