"""Modops Provider — RPC списка и жизненного цикла модулей."""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, NoReturn

from core.task_decorator import task
from modules_system.runtime_registry import ModuleRuntimeRegistry, merge_runtime_views

from .errors import ForbiddenError, ModopsError
from .module_git import ModuleGitError, apply_module_update, check_module_update
from .module_ops import RELOAD_FORBIDDEN, broadcast_reload

__all__ = ["ModopsProvider", "ModopsError", "ForbiddenError"]


class ModopsProvider:
    """Провайдер modops: Redis merge, git update, hot-reload."""

    def __init__(self, log: Any = None) -> None:
        self._log = log
        self._state: Any = None
        self._modules_registry: ModuleRuntimeRegistry | None = None
        self._app: Any = None

    def bind(self, state: Any) -> None:
        self._state = state
        self._app = state

    @task(
        type="cpu",
        api=True,
        permission="modops:read",
        name="list",
    )
    async def list(
        self, _session_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Merge Redis belle + belle-worker. Пустой Redis → list_all/discover."""
        registry = self._modules_runtime()
        belle = registry.view_service("belle")
        worker = registry.view_service("belle-worker")
        if belle or worker:
            return {"items": merge_runtime_views(belle, worker), "degraded": False}
        return {"items": self._modules_fallback(), "degraded": True}

    @task(
        type="io",
        api=True,
        permission="modops:write",
        name="reload",
        timeout=30,
    )
    async def reload(
        self, name: str, _session_user_id: str | None = None,
    ) -> dict[str, Any]:
        key = str(name or "").strip()
        if not key:
            raise ModopsError("Module name required", "VALIDATION", human="Invalid request")
        if key in RELOAD_FORBIDDEN:
            raise ForbiddenError("Cannot reload this module")
        self._broadcast_reload(key)
        return {"name": key, "reloaded": True}

    @task(
        type="io",
        api=True,
        permission="modops:write",
        name="check_update",
        timeout=30,
    )
    async def check_update(
        self, name: str, _session_user_id: str | None = None,
    ) -> dict[str, Any]:
        return self._git_call(check_module_update, name)

    @task(
        type="io",
        api=True,
        permission="modops:write",
        name="update",
        timeout=60,
    )
    async def update(
        self, name: str, _session_user_id: str | None = None,
    ) -> dict[str, Any]:
        result = self._git_call(apply_module_update, name)
        key = str(result.get("name") or name).strip()
        if key not in RELOAD_FORBIDDEN:
            self._broadcast_reload(key)
        return result

    @task(
        type="cpu",
        api=True,
        permission="modops:write",
        name="unload",
    )
    async def unload(
        self, name: str, _session_user_id: str | None = None,
    ) -> NoReturn:
        self._stub_module_action(name, block_core=True)

    @task(
        type="cpu",
        api=True,
        permission="modops:write",
        name="disable",
    )
    async def disable(
        self, name: str, _session_user_id: str | None = None,
    ) -> NoReturn:
        self._stub_module_action(name, block_core=True)

    @task(
        type="cpu",
        api=True,
        permission="modops:write",
        name="enable",
    )
    async def enable(
        self, name: str, _session_user_id: str | None = None,
    ) -> NoReturn:
        self._stub_module_action(name, block_core=False)

    @task(
        type="cpu",
        api=True,
        permission="modops:write",
        name="delete",
    )
    async def delete(
        self, name: str, _session_user_id: str | None = None,
    ) -> NoReturn:
        self._stub_module_action(name, block_core=True)

    def _broadcast_reload(self, name: str) -> None:
        broadcast_reload(
            self._require_app(),
            name,
            log_=self._log,
            client=self._cmd_redis(),
        )

    def _require_app(self) -> Any:
        app = self._app if self._app is not None else self._state
        if app is None or getattr(app, "modules", None) is None:
            raise ModopsError("Modops not initialized")
        return app

    def _cmd_redis(self) -> Any | None:
        registry = self._modules_registry
        return None if registry is None else getattr(registry, "_client", None)

    def _git_call(self, fn: Callable[[str], dict[str, Any]], name: str) -> dict[str, Any]:
        key = str(name or "").strip()
        if not key:
            raise ModopsError("Module name required", "VALIDATION", human="Invalid request")
        try:
            return fn(key)
        except ModuleGitError as exc:
            raise ModopsError(exc.human, exc.code, human=exc.human) from exc

    def _stub_module_action(self, name: str, *, block_core: bool) -> NoReturn:
        key = str(name or "").strip()
        if not key:
            raise ModopsError("Module name required", "VALIDATION", human="Invalid request")
        if block_core and (key in RELOAD_FORBIDDEN or self._module_is_system(key)):
            raise ForbiddenError("Cannot modify a system module")
        raise ModopsError("Not implemented yet", "NOT_IMPLEMENTED", human="Not implemented yet")

    def _module_is_system(self, name: str) -> bool:
        for item in self._module_rows():
            if item.get("name") == name:
                return bool(item.get("is_system"))
        return False

    def _module_rows(self) -> list[dict[str, Any]]:
        registry = self._modules_runtime()
        belle = registry.view_service("belle")
        worker = registry.view_service("belle-worker")
        if belle or worker:
            return merge_runtime_views(belle, worker)
        return self._modules_fallback()

    def _modules_runtime(self) -> ModuleRuntimeRegistry:
        if self._modules_registry is None:
            self._modules_registry = ModuleRuntimeRegistry.from_env("belle")
        return self._modules_registry

    def _modules_fallback(self) -> list[dict[str, Any]]:
        rows = self._fallback_rows()
        if os.environ.get("SERVICE_NAME", "").strip() == "belle":
            return merge_runtime_views(rows, {})
        return merge_runtime_views({}, rows)

    def _fallback_rows(self) -> dict[str, dict[str, Any]]:
        modules = getattr(self._state, "modules", None) if self._state is not None else None
        if modules is None:
            return {}
        loaded = list(modules.list_all()) if hasattr(modules, "list_all") else []
        discovered = list(modules.discover()) if hasattr(modules, "discover") else []
        loaded_set = set(loaded)
        rows: dict[str, dict[str, Any]] = {}
        for name in list(dict.fromkeys(discovered + loaded)):
            meta = modules.read_meta(name) if hasattr(modules, "read_meta") else None
            if meta is not None and getattr(meta, "is_example", False):
                continue
            module = modules.get(name) if hasattr(modules, "get") else None
            present = name in loaded_set
            rows[name] = {
                "name": name,
                "display_name": getattr(meta, "display_name", None) or name,
                "version": getattr(module, "version", None) or "0.0.0",
                "status": "loaded" if present else "unknown",
                "health": "ok" if present else "unknown",
                "is_system": bool(getattr(meta, "is_system", False)),
                "source": "image",
                "pid": None,
                "error": None,
            }
        return rows
