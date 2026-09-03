"""Modops Module — операционка модулей Mia Framework.

Использование:
    app.load_module("modops")

    provider = app.services.resolve(ModopsProvider)
"""
from __future__ import annotations

import os
from typing import Any

from modules_system.module_base import ModuleBase, ModuleMeta

from .module_ops import start_cmd_watcher
from .provider import ModopsProvider
from .schema import AUTH_SCHEMA

__all__ = [
    "ModopsModule",
    "ModopsProvider",
    "AUTH_SCHEMA",
]

MODULE_VERSION = "1.0.0"


class ModopsModule(ModuleBase):
    """Ядро modops: list/reload/update и Redis cmd-watcher."""

    @property
    def name(self) -> str:
        return "modops"

    @property
    def version(self) -> str:
        return MODULE_VERSION

    @property
    def meta(self) -> ModuleMeta:
        return ModuleMeta(
            dependencies=["log", "db", "auth"],
            cache_rules={},
            timeout_defaults={},
            load_on="all",
            is_system=True,
            display_name="Modops",
            is_example=False,
        )

    def __init__(self) -> None:
        self._provider: ModopsProvider | None = None
        self._log: Any = None

    def on_load(self, state: Any) -> None:
        """Bind провайдер, AUTH_SCHEMA, watcher на belle."""
        self._log = state.log
        self._provider = ModopsProvider(log=self._log)
        self._provider.bind(state)
        try:
            if hasattr(state, "services") and hasattr(state.services, "register"):
                state.services.register(ModopsProvider, self._provider)
        except Exception as exc:
            if self._log is not None:
                self._log.warning(
                    "failed_to_register_modops_provider", extra={"error": str(exc)},
                )
        try:
            from modules.auth.provider import AuthProvider

            auth = state.services.resolve(AuthProvider)
            if auth.registry is not None:
                auth.registry.register_sync("modops", AUTH_SCHEMA, is_builtin=False)
        except Exception as exc:
            if self._log is not None:
                self._log.warning(
                    "failed_to_register_modops_auth", extra={"error": str(exc)},
                )
        state.modops = self._provider
        if os.environ.get("SERVICE_NAME", "").strip() == "belle":
            start_cmd_watcher(state, self._log)
        if self._log is not None:
            self._log.info("modops_module_loaded", extra={"version": self.version})

    def on_unload(self) -> None:
        self._provider = None
        if self._log is not None:
            self._log.info("modops_module_unloaded")
        self._log = None
