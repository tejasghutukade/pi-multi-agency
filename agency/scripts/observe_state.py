#!/usr/bin/env python3
"""Pure projection of agency root → roster + inbox + events (observer API)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = ("pending", "processing", "done")
EMPTY_TIMELINE_COPY = (
    "Enable AGENCY_EVENTS=1 for ephemeral moments; durable state is in roster + inbox."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_sessions_retry(root: Path, *, attempts: int = 3) -> dict[str, Any]:
    path = root / "sessions.json"
    if not path.exists():
        return {"version": 1, "instances": [], "error": None}
    last_err: str | None = None
    for i in range(attempts):
        try:
            return {**json.loads(path.read_text()), "error": None}
        except (OSError, json.JSONDecodeError) as e:
            last_err = str(e)
            time.sleep(0.02 * (i + 1))
    return {"version": 1, "instances": [], "error": last_err}


def _envelope_meta(path: Path) -> dict[str, Any] | None:
    try:
        env = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "file": path.name,
        "path": str(path),
        "type": env.get("type"),
        "from": env.get("from"),
        "to": env.get("to"),
        "taskId": env.get("taskId"),
        "createdAt": env.get("createdAt"),
        "id": env.get("id"),
    }


def scan_inbox(root: Path) -> dict[str, Any]:
    inbox = root / "inbox"
    out: dict[str, Any] = {}
    if not inbox.is_dir():
        return out
    for inst_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        name = inst_dir.name
        stages: dict[str, Any] = {}
        for stage in STAGES:
            d = inst_dir / stage
            msgs: list[dict[str, Any]] = []
            if d.is_dir():
                for p in sorted(d.glob("*.json")):
                    meta = _envelope_meta(p)
                    if meta:
                        msgs.append(meta)
            stages[stage] = {"count": len(msgs), "messages": msgs}
        out[name] = stages
    return out


def tail_events(root: Path, *, limit: int = 200) -> dict[str, Any]:
    path = root / "events.jsonl"
    if not path.exists():
        return {"enabledFile": False, "events": [], "emptyCopy": EMPTY_TIMELINE_COPY}
    lines = path.read_text().splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"enabledFile": True, "events": events, "emptyCopy": None if events else EMPTY_TIMELINE_COPY}


def snapshot(root: Path) -> dict[str, Any]:
    root = Path(root)
    sessions = load_sessions_retry(root)
    instances = sessions.get("instances") or []
    hub = next(
        (
            i
            for i in instances
            if i.get("role") == "orchestrator" or i.get("intercomName") == "orchestrator"
        ),
        None,
    )
    claim = {
        "bound": bool(hub and hub.get("cmuxSurface")),
        "surface": (hub or {}).get("cmuxSurface"),
        "pane": (hub or {}).get("cmuxPane"),
    }
    return {
        "ok": True,
        "agencyRoot": str(root),
        "lastSnapshotAt": utc_now(),
        "sessionsError": sessions.get("error"),
        "claim": claim,
        "instances": instances,
        "inbox": scan_inbox(root),
        "timeline": tail_events(root),
    }
