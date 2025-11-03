"""Simplified context management used by the FastMCP tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List


@dataclass
class Context:
    span: Any | None = None


_current_stack: List[Context] = [Context()]


def get_current() -> Context:
    return _current_stack[-1]


def set_current(context: Context) -> None:
    if not isinstance(context, Context):
        context = Context(span=getattr(context, "span", None))
    _current_stack[-1] = context


def attach(context: Context) -> int:
    _current_stack.append(context)
    return len(_current_stack) - 1


def detach(token: int) -> None:
    if 0 <= token < len(_current_stack):
        del _current_stack[token:]
        if not _current_stack:
            _current_stack.append(Context())

