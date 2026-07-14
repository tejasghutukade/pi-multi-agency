#!/usr/bin/env python3
"""Orchestrator delegate/reply → Specialist delivery (claim + format)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

DELIVERY_TYPES = {"delegate", "reply"}


def _ensure_dirs(root: Path, name: str) -> tuple[Path, Path]:
    pending = root / "inbox" / name / "pending"
    processing = root / "inbox" / name / "processing"
    pending.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    return pending, processing


def format_specialist_delivery_text(env: dict[str, Any], *, path: str, name: str) -> str:
    typ = env.get("type") or "message"
    frm = env.get("from") or "orchestrator"
    task = env.get("taskId") or "?"
    payload = env.get("payload")
    payload_path = env.get("payloadPath")
    lines = [
        f"[agency:{typ}] from `{frm}` · taskId `{task}`",
        "",
        "A new agency message was delivered by the lifecycle bridge.",
        "Process it now. Report or ask through agency_report / agency_ask.",
        "",
    ]
    if isinstance(payload, dict):
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2)[:12000])
        lines.append("```")
    elif payload is not None:
        lines.append(str(payload)[:12000])
    if payload_path:
        lines.append(f"\npayloadPath: {payload_path}")
    return "\n".join(lines)


def pending_for_specialist(root: Path, *, name: str) -> dict[str, Any]:
    pending, processing = _ensure_dirs(root, name)
    pending_msgs: list[dict[str, Any]] = []
    for p in sorted(pending.glob("*.json")):
        try:
            env = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if env.get("type") not in DELIVERY_TYPES:
            continue
        pending_msgs.append(
            {
                "path": str(p),
                "type": env.get("type"),
                "from": env.get("from"),
                "taskId": env.get("taskId"),
                "createdAt": env.get("createdAt"),
            }
        )

    processing_count = 0
    for p in processing.glob("*.json"):
        try:
            env = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if env.get("type") in DELIVERY_TYPES:
            processing_count += 1

    return {
        "ok": True,
        "count": len(pending_msgs),
        "processingCount": processing_count,
        "messages": pending_msgs,
    }


def claim_for_specialist_delivery(root: Path, *, name: str, task_id: str | None = None) -> dict[str, Any]:
    pending, processing = _ensure_dirs(root, name)

    # Keep at most one claimed specialist envelope in processing at a time.
    # Re-surface it so restarted panes can resume work without a new delegate send.
    for p in sorted(processing.glob("*.json")):
        try:
            env = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if env.get("type") not in DELIVERY_TYPES:
            continue
        if task_id and env.get("taskId") != task_id:
            continue
        return {
            "ok": True,
            "empty": False,
            "blocked": "processing",
            "replay": True,
            "path": str(p),
            "envelope": env,
            "text": format_specialist_delivery_text(env, path=str(p), name=name),
        }

    chosen: Path | None = None
    env: dict[str, Any] | None = None
    for p in sorted(pending.glob("*.json")):
        try:
            peek = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if peek.get("type") not in DELIVERY_TYPES:
            continue
        if task_id and peek.get("taskId") != task_id:
            continue
        chosen = p
        env = peek
        break

    if not chosen or env is None:
        return {"ok": True, "empty": True}

    proc = processing / chosen.name
    chosen.replace(proc)
    from agency_events import emit

    emit(
        "specialist.delivery.claimed",
        root=root,
        instance=name,
        taskId=env.get("taskId"),
        envelopeType=env.get("type"),
        fromName=env.get("from"),
        path=str(proc),
    )

    text = format_specialist_delivery_text(env, path=str(proc), name=name)
    return {
        "ok": True,
        "empty": False,
        "path": str(proc),
        "envelope": env,
        "text": text,
    }
