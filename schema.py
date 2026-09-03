"""AUTH_SCHEMA — permissions модуля modops.

Роль не плодим: system_admin *:* покрывает. Регистрация:
AuthSchemaRegistry.register_sync("modops", AUTH_SCHEMA).
"""
from __future__ import annotations

from typing import Any

__all__ = ["AUTH_SCHEMA"]

AUTH_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "permissions": [
        {
            "name": "modops:read",
            "description": "Чтение списка модулей",
        },
        {
            "name": "modops:write",
            "description": "Управление модулями (reload/update/unload)",
        },
    ],
    "roles": [],
}
