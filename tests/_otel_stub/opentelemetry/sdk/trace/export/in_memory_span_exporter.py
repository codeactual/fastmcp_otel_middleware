"""Minimal in-memory span exporter stub."""

from __future__ import annotations


class InMemorySpanExporter:
    def __init__(self):
        self._finished: list[object] = []

    def export(self, spans: list[object]) -> None:
        self._finished.extend(spans)

    def get_finished_spans(self) -> list[object]:
        return list(self._finished)

    def clear(self) -> None:
        self._finished.clear()
