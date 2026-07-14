#!/usr/bin/env python3
"""Optional agency event emit — timeline aid; sessions/inbox remain truth."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EmitFn = Callable[..., None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def events_enabled() -> bool:
    return os.environ.get("AGENCY_EVENTS", "").strip() in ("1", "true", "TRUE", "yes", "YES")


def _default_emit(event_type: str, *, root: Path | None = None, **fields: Any) -> None:
    if not events_enabled() or root is None:
        return
    try:
        path = Path(root) / "events.jsonl"
        row = {"ts": utc_now(), "type": event_type, **fields}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        return


_emit: EmitFn = _default_emit


def set_emit(fn: EmitFn | None) -> None:
    """Override emit side-effect (tests). Pass None to restore default."""
    global _emit
    _emit = fn if fn is not None else _default_emit


def emit(event_type: str, *, root: Path | None = None, **fields: Any) -> None:
    try:
        _emit(event_type, root=root, **fields)
    except Exception:
        return


def instance_fingerprint(inst: dict[str, Any]) -> dict[str, Any]:
    return {
        "intercomName": inst.get("intercomName"),
        "status": inst.get("status"),
        "taskId": inst.get("taskId"),
        "cmuxSurface": inst.get("cmuxSurface"),
        "lifecycle": inst.get("lifecycle"),
        "role": inst.get("role"),
    }


def sessions_delta(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    before_map = {
        i.get("intercomName"): instance_fingerprint(i)
        for i in (before or {}).get("instances") or []
        if i.get("intercomName")
    }
    after_map = {
        i.get("intercomName"): instance_fingerprint(i)
        for i in (after.get("instances") or [])
        if i.get("intercomName")
    }
    changes: list[dict[str, Any]] = []
    for name, fp in after_map.items():
        prev = before_map.get(name)
        if prev != fp:
            changes.append({"instance": name, "before": prev, "after": fp})
    for name in before_map:
        if name not in after_map:
            changes.append({"instance": name, "before": before_map[name], "after": None})
    return changes
