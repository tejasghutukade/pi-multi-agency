#!/usr/bin/env python3
"""Pi lifecycle bridge helpers (v0.3) — liveness via agent_* events; task truth via bus."""

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


def hub_inbox_envelopes(root: Path, types: set[str] | None = None) -> list[dict[str, Any]]:
    types = types or {"report", "ask"}
    out: list[dict[str, Any]] = []
    for sub in ("pending", "processing"):
        d = root / "inbox" / HUB / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                env = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if env.get("type") in types:
                out.append({"path": str(p), "stage": sub, "envelope": env})
    return out


def has_hub_message_for_task(root: Path, task_id: str) -> bool:
    if not task_id:
        return False
    for item in hub_inbox_envelopes(root):
        if item["envelope"].get("taskId") == task_id:
            return True
    return False


def find_by_surface(data: dict[str, Any], surface: str | None) -> dict[str, Any] | None:
    if not surface:
        return None
    for i in data.get("instances") or []:
        if i.get("cmuxSurface") == surface:
            return i
    return None


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


def cmd_pending_hub(_args: argparse.Namespace) -> int:
    ctl = import_ctl()
    root = ctl.agency_root()
    items = [x for x in hub_inbox_envelopes(root) if x["stage"] == "pending"]
    print(
        json.dumps(
            {
                "ok": True,
                "count": len(items),
                "messages": [
                    {
                        "path": i["path"],
                        "type": i["envelope"].get("type"),
                        "from": i["envelope"].get("from"),
                        "taskId": i["envelope"].get("taskId"),
                        "createdAt": i["envelope"].get("createdAt"),
                    }
                    for i in items
                ],
            },
            indent=2,
        )
    )
    return 0


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


def cmd_claim_for_delivery(args: argparse.Namespace) -> int:
    """Claim oldest pending hub report/ask → processing and return formatted text."""
    ctl = import_ctl()
    root = ctl.agency_root()
    ensure = root / "inbox" / HUB / "pending"
    ensure.mkdir(parents=True, exist_ok=True)
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
        if args.task_id and peek.get("taskId") != args.task_id:
            continue
        chosen = p
        env = peek
        break
    if not chosen or env is None:
        print(json.dumps({"ok": True, "empty": True}))
        return 0
    processing = root / "inbox" / HUB / "processing" / chosen.name
    chosen.replace(processing)
    text = format_delivery_text(env)
    print(
        json.dumps(
            {
                "ok": True,
                "empty": False,
                "path": str(processing),
                "envelope": env,
                "text": text,
            },
            indent=2,
        )
    )
    return 0


def cmd_ack_delivery(args: argparse.Namespace) -> int:
    ctl = import_ctl()
    root = ctl.agency_root()
    path = Path(args.path)
    if not path.is_file():
        raise RuntimeError(f"missing {path}")
    dest = root / "inbox" / HUB / "done" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    path.replace(dest)
    # clear taskId on source instance if report
    try:
        env = json.loads(dest.read_text())
    except (OSError, json.JSONDecodeError):
        env = {}
    data = ctl.load_sessions(root)
    frm = env.get("from")
    if frm and env.get("type") in ("report", "ask"):
        inst = ctl.find_instance(data, frm)
        if inst and inst.get("taskId") == env.get("taskId"):
            if env.get("type") == "report":
                inst["taskId"] = None
                inst["silentSettleAt"] = None
                inst["nudgeCount"] = 0
                inst["awaitingStartAfterNudge"] = False
                inst["lastDelegate"] = None
            inst["updatedAt"] = utc_now()
            ctl.save_sessions(root, data)
    print(json.dumps({"ok": True, "done": str(dest)}, indent=2))
    return 0


def nudge_instance(ctl: Any, inst: dict[str, Any], task_id: str) -> dict[str, Any]:
    surface = inst.get("cmuxSurface")
    if not surface:
        return {"ok": False, "error": "no cmuxSurface"}
    body = (
        f"Lifecycle bridge: you settled without a bus report/ask for taskId={task_id}. "
        f"Immediately send a report (or ask) to orchestrator via the hybrid bus. "
        f"Do not go idle again without reporting."
    )
    nudge = body.replace("\\", "\\\\").replace("\n", "\\n") + "\\n"
    r = ctl.cmux_run(["send", "--surface", str(surface), nudge])
    return {
        "ok": r.returncode == 0,
        "stdout": (r.stdout or "").strip(),
        "stderr": (r.stderr or "").strip(),
    }


def cmd_tick(args: argparse.Namespace) -> int:
    """Specialist silent-settle tick: grace → one nudge → abandon if no agent_start."""
    ctl = import_ctl()
    root = ctl.agency_root()
    data = ctl.load_sessions(root)
    name = args.name
    if not name:
        surface, _ = ctl.caller_surface()
        inst = find_by_surface(data, surface)
        name = (inst or {}).get("intercomName")
    if not name:
        raise RuntimeError("could not resolve instance")
    inst = ctl.find_instance(data, name)
    if not inst:
        raise RuntimeError(f"no instance {name}")
    if inst.get("role") == HUB or inst.get("intercomName") == HUB:
        print(json.dumps({"ok": True, "action": "tick", "skipped": "hub"}, indent=2))
        return 0

    task_id = inst.get("taskId")
    if not task_id:
        print(json.dumps({"ok": True, "action": "tick", "skipped": "no-task"}, indent=2))
        return 0

    if has_hub_message_for_task(root, task_id):
        inst["silentSettleAt"] = None
        inst["awaitingStartAfterNudge"] = False
        ctl.save_sessions(root, data)
        print(
            json.dumps(
                {"ok": True, "action": "tick", "status": "report-present", "taskId": task_id},
                indent=2,
            )
        )
        return 0

    # only act when process looks idle (settled)
    if inst.get("status") == "working":
        print(json.dumps({"ok": True, "action": "tick", "skipped": "still-working"}, indent=2))
        return 0

    now = utc_now()
    nudge_count = int(inst.get("nudgeCount") or 0)

    if inst.get("awaitingStartAfterNudge"):
        waited = age_sec(inst.get("nudgeAt")) or 0
        if waited >= (args.nudge_wait_sec or NUDGE_START_WAIT_SEC):
            # abandon
            print(
                json.dumps(
                    {
                        "ok": True,
                        "action": "tick",
                        "status": "abandon",
                        "reason": "no-agent_start-after-nudge",
                        "taskId": task_id,
                        "instance": name,
                    },
                    indent=2,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "tick",
                    "status": "awaiting-start-after-nudge",
                    "waitedSec": round(waited, 1),
                },
                indent=2,
            )
        )
        return 0

    silent_at = inst.get("silentSettleAt") or inst.get("lastSettledAt")
    waited = age_sec(silent_at) or 0
    grace = args.grace_sec or SILENT_SETTLE_GRACE_SEC
    if waited < grace:
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "tick",
                    "status": "grace",
                    "waitedSec": round(waited, 1),
                    "graceSec": grace,
                },
                indent=2,
            )
        )
        return 0

    if nudge_count >= 1:
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "tick",
                    "status": "nudge-already-used",
                    "hint": "waiting for report or manual recovery",
                },
                indent=2,
            )
        )
        return 0

    result = nudge_instance(ctl, inst, task_id)
    inst["nudgeCount"] = 1
    inst["nudgeAt"] = now
    inst["awaitingStartAfterNudge"] = True
    inst["updatedAt"] = now
    ctl.save_sessions(root, data)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "tick",
                "status": "nudged",
                "taskId": task_id,
                "nudge": result,
            },
            indent=2,
        )
    )
    return 0


def cmd_abandon(args: argparse.Namespace) -> int:
    """Teardown silent specialist, respawn, re-delegate same taskId; wake hub."""
    ctl = import_ctl()
    root = ctl.agency_root()
    data = ctl.load_sessions(root)
    inst = ctl.find_instance(data, args.name)
    if not inst:
        raise RuntimeError(f"no instance {args.name}")
    if inst.get("role") == HUB:
        raise RuntimeError("refusing to abandon orchestrator")

    last = inst.get("lastDelegate") or {}
    task_id = last.get("taskId") or inst.get("taskId")
    if not task_id:
        raise RuntimeError("no taskId / lastDelegate to re-delegate")
    role = inst.get("role")
    lifecycle = inst.get("lifecycle") or "temporary"
    cwd = inst.get("cwd")
    payload = last.get("payload") or {}
    workflow_id = last.get("workflowId")

    # teardown without orchestrator gate
    surface = inst.get("cmuxSurface")
    closed = None
    if surface and not args.keep_pane:
        r = ctl.cmux_run(["close-surface", "--surface", str(surface)])
        closed = {"ok": r.returncode == 0}

    data["instances"] = [
        i
        for i in (data.get("instances") or [])
        if i.get("intercomName") != inst.get("intercomName")
        and i.get("instanceId") != inst.get("instanceId")
    ]
    ctl.save_sessions(root, data)

    ctl_path = Path(__file__).resolve().parent / "agency_ctl.py"
    env = {
        **os.environ,
        "AGENCY_ROOT": str(root),
        "AGENCY_PROJECT_ROOT": str(ctl.project_root()),
    }
    spawn_args = [
        sys.executable,
        str(ctl_path),
        "spawn",
        "--role",
        str(role),
        "--lifecycle",
        str(lifecycle),
        "--recovery",
    ]
    if cwd:
        spawn_args.extend(["--cwd", str(cwd)])
    spawn = subprocess.run(spawn_args, capture_output=True, text=True, timeout=180, env=env, cwd=str(ctl.project_root()))
    if spawn.returncode != 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "abandon",
                    "error": "respawn failed",
                    "stderr": spawn.stderr,
                    "stdout": spawn.stdout,
                    "closed": closed,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        spawn_out = json.loads(spawn.stdout)
    except json.JSONDecodeError:
        spawn_out = {"raw": spawn.stdout}
    new_name = (spawn_out.get("instance") or {}).get("intercomName")
    if not new_name:
        raise RuntimeError(f"spawn succeeded but no instance name: {spawn_out}")

    del_args = [
        sys.executable,
        str(ctl_path),
        "delegate",
        "--to",
        new_name,
        "--task-id",
        str(task_id),
        "--payload-json",
        json.dumps(payload),
        "--recovery",
    ]
    if workflow_id:
        del_args.extend(["--workflow-id", str(workflow_id)])
    dele = subprocess.run(del_args, capture_output=True, text=True, timeout=60, env=env, cwd=str(ctl.project_root()))
    if dele.returncode != 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "abandon",
                    "error": "re-delegate failed",
                    "stderr": dele.stderr,
                    "stdout": dele.stdout,
                    "spawn": spawn_out,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    # wake hub via bus progress/report system notice
    notice = {
        "summary": f"Specialist `{args.name}` abandoned after silent settle (no agent_start after one nudge). "
        f"Respawned as `{new_name}` and re-delegated taskId `{task_id}`.",
        "abandoned": args.name,
        "replacement": new_name,
        "taskId": task_id,
        "reason": args.reason or "no-agent_start-after-nudge",
    }
    ctl.bus_run(
        root,
        [
            "send",
            "--from",
            new_name,
            "--to",
            HUB,
            "--type",
            "report",
            "--task-id",
            str(task_id),
            "--payload-json",
            json.dumps(notice),
        ],
    )

    print(
        json.dumps(
            {
                "ok": True,
                "action": "abandon",
                "cleared": args.name,
                "replacement": new_name,
                "taskId": task_id,
                "closed": closed,
                "spawn": spawn_out,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="lifecycle_bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="Resolve this cmux surface to a sessions.json instance")

    st = sub.add_parser("status", help="Update instance process status from agent_* events")
    st.add_argument("--name")
    st.add_argument("--status", required=True, choices=["working", "idle", "interrupted", "failed"])

    sub.add_parser("pending-hub", help="List pending hub report/ask envelopes")

    cl = sub.add_parser("claim-delivery", help="Claim one pending hub message for push")
    cl.add_argument("--task-id")

    ack = sub.add_parser("ack-delivery", help="Move claimed message to done/")
    ack.add_argument("--path", required=True)

    tick = sub.add_parser("tick", help="Silent-settle tick (grace / nudge / abandon signal)")
    tick.add_argument("--name")
    tick.add_argument("--grace-sec", type=float)
    tick.add_argument("--nudge-wait-sec", type=float)

    ab = sub.add_parser("abandon", help="Release dead specialist, respawn, re-delegate")
    ab.add_argument("--name", required=True)
    ab.add_argument("--reason")
    ab.add_argument("--keep-pane", action="store_true")

    args = p.parse_args()
    try:
        if args.cmd == "whoami":
            return cmd_whoami(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "pending-hub":
            return cmd_pending_hub(args)
        if args.cmd == "claim-delivery":
            return cmd_claim_for_delivery(args)
        if args.cmd == "ack-delivery":
            return cmd_ack_delivery(args)
        if args.cmd == "tick":
            return cmd_tick(args)
        if args.cmd == "abandon":
            return cmd_abandon(args)
        raise RuntimeError(f"unknown {args.cmd}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
