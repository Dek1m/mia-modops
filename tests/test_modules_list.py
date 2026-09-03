"""modops.list — Redis merge, fallback ФС, type=cpu."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.task import TaskType
from modules.modops.provider import ForbiddenError, ModopsError, ModopsProvider
from modules_system.module_base import ModuleMeta
from modules_system.runtime_registry import HASH_PREFIX, ModuleRuntimeRegistry


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    def _guard(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict | None = None) -> int:
        self._guard()
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
            return len(mapping)
        if field is not None and value is not None:
            bucket[field] = value
            return 1
        return 0

    def hgetall(self, key: str) -> dict[str, str]:
        self._guard()
        return dict(self.hashes.get(key, {}))

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._guard()
        self.strings[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    def get(self, key: str) -> str | None:
        self._guard()
        return self.strings.get(key)

    def exists(self, key: str) -> int:
        self._guard()
        return 1 if key in self.strings else 0


def _snap(name: str, service: str = "belle") -> dict[str, Any]:
    return {
        "name": name,
        "display_name": name.title(),
        "version": "1.0.0",
        "status": "loaded",
        "health": "ok",
        "load_on": "all",
        "is_system": True,
        "is_example": False,
        "source": "image",
        "error": None,
        "pid": 1,
        "service": service,
        "updated_at": "2026-09-02T12:00:00Z",
    }


def _modules_state() -> SimpleNamespace:
    metas = {
        "auth": ModuleMeta(display_name="Auth", is_system=True),
        "sample": ModuleMeta(display_name="Sample", is_example=True),
        "fs": ModuleMeta(display_name="FS", is_system=False),
    }
    loaded = {"auth": SimpleNamespace(version="2.0.0")}
    return SimpleNamespace(
        list_all=lambda: ["auth"],
        discover=lambda: ["auth", "sample", "fs"],
        read_meta=lambda name: metas[name],
        get=lambda name: loaded.get(name),
    )


@pytest.mark.asyncio
async def test_modules_list_merges_two_snapshots(provider: ModopsProvider) -> None:
    redis = FakeRedis()
    registry = ModuleRuntimeRegistry("belle", client=redis)
    registry.publish_all([_snap("auth"), _snap("db")])
    worker = ModuleRuntimeRegistry("belle-worker", client=redis)
    worker.publish_all([_snap("auth", "belle-worker"), _snap("fs", "belle-worker")])
    provider._modules_registry = registry
    result = await provider.list()
    names = {item["name"] for item in result["items"]}
    assert names == {"auth", "db", "fs"}
    assert result["degraded"] is False
    auth = next(item for item in result["items"] if item["name"] == "auth")
    assert auth["display_name"] == "Auth"
    assert auth["is_system"] is True
    assert auth["services"]["belle"]["status"] == "loaded"
    assert auth["services"]["worker"]["status"] == "loaded"
    fs = next(item for item in result["items"] if item["name"] == "fs")
    assert fs["services"]["belle"]["status"] == "unknown"
    assert fs["services"]["worker"]["status"] == "loaded"


@pytest.mark.asyncio
async def test_modules_list_hb_expired_unknown(provider: ModopsProvider) -> None:
    redis = FakeRedis()
    registry = ModuleRuntimeRegistry("belle", client=redis)
    registry.publish_all([_snap("auth")])
    redis.strings.pop(f"{HASH_PREFIX}belle:hb")
    provider._modules_registry = registry
    result = await provider.list()
    auth = result["items"][0]
    assert auth["services"]["belle"]["status"] == "unknown"
    assert auth["services"]["belle"]["health"] == "unknown"


@pytest.mark.asyncio
async def test_modules_list_redis_empty_falls_back(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    provider._modules_registry = ModuleRuntimeRegistry("belle", client=redis)
    provider._state.modules = _modules_state()
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    result = await provider.list()
    assert result["degraded"] is True
    names = {item["name"] for item in result["items"]}
    assert "auth" in names
    assert "fs" in names
    assert "sample" not in names
    assert result["items"]


@pytest.mark.asyncio
async def test_modules_list_not_empty_when_modules_exist(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    redis.fail = True
    provider._modules_registry = ModuleRuntimeRegistry("belle", client=redis)
    provider._state.modules = _modules_state()
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    result = await provider.list()
    assert result["items"]
    assert result["degraded"] is True


def test_modules_list_is_cpu_task() -> None:
    assert ModopsProvider.list._task_type == TaskType.CPU
    assert ModopsProvider.list._api_meta["required_permission"] == "modops:read"


def test_module_action_stubs_are_cpu_write() -> None:
    for method in (
        ModopsProvider.unload,
        ModopsProvider.disable,
        ModopsProvider.enable,
        ModopsProvider.delete,
    ):
        assert method._task_type == TaskType.CPU
        assert method._api_meta["required_permission"] == "modops:write"


def test_modules_reload_is_io_write() -> None:
    assert ModopsProvider.reload._task_type == TaskType.IO
    assert ModopsProvider.reload._task_timeout == 30
    assert ModopsProvider.reload._api_meta["required_permission"] == "modops:write"


def test_modules_check_and_update_are_io_write() -> None:
    assert ModopsProvider.check_update._task_type == TaskType.IO
    assert ModopsProvider.check_update._task_timeout == 30
    assert ModopsProvider.check_update._api_meta["required_permission"] == "modops:write"
    assert ModopsProvider.update._task_type == TaskType.IO
    assert ModopsProvider.update._task_timeout == 60
    assert ModopsProvider.update._api_meta["required_permission"] == "modops:write"


@pytest.mark.asyncio
async def test_modules_reload_forbidden_on_system(provider: ModopsProvider) -> None:
    with pytest.raises(ForbiddenError) as exc:
        await provider.reload("system")
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_modules_update_allows_system(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "modules.modops.provider.apply_module_update",
        lambda name: {"name": name, "sha": "abc1234deadbeef", "version": "v1.2.3"},
    )
    result = await provider.update("auth")
    assert result == {"name": "auth", "sha": "abc1234deadbeef", "version": "v1.2.3"}


@pytest.mark.asyncio
async def test_modules_unload_system_forbidden(provider: ModopsProvider) -> None:
    redis = FakeRedis()
    registry = ModuleRuntimeRegistry("belle", client=redis)
    registry.publish_all([_snap("auth")])
    provider._modules_registry = registry
    with pytest.raises(ForbiddenError) as exc:
        await provider.unload("auth")
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_modules_unload_modops_forbidden(provider: ModopsProvider) -> None:
    with pytest.raises(ForbiddenError) as exc:
        await provider.unload("modops")
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_modules_disable_delete_system_forbidden(provider: ModopsProvider) -> None:
    redis = FakeRedis()
    registry = ModuleRuntimeRegistry("belle", client=redis)
    registry.publish_all([_snap("auth")])
    provider._modules_registry = registry
    with pytest.raises(ForbiddenError):
        await provider.disable("auth")
    with pytest.raises(ForbiddenError):
        await provider.delete("auth")


@pytest.mark.asyncio
async def test_modules_unload_non_system_not_implemented(provider: ModopsProvider) -> None:
    redis = FakeRedis()
    registry = ModuleRuntimeRegistry("belle", client=redis)
    snap = _snap("fs")
    snap["is_system"] = False
    registry.publish_all([snap])
    provider._modules_registry = registry
    with pytest.raises(ModopsError) as exc:
        await provider.unload("fs")
    assert exc.value.code == "NOT_IMPLEMENTED"
