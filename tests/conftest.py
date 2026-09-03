"""Фикстуры modops: лог и провайдер без PG."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from modules.modops.provider import ModopsProvider

__all__ = ["RecordingLog"]


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _store(self, level: str, message: str, extra: dict[str, Any] | None = None) -> None:
        self.records.append((level, message, extra or {}))

    def debug(self, message: str, extra: dict[str, Any] | None = None) -> None:
        self._store("debug", message, extra)

    def info(self, message: str, extra: dict[str, Any] | None = None) -> None:
        self._store("info", message, extra)

    def warning(self, message: str, extra: dict[str, Any] | None = None) -> None:
        self._store("warning", message, extra)

    def error(self, message: str, extra: dict[str, Any] | None = None) -> None:
        self._store("error", message, extra)


@pytest.fixture
def log() -> RecordingLog:
    return RecordingLog()


@pytest.fixture
def provider(log: RecordingLog) -> ModopsProvider:
    prov = ModopsProvider(log=log)
    prov._state = SimpleNamespace()
    return prov
