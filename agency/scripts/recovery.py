#!/usr/bin/env python3
"""Recovery — silent-settle nudge, abandon/respawn, temp idle teardown."""

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

from cmux_pane import close_surface, send_to_surface  # noqa: E402
from ledger import clear_instance, find_by_surface, find_instance, load_sessions, save_sessions  # noqa: E402

SILENT_SETTLE_GRACE_SEC = 60
NUDGE_START_WAIT_SEC = 25
TEMP_IDLE_TEARDOWN_SEC = 300
HUB = "orchestrator"

_clock = time.time


def set_clock(fn) -> None:
    """Override time source for tests. Pass None to restore."""
    global _clock
    _clock = fn if fn is not None else time.time


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
    return _clock() - t


def import_ctl():
    import importlib.util
    path = Path(__file__).resolve().parent / "agency_ctl.py"
    spec = importlib.util.spec_from_file_location("agency_ctl_recovery", path)
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


def teardown_instance(ctl: Any, root: Path, inst: dict[str, Any], *, keep_pane: bool = False) -> dict[str, Any] | None:
    """Close surface (unless keep_pane) and clear ledger row."""
    surface = inst.get("cmuxSurface")
    closed = None
    if surface and not keep_pane:
        r = close_surface(str(surface))
        closed = {"ok": r.returncode == 0}
    data = load_sessions(root)
    clear_instance(data, inst)
    save_sessions(root, data)
    return closed

def cmd_idle_teardown(args: argparse.Namespace) -> int:
    """Close a temporary specialist after prolonged idle (no hub involvement)."""
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
        raise RuntimeError("refusing to idle-teardown orchestrator")
    if inst.get("lifecycle") != "temporary":
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "idle-teardown",
                    "skipped": "not-temporary",
                    "instance": name,
                    "lifecycle": inst.get("lifecycle"),
                },
                indent=2,
            )
        )
        return 0
    if inst.get("status") == "working":
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "idle-teardown",
                    "skipped": "still-working",
                    "instance": name,
                },
                indent=2,
            )
        )
        return 0

    closed = teardown_instance(ctl, root, inst, keep_pane=False)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "idle-teardown",
                "reason": args.reason or "temp-idle-timeout",
                "idleSec": args.idle_sec or TEMP_IDLE_TEARDOWN_SEC,
                "instance": name,
                "closed": closed,
            },
            indent=2,
        )
    )
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
    try:
        r = send_to_surface(str(surface), body, enter=True)
        return {
            "ok": r.returncode == 0,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    closed = teardown_instance(ctl, root, inst, keep_pane=bool(args.keep_pane))

    from agent_spawn import spawn_specialist

    try:
        spawn_out = spawn_specialist(
            str(role),
            lifecycle=str(lifecycle),
            cwd=str(cwd) if cwd else None,
            recovery=True,
            boot_wait=0,
            nudge=False,
        )
    except Exception as e:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "abandon",
                    "error": "respawn failed",
                    "stderr": str(e),
                    "stdout": "",
                    "closed": closed,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    new_name = (spawn_out.get("instance") or {}).get("intercomName")
    if not new_name:
        raise RuntimeError(f"spawn succeeded but no instance name: {spawn_out}")

    ctl_path = Path(__file__).resolve().parent / "agency_ctl.py"
    env = {
        **os.environ,
        "AGENCY_ROOT": str(root),
        "AGENCY_PROJECT_ROOT": str(ctl.project_root()),
    }
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



