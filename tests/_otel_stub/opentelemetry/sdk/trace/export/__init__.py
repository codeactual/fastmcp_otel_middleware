"""Minimal span exporter stubs."""

from __future__ import annotations


class SimpleSpanProcessor:
    def __init__(self, exporter):
        self._exporter = exporter

    def on_end(self, span: object) -> None:
        self._exporter.export([span])
