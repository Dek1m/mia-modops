"""Ошибки modops. message — в лог, human — клиенту."""
from __future__ import annotations

__all__ = ["ModopsError", "ForbiddenError"]


class ModopsError(Exception):
    """Базовая ошибка modops. code — RPC, human — UI."""

    def __init__(
        self, message: str, code: str = "MODOPS_ERROR", *, human: str | None = None,
    ) -> None:
        self.code = code
        self.human = human or message
        super().__init__(message)


class ForbiddenError(ModopsError):
    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(message, "FORBIDDEN")
