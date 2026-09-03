"""Hot-reload: mock registry, FORBIDDEN ядра, без живого celery."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from modules.modops.module_ops import (
    CMD_KEY,
    consume_reload_command,
    purge_import,
    publish_reload_command,
    reload_local,
    schedule_pool_restart,
)
from modules.modops.provider import ForbiddenError, ModopsError, ModopsProvider
from modules.modops.tests.conftest import RecordingLog
from modules.modops.tests.test_modules_list import FakeRedis
from modules_system.module_base import ModuleMeta
from modules_system.runtime_registry import ModuleRuntimeRegistry


class FakeModules:
    def __init__(self) -> None:
        self.unloaded: list[str] = []
        self.loaded: list[tuple[str, Any]] = []
        self.collected = 0
        self.published = 0
        self._loaded: dict[str, object] = {"fs": object()}
        self._metas = {
            "fs": ModuleMeta(dependencies=["db"]),
            "notification": ModuleMeta(dependencies=["fs"]),
        }

    def list_all(self) -> list[str]:
        return list(self._loaded)

    def unload(self, name: str) -> None:
        self.unloaded.append(name)
        self._loaded.pop(name, None)

    def load(self, name: str, state: Any = None) -> object:
        inst = object()
        self.loaded.append((name, state))
        self._loaded[name] = inst
        return inst

    def read_meta(self, name: str) -> ModuleMeta:
        return self._metas.get(name, ModuleMeta())

    def get(self, name: str) -> object | None:
        return self._loaded.get(name)


def _app(modules: FakeModules) -> SimpleNamespace:
    def publish() -> None:
        modules.published += 1

    def collect() -> None:
        modules.collected += 1

    return SimpleNamespace(
        modules=modules,
        publish_runtime=publish,
        _collect_apiproxy=collect,
    )


def test_purge_import_drops_package_and_children() -> None:
    sys.modules["modules.fs"] = SimpleNamespace()
    sys.modules["modules.fs.provider"] = SimpleNamespace()
    sys.modules["modules.fsx"] = SimpleNamespace()
    purge_import("fs")
    assert "modules.fs" not in sys.modules
    assert "modules.fs.provider" not in sys.modules
    assert "modules.fsx" in sys.modules
    del sys.modules["modules.fsx"]


def test_reload_local_unload_purge_load() -> None:
    modules = FakeModules()
    app = _app(modules)
    sys.modules["modules.fs.provider"] = SimpleNamespace()
    reload_local(app, "fs")
    assert modules.unloaded == ["fs"]
    assert modules.loaded == [("fs", app)]
    assert modules.published == 1
    assert modules.collected == 1
    assert "modules.fs.provider" not in sys.modules


@pytest.mark.parametrize(
    "name",
    ["log", "db", "auth", "apiproxy", "rest", "system", "worker", "modops"],
)
def test_reload_local_forbidden_core(name: str) -> None:
    with pytest.raises(ForbiddenError) as exc:
        reload_local(_app(FakeModules()), name)
    assert exc.value.code == "FORBIDDEN"


def test_reload_local_blocked_by_loaded_dependent() -> None:
    modules = FakeModules()
    modules._loaded["workspace"] = object()
    modules._metas["workspace"] = ModuleMeta(dependencies=["fs"])
    app = _app(modules)
    reload_local(app, "fs")
    assert modules.unloaded == ["fs"]
    assert modules.loaded == [("fs", app)]
    assert "workspace" in modules._loaded


@pytest.mark.asyncio
async def test_modules_reload_rpc(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = FakeModules()
    provider._app = _app(modules)
    redis = FakeRedis()
    provider._modules_registry = ModuleRuntimeRegistry("belle", client=redis)
    monkeypatch.setattr("modules.modops.module_ops.schedule_pool_restart", lambda *_a, **_k: None)
    result = await provider.reload("fs")
    assert result == {"name": "fs", "reloaded": True}
    assert modules.unloaded == ["fs"]
    assert modules.loaded[0][0] == "fs"
    raw = redis.get(CMD_KEY)
    assert raw is not None
    cmd = json.loads(raw)
    assert cmd["op"] == "reload"
    assert cmd["name"] == "fs"
    assert cmd["ts"] > 0


@pytest.mark.asyncio
async def test_modules_reload_forbidden_system(provider: ModopsProvider) -> None:
    with pytest.raises(ForbiddenError):
        await provider.reload("system")


@pytest.mark.asyncio
async def test_modules_reload_forbidden_modops(provider: ModopsProvider) -> None:
    with pytest.raises(ForbiddenError):
        await provider.reload("modops")


@pytest.mark.asyncio
async def test_modules_reload_empty_name(provider: ModopsProvider) -> None:
    with pytest.raises(ModopsError) as exc:
        await provider.reload("  ")
    assert exc.value.code == "VALIDATION"


def test_schedule_pool_restart_warns_without_control(monkeypatch: pytest.MonkeyPatch) -> None:
    log = RecordingLog()

    class Immediate:
        def __init__(self, delay: float, fn: Any) -> None:
            self.delay = delay
            self.fn = fn

        def start(self) -> None:
            self.fn()

    monkeypatch.setattr("modules.modops.module_ops.threading.Timer", Immediate)
    monkeypatch.setattr(
        "modules.modops.module_ops._celery_app",
        lambda: SimpleNamespace(control=None),
    )
    schedule_pool_restart(log)
    assert any(item[1] == "pool_restart_skipped" for item in log.records)


def test_consume_reload_newer_ts() -> None:
    modules = FakeModules()
    app = _app(modules)
    redis = FakeRedis()
    publish_reload_command("fs", client=redis, ts=10.0)
    nxt = consume_reload_command(app, last_ts=1.0, client=redis)
    assert nxt == 10.0
    assert modules.unloaded == ["fs"]


def test_consume_reload_stale_ts() -> None:
    modules = FakeModules()
    app = _app(modules)
    redis = FakeRedis()
    publish_reload_command("fs", client=redis, ts=1.0)
    nxt = consume_reload_command(app, last_ts=5.0, client=redis)
    assert nxt == 5.0
    assert modules.unloaded == []


def test_consume_reload_redis_down() -> None:
    modules = FakeModules()
    redis = FakeRedis()
    redis.fail = True
    nxt = consume_reload_command(_app(modules), last_ts=3.0, client=redis)
    assert nxt == 3.0


@pytest.mark.asyncio
async def test_modules_update_reloads_product(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = FakeModules()
    provider._app = _app(modules)
    redis = FakeRedis()
    provider._modules_registry = ModuleRuntimeRegistry("belle", client=redis)
    monkeypatch.setattr(
        "modules.modops.provider.apply_module_update",
        lambda name: {"name": name, "sha": "abc", "version": "v1.0.0"},
    )
    monkeypatch.setattr("modules.modops.module_ops.schedule_pool_restart", lambda *_a, **_k: None)
    result = await provider.update("fs")
    assert result["sha"] == "abc"
    assert modules.unloaded == ["fs"]
    assert redis.get(CMD_KEY) is not None
