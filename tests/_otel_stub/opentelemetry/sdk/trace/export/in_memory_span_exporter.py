"""Minimal in-memory span exporter stub."""
from __future__ import annotations

from typing import List


class InMemorySpanExporter:
    def __init__(self):
        self._finished: List[object] = []

    def export(self, spans: List[object]) -> None:
        self._finished.extend(spans)

    def get_finished_spans(self) -> List[object]:
        return list(self._finished)

    def clear(self) -> None:
        self._finished.clear()
