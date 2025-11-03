"""SDK trace provider stub."""

from __future__ import annotations

from typing import Any

from ...trace import TracerProvider


class SpanProcessor:
    def on_end(self, span: Any) -> None:  # pragma: no cover - interface
        raise NotImplementedError


__all__ = ["TracerProvider", "SpanProcessor"]
