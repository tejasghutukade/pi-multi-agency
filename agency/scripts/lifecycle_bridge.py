#!/usr/bin/env python3
"""Pi lifecycle bridge helpers (v0.3) — liveness via agent_* events; broker is the sole agency transport."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ledger import find_by_surface, find_instance, load_sessions, save_sessions  # noqa: E402

# draft timers from docs/architecture.md
SILENT_SETTLE_GRACE_SEC = 60
NUDGE_START_WAIT_SEC = 25
HUB_DELIVER_GRACE_SEC = 30

HUB = "orchestrator"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def age_sec(ts: str | None) -> float | None:
    t = parse_iso(ts)
    if t is None:
        return None
    return time.time() - t


def import_ctl():
    """Load sibling agency_ctl as module without running main."""
    import importlib.util

    path = Path(__file__).resolve().parent / "agency_ctl.py"
    spec = importlib.util.spec_from_file_location("agency_ctl_lifecyle", path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load agency_ctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_whoami(_args: argparse.Namespace) -> int:
    ctl = import_ctl()
    root = ctl.agency_root()
    data = ctl.load_sessions(root)
    try:
        surface, pane = ctl.caller_surface()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "instance": None}))
        return 0
    inst = find_by_surface(data, surface)
    print(
        json.dumps(
            {
                "ok": True,
                "cmuxSurface": surface,
                "cmuxPane": pane,
                "instance": inst,
                "isHub": bool(inst and (inst.get("role") == HUB or inst.get("intercomName") == HUB)),
                "isTemporary": bool(inst and inst.get("lifecycle") == "temporary"),
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctl = import_ctl()
    root = ctl.agency_root()
    data = ctl.load_sessions(root)
    name = args.name
    if not name:
        try:
            surface, _ = ctl.caller_surface()
            inst = find_by_surface(data, surface)
            name = (inst or {}).get("intercomName")
        except Exception:
            name = None
    if not name:
        raise RuntimeError("could not resolve instance name (pass --name)")
    inst = ctl.find_instance(data, name)
    if not inst:
        raise RuntimeError(f"no instance {name}")

    now = utc_now()
    status = args.status
    if status not in ("working", "idle", "interrupted", "failed"):
        raise RuntimeError("status must be working|idle|interrupted|failed")

    inst["status"] = status
    inst["updatedAt"] = now
    if status == "working":
        inst["lastAgentStartAt"] = now
        if inst.get("awaitingStartAfterNudge"):
            inst["awaitingStartAfterNudge"] = False
            inst["nudgeRevivedAt"] = now
    elif status == "idle":
        inst["lastSettledAt"] = now
        if inst.get("taskId") and not has_hub_message_for_task(root, inst["taskId"]):
            if not inst.get("silentSettleAt"):
                inst["silentSettleAt"] = now
        else:
            inst["silentSettleAt"] = None
            if has_hub_message_for_task(root, inst.get("taskId") or ""):
                inst["taskCompleteAt"] = now
    elif status == "interrupted":
        inst["lastSettledAt"] = now
        if not inst.get("silentSettleAt"):
            inst["silentSettleAt"] = now

    ctl.save_sessions(root, data)
    print(json.dumps({"ok": True, "action": "lifecycle-status", "instance": inst}, indent=2))
    return 0


def cmd_broker_ack(args: argparse.Namespace) -> int:
    ctl = import_ctl()
    root = ctl.agency_root()
    data = load_sessions(root)
    frm = args.from_name
    typ = args.type
    task_id = args.task_id
    inst = find_instance(data, frm) if frm else None
    if inst and typ == "report" and (not task_id or inst.get("taskId") == task_id):
        inst["taskId"] = None
        inst["silentSettleAt"] = None
        inst["nudgeCount"] = 0
        inst["awaitingStartAfterNudge"] = False
        inst["lastDelegate"] = None
        inst["updatedAt"] = utc_now()
        save_sessions(root, data)
    elif inst and typ == "ask":
        inst["updatedAt"] = utc_now()
        save_sessions(root, data)
    from agency_events import emit

    emit(
        "broker.delivery.acked",
        root=root,
        instance=HUB,
        taskId=task_id,
        envelopeType=typ,
        fromName=frm,
    )
    print(json.dumps({"ok": True, "action": "broker-ack", "from": frm, "type": typ, "taskId": task_id}, indent=2))
    return 0


from recovery import (  # noqa: E402
    TEMP_IDLE_TEARDOWN_SEC,
    cmd_abandon,
    cmd_idle_teardown,
    cmd_tick,
    nudge_instance,
)


def main() -> int:
    p = argparse.ArgumentParser(prog="lifecycle_bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="Resolve this cmux surface to a sessions.json instance")

    st = sub.add_parser("status", help="Update instance process status from agent_* events")
    st.add_argument("--name")
    st.add_argument("--status", required=True, choices=["working", "idle", "interrupted", "failed"])

    ba = sub.add_parser("broker-ack", help="Ack a live broker delivery and update ledger")
    ba.add_argument("--from", dest="from_name", required=True)
    ba.add_argument("--type", required=True, choices=["report", "ask", "progress", "reply", "delegate", "release"])
    ba.add_argument("--task-id")

    tick = sub.add_parser("tick", help="Silent-settle tick (grace / nudge / abandon signal)")
    tick.add_argument("--name")
    tick.add_argument("--grace-sec", type=float)
    tick.add_argument("--nudge-wait-sec", type=float)

    ab = sub.add_parser("abandon", help="Release dead specialist, respawn, re-delegate")
    ab.add_argument("--name", required=True)
    ab.add_argument("--reason")
    ab.add_argument("--keep-pane", action="store_true")

    itd = sub.add_parser(
        "idle-teardown",
        help="Teardown temporary specialist after prolonged idle (no Orchestrator action)",
    )
    itd.add_argument("--name")
    itd.add_argument("--reason")
    itd.add_argument("--idle-sec", type=float, default=TEMP_IDLE_TEARDOWN_SEC)

    args = p.parse_args()
    try:
        if args.cmd == "whoami":
            return cmd_whoami(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "broker-ack":
            return cmd_broker_ack(args)
        if args.cmd == "tick":
            return cmd_tick(args)
        if args.cmd == "abandon":
            return cmd_abandon(args)
        if args.cmd == "idle-teardown":
            return cmd_idle_teardown(args)
        raise RuntimeError(f"unknown {args.cmd}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
