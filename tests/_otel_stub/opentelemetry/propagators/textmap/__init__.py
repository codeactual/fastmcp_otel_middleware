"""Minimal textmap propagator classes."""

from __future__ import annotations

from typing import Generic, Iterable, Mapping, MutableMapping, Sequence, TypeVar

T = TypeVar("T", bound=Mapping[str, str] | MutableMapping[str, str])


class Getter(Generic[T]):
    def get(self, carrier: T | None, key: str) -> Sequence[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def keys(self, carrier: T | None) -> Iterable[str]:  # pragma: no cover - interface
        raise NotImplementedError


class Setter(Generic[T]):
    def set(self, carrier: T, key: str, value: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class DictSetter(Setter[MutableMapping[str, str]]):
    def set(self, carrier: MutableMapping[str, str], key: str, value: str) -> None:
        carrier[key] = value


class TextMapPropagator:
    def extract(self, carrier: T | None, getter: Getter[T]) -> object:
        raise NotImplementedError

    def inject(self, carrier: MutableMapping[str, str], context: object) -> None:
        raise NotImplementedError
