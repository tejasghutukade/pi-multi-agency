#!/usr/bin/env python3
"""Runtime roster — sessions.json (not static role config)."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

HUB = "orchestrator"


def load_sessions(root: Path) -> dict[str, Any]:
    path = root / "sessions.json"
    if not path.exists():
        return {"version": 1, "instances": []}
    return json.loads(path.read_text())


def save_sessions(root: Path, data: dict[str, Any]) -> None:
    from agency_events import emit, sessions_delta

    before = None
    path = root / "sessions.json"
    if path.exists():
        try:
            before = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            before = None
    path.write_text(json.dumps(data, indent=2) + "\n")
    changes = sessions_delta(before, data)
    if changes:
        emit("sessions.saved", root=root, changes=changes)


def find_instance(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for i in data.get("instances") or []:
        if i.get("intercomName") == name or i.get("instanceId") == name:
            return i
    return None


def find_instance_by_task(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for inst in data.get("instances") or []:
        if inst.get("taskId") == task_id:
            return inst
    return None


def find_by_surface(data: dict[str, Any], surface: str | None) -> dict[str, Any] | None:
    if not surface:
        return None
    for i in data.get("instances") or []:
        if i.get("cmuxSurface") == surface:
            return i
    return None


def find_idle_role(data: dict[str, Any], role: str) -> dict[str, Any] | None:
    for i in data.get("instances") or []:
        if i.get("role") == role and i.get("status") == "idle" and i.get("intercomName") != HUB:
            return i
    return None


def specialist_count(data: dict[str, Any]) -> int:
    return sum(1 for i in (data.get("instances") or []) if i.get("role") != HUB)


def make_instance_name(role: str, lifecycle: str) -> str:
    if lifecycle == "persistent":
        return role
    return f"{role}-t{secrets.token_hex(2)}"


def clear_instance(data: dict[str, Any], inst: dict[str, Any]) -> dict[str, Any]:
    """Remove instance row by intercomName/instanceId. Returns updated data."""
    name = inst.get("intercomName")
    iid = inst.get("instanceId")
    data["instances"] = [
        i
        for i in (data.get("instances") or [])
        if i.get("intercomName") != name and i.get("instanceId") != iid
    ]
    return data
