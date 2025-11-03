"""Simplified tracing implementation for tests."""
from __future__ import annotations

import itertools
from contextlib import ContextDecorator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from .. import context as context_api


class StatusCode(Enum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


@dataclass
class Status:
    status_code: StatusCode
    description: str = ""


class SpanKind(Enum):
    INTERNAL = "INTERNAL"
    SERVER = "SERVER"


@dataclass
class TraceState:
    items: tuple[tuple[str, str], ...] = ()


class TraceFlags(int):
    SAMPLED = 1


@dataclass
class SpanContext:
    trace_id: int
    span_id: int
    is_remote: bool = False
    trace_flags: int = TraceFlags.SAMPLED
    trace_state: TraceState = field(default_factory=TraceState)

    @property
    def is_valid(self) -> bool:
        return bool(self.trace_id and self.span_id)


class NonRecordingSpan:
    def __init__(self, context: SpanContext):
        self._context = context

    def get_span_context(self) -> SpanContext:
        return self._context

    # The real OpenTelemetry implementation provides these no-op methods so that
    # callers can safely invoke them on the current span even after context
    # propagation has detached.
    def record_exception(self, exc: Exception) -> None:  # pragma: no cover - simple stub
        pass

    def set_status(self, status: Status) -> None:  # pragma: no cover - simple stub
        pass

    def set_attribute(self, key: str, value: Any) -> None:  # pragma: no cover - simple stub
        pass


class Span:
    def __init__(self, name: str, context: SpanContext, parent: Optional[SpanContext], kind: SpanKind):
        self.name = name
        self._context = context
        self.parent = parent
        self.kind = kind
        self.attributes: dict[str, Any] = {}
        self.status = Status(StatusCode.UNSET)
        self.events: list[Any] = []

    def get_span_context(self) -> SpanContext:
        return self._context

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: Exception) -> None:
        self.events.append(("exception", exc))

    def set_status(self, status: Status) -> None:
        self.status = status

    def end(self) -> None:
        pass


class _SpanContextManager(ContextDecorator):
    def __init__(self, span: Span, token: int, tracer: "Tracer"):
        self._span = span
        self._token = token
        self._tracer = tracer

    def __enter__(self) -> Span:
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        context_api.detach(self._token)
        self._tracer._on_end(self._span)
        return False


class Tracer:
    def __init__(self, provider: "TracerProvider", name: str):
        self._provider = provider
        self._name = name

    def start_as_current_span(
        self,
        name: str,
        *,
        context: Optional[context_api.Context] = None,
        kind: SpanKind | None = None,
    ) -> _SpanContextManager:
        parent_span_context: Optional[SpanContext] = None
        if context and getattr(context, "span", None):
            parent_span = context.span
            if hasattr(parent_span, "get_span_context"):
                parent_span_context = parent_span.get_span_context()
        else:
            current_span = get_current_span()
            if current_span and hasattr(current_span, "get_span_context"):
                parent_span_context = current_span.get_span_context()
        span_context = self._provider._next_span_context(parent_span_context)
        span = Span(name, span_context, parent_span_context, kind or SpanKind.INTERNAL)
        token = context_api.attach(context_api.Context(span=span))
        return _SpanContextManager(span, token, self)

    def _on_end(self, span: Span) -> None:
        self._provider._on_end(span)


class TracerProvider:
    def __init__(self):
        self._span_processors: List[Any] = []
        self._id_iter = itertools.count(1)

    def add_span_processor(self, processor: Any) -> None:
        self._span_processors.append(processor)

    def get_tracer(self, name: str, version: Optional[str] = None) -> Tracer:
        return Tracer(self, name)

    def _next_span_context(self, parent: Optional[SpanContext]) -> SpanContext:
        span_id = next(self._id_iter)
        if parent is None:
            trace_id = span_id << 64 | span_id
        else:
            trace_id = parent.trace_id
        return SpanContext(trace_id=trace_id, span_id=span_id, is_remote=False)

    def _on_end(self, span: Span) -> None:
        for processor in self._span_processors:
            processor.on_end(span)


_global_tracer_provider = TracerProvider()


def set_tracer_provider(provider: TracerProvider) -> None:
    global _global_tracer_provider
    _global_tracer_provider = provider


def get_tracer_provider() -> TracerProvider:
    return _global_tracer_provider


def get_tracer(name: str, version: Optional[str] = None) -> Tracer:
    return _global_tracer_provider.get_tracer(name, version)


def get_current_span() -> Span | None:
    ctx = context_api.get_current()
    return getattr(ctx, "span", None)


def set_span_in_context(span: Any) -> context_api.Context:
    return context_api.Context(span=span)

