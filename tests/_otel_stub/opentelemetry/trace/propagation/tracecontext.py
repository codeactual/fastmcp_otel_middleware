"""Very small W3C trace context propagator implementation."""
from __future__ import annotations

from typing import MutableMapping, Optional

from ...propagators.textmap import DictSetter, Getter, TextMapPropagator
from .. import NonRecordingSpan, SpanContext, TraceFlags, TraceState
from ... import context as context_api


class TraceContextTextMapPropagator(TextMapPropagator):
    TRACEPARENT_HEADER = "traceparent"

    def __init__(self):
        self._setter = DictSetter()

    def extract(self, carrier: Optional[MutableMapping[str, str]], getter: Getter) -> context_api.Context:
        if carrier is None:
            return context_api.Context()
        values = getter.get(carrier, self.TRACEPARENT_HEADER)
        if not values:
            return context_api.Context()
        traceparent = values[0]
        try:
            version, trace_id_hex, span_id_hex, *_rest = traceparent.split("-")
            trace_id = int(trace_id_hex, 16)
            span_id = int(span_id_hex, 16)
        except Exception:
            return context_api.Context()
        span_context = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags.SAMPLED,
            trace_state=TraceState(),
        )
        return context_api.Context(span=NonRecordingSpan(span_context))

    def inject(self, carrier: MutableMapping[str, str], context: context_api.Context) -> None:
        span = getattr(context, "span", None)
        if not span or not hasattr(span, "get_span_context"):
            return
        span_context = span.get_span_context()
        traceparent = f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-01"
        self._setter.set(carrier, self.TRACEPARENT_HEADER, traceparent)

