"""Hot-reload модуля в процессе: unload → purge import → load.

Ядро (log/db/auth/apiproxy/rest/system/worker/modops) не трогаем.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, NoReturn

from argenta_logging import get_logger

__all__ = [
    "CMD_KEY",
    "RELOAD_FORBIDDEN",
    "broadcast_reload",
    "consume_reload_command",
    "purge_import",
    "publish_reload_command",
    "read_reload_command",
    "reload_local",
    "schedule_pool_restart",
    "start_cmd_watcher",
]

log = get_logger(__name__)

CMD_KEY = "mia:modules:cmd"
RELOAD_FORBIDDEN = frozenset({
    "log", "db", "auth", "apiproxy", "rest", "system", "worker", "modops",
})
_WATCH_INTERVAL = 1.0
_POOL_RESTART_DELAY = 1.0
_watcher_lock = threading.Lock()
_watcher_started = False


def purge_import(name: str) -> None:
    """Сбросить пакет modules.{name} и подмодули, чтобы load прочитал диск."""
    prefix = f"modules.{name}"
    for key in [item for item in sys.modules if item == prefix or item.startswith(prefix + ".")]:
        del sys.modules[key]


def reload_local(app: Any, name: str) -> None:
    """unload + purge + load в этом процессе, затем runtime HASH и collect."""
    key = _require_name(name)
    _forbid_core(key)
    app.modules.unload(key)
    purge_import(key)
    app.modules.load(key, state=app)
    publish = getattr(app, "publish_runtime", None)
    if callable(publish):
        publish()
    collect = getattr(app, "_collect_apiproxy", None)
    if callable(collect):
        collect()


def broadcast_reload(
    app: Any,
    name: str,
    *,
    log_: Any | None = None,
    client: Any | None = None,
) -> None:
    """Воркер: локальный reload, команда REST, отложенный pool_restart."""
    reload_local(app, name)
    publish_reload_command(name, client=client)
    schedule_pool_restart(log_)


def publish_reload_command(name: str, client: Any | None = None, *, ts: float | None = None) -> None:
    payload = json.dumps({"op": "reload", "name": name, "ts": ts if ts is not None else time.time()})
    try:
        _client(client).set(CMD_KEY, payload)
    except Exception as exc:
        log.warning("module_cmd_publish_failed", extra={"error": str(exc), "name": name})


def read_reload_command(client: Any | None = None) -> dict[str, Any] | None:
    raw = _client(client).get(CMD_KEY)
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("op") != "reload":
        return None
    return data


def consume_reload_command(
    app: Any,
    last_ts: float,
    client: Any | None = None,
    log_: Any | None = None,
) -> float:
    """Если ts новее — reload_local. Ошибки глотаем, ts команды считаем съеденным."""
    try:
        cmd = read_reload_command(client)
    except Exception as exc:
        _warn(log_, "module_cmd_read_failed", error=str(exc))
        return last_ts
    if cmd is None:
        return last_ts
    ts = float(cmd.get("ts") or 0)
    key = str(cmd.get("name") or "").strip()
    if ts <= last_ts or not key:
        return last_ts
    try:
        reload_local(app, key)
    except Exception as exc:
        _warn(log_, "module_cmd_reload_failed", error=str(exc), name=key)
    return ts


def start_cmd_watcher(app: Any, log_: Any | None = None) -> None:
    """Поток 1s poll. Только belle REST; Redis down — не падаем."""
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return
        _watcher_started = True
    thread = threading.Thread(
        target=_watch_loop, args=(app, log_), name="mia-modules-cmd", daemon=True,
    )
    thread.start()


def schedule_pool_restart(log_: Any | None = None, delay: float = _POOL_RESTART_DELAY) -> None:
    """После ответа RPC — prefork children перечитают код. Нет control → WARN, ok."""
    threading.Timer(delay, lambda: _pool_restart(log_)).start()


def _pool_restart(log_: Any | None) -> None:
    try:
        control = getattr(_celery_app(), "control", None)
        if control is None:
            _warn(log_, "pool_restart_skipped", reason="no control")
            return
        control.broadcast("pool_restart", arguments={"reload": True})
    except Exception as exc:
        _warn(log_, "pool_restart_failed", error=str(exc))


def _celery_app() -> Any:
    from celery import current_app

    return current_app


def _watch_loop(app: Any, log_: Any | None) -> None:
    last_ts = _prime_ts()
    while True:
        last_ts = consume_reload_command(app, last_ts, log_=log_)
        time.sleep(_WATCH_INTERVAL)


def _prime_ts() -> float:
    """Не повторять команду, которая уже лежала в Redis до старта процесса."""
    try:
        cmd = read_reload_command()
    except Exception:
        return 0.0
    if cmd is None:
        return 0.0
    return float(cmd.get("ts") or 0)


def _forbid_core(name: str) -> None:
    if name in RELOAD_FORBIDDEN:
        _raise_forbidden("Cannot reload this module")


def _forbid_dependents(app: Any, name: str) -> None:
    modules = getattr(app, "modules", None)
    if modules is None or not hasattr(modules, "list_all"):
        return
    holders: list[str] = []
    for loaded in modules.list_all():
        if loaded == name:
            continue
        meta = modules.read_meta(loaded) if hasattr(modules, "read_meta") else None
        deps = list(getattr(meta, "dependencies", None) or [])
        if name in deps:
            holders.append(loaded)
    if holders:
        _raise_forbidden("Module has loaded dependents")


def _require_name(name: str) -> str:
    key = str(name or "").strip()
    if not key:
        from .errors import ModopsError

        raise ModopsError("Module name required", "VALIDATION", human="Invalid request")
    return key


def _raise_forbidden(message: str) -> NoReturn:
    from .errors import ForbiddenError

    raise ForbiddenError(message)


def _client(client: Any | None) -> Any:
    if client is not None:
        return client
    from modules_system.runtime_registry import _connect

    return _connect()


def _warn(log_: Any | None, message: str, **extra: Any) -> None:
    target = log_ if log_ is not None else log
    target.warning(message, extra=extra)
