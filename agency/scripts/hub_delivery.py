#!/usr/bin/env python3
"""Specialist report/ask → Orchestrator delivery (claim / format / ack)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agency_paths import agency_root  # noqa: E402
from ledger import find_instance, load_sessions, save_sessions  # noqa: E402

HUB = "orchestrator"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def format_delivery_text(env: dict[str, Any]) -> str:
    typ = env.get("type") or "message"
    frm = env.get("from") or "specialist"
    task = env.get("taskId") or "?"
    payload = env.get("payload")
    path = env.get("payloadPath")
    lines = [
        f"[agency:{typ}] from `{frm}` · taskId `{task}`",
        "",
        "A specialist bus message was delivered by the lifecycle bridge. Act on it (synthesize, reply, or continue the workflow). Do not re-implement the specialist work.",
        "",
    ]
    if isinstance(payload, dict):
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2)[:12000])
        lines.append("```")
    elif payload is not None:
        lines.append(str(payload)[:12000])
    if path:
        lines.append(f"\npayloadPath: {path}")
    return "\n".join(lines)


def claim_for_delivery(root: Path, *, task_id: str | None = None) -> dict[str, Any]:
    """Claim oldest pending hub report/ask → processing. Returns result dict (not printed)."""
    ensure = root / "inbox" / HUB / "pending"
    ensure.mkdir(parents=True, exist_ok=True)
    (root / "inbox" / HUB / "processing").mkdir(parents=True, exist_ok=True)
    pending = sorted(p for p in ensure.glob("*.json") if p.is_file())
    chosen: Path | None = None
    env: dict[str, Any] | None = None
    for p in pending:
        try:
            peek = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if peek.get("type") not in ("report", "ask"):
            continue
        if task_id and peek.get("taskId") != task_id:
            continue
        chosen = p
        env = peek
        break
    if not chosen or env is None:
        return {"ok": True, "empty": True}
    processing = root / "inbox" / HUB / "processing" / chosen.name
    chosen.replace(processing)
    text = format_delivery_text(env)
    return {
        "ok": True,
        "empty": False,
        "path": str(processing),
        "envelope": env,
        "text": text,
    }


def ack_delivery(root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"missing {path}")
    dest = root / "inbox" / HUB / "done" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    path.replace(dest)
    try:
        env = json.loads(dest.read_text())
    except (OSError, json.JSONDecodeError):
        env = {}
    data = load_sessions(root)
    frm = env.get("from")
    if frm and env.get("type") in ("report", "ask"):
        inst = find_instance(data, frm)
        if inst and inst.get("taskId") == env.get("taskId"):
            if env.get("type") == "report":
                inst["taskId"] = None
                inst["silentSettleAt"] = None
                inst["nudgeCount"] = 0
                inst["awaitingStartAfterNudge"] = False
                inst["lastDelegate"] = None
            inst["updatedAt"] = utc_now()
            save_sessions(root, data)
    return {"ok": True, "done": str(dest)}


def cmd_claim_for_delivery(args: argparse.Namespace) -> int:
    result = claim_for_delivery(agency_root(), task_id=getattr(args, "task_id", None))
    print(json.dumps(result, indent=2))
    return 0


def cmd_ack_delivery(args: argparse.Namespace) -> int:
    result = ack_delivery(agency_root(), Path(args.path))
    print(json.dumps(result, indent=2))
    return 0
