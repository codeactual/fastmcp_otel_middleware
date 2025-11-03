"""Stub propagate module with a global textmap propagator."""
from __future__ import annotations

from ..propagators.textmap import TextMapPropagator
from ..trace.propagation.tracecontext import TraceContextTextMapPropagator

_global_textmap: TextMapPropagator = TraceContextTextMapPropagator()


def get_global_textmap() -> TextMapPropagator:
    return _global_textmap


def set_global_textmap(propagator: TextMapPropagator) -> None:
    global _global_textmap
    _global_textmap = propagator

