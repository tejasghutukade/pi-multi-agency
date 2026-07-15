#!/usr/bin/env python3
"""Multi-Agency Option C control plane — spawn / list / delegate / release."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agency_paths import (  # noqa: E402
    agency_root,
    kit_root,
    package_root,
    project_root,
    resolve_resource,
    scripts_dir,
)
from catalog import (  # noqa: E402
    agent_file_for,
    load_agents,
    parse_agent_frontmatter,
    role_of,
)
from catalog import HUB as CATALOG_HUB  # noqa: E402
from cmux_pane import (  # noqa: E402
    caller_surface,
    close_surface,
    cmux_json,
    cmux_run,
    identify,
    surface_alive,
)
from ledger import (  # noqa: E402
    clear_instance,
    find_by_surface,
    find_idle_role,
    find_instance,
    find_instance_by_task,
    load_sessions,
    make_instance_name,
    save_sessions,
    specialist_count,
)
from pi_launch import build_pi_command  # noqa: E402
import pipeline_state  # noqa: E402

HUB = "orchestrator"
assert CATALOG_HUB == HUB
STARTING_TIMEOUT_SEC = 90
ACTIVE_PIPELINE_RUNNER_STATUSES = frozenset({"idle", "working"})
# Hub process allowlist: read/search + agency_* — no edit/write/bash (see docs/architecture.md).
HUB_TOOLS = (
    "read,grep,find,ls,"
    "agency_init,agency_list,agency_spawn,agency_delegate,agency_release"
)


def lifecycle_py() -> Path:
    return scripts_dir() / "lifecycle_bridge.py"


def lifecycle_run(args: list[str], timeout: float = 120) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGENCY_ROOT"] = str(agency_root())
    env["AGENCY_PROJECT_ROOT"] = str(project_root())
    r = subprocess.run(
        [sys.executable, str(lifecycle_py()), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(project_root()),
    )
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        err = (r.stderr or "").strip() or out or "lifecycle_bridge failed"
        raise RuntimeError(err)
    return json.loads(out) if out else {"ok": True}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bus_py(_root: Path | None = None) -> Path:
    return scripts_dir() / "bus.py"


def memory_py() -> Path:
    return scripts_dir() / "memory.py"


def bus_run(root: Path, args: list[str], timeout: float = 60) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGENCY_ROOT"] = str(root)
    env["AGENCY_PROJECT_ROOT"] = str(project_root())
    r = subprocess.run(
        [sys.executable, str(bus_py()), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(project_root()),
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"bus exit {r.returncode}")
    return json.loads(r.stdout)


def cmd_wait(args: argparse.Namespace) -> int:
    root = agency_root()
    pipeline_id = getattr(args, "pipeline_id", None)
    require_operation_authority(root, pipeline_id=pipeline_id)
    expected_sender = None
    if pipeline_id is not None:
        if args.as_name != HUB:
            raise RuntimeError("pipeline wait denied: pipeline runner may wait only as orchestrator")
        ownership = require_active_dispatched_stage(root, pipeline_id, args.task_id)
        expected_sender = ownership["expectedSender"]
    data = load_sessions(root)
    inst = find_instance_by_task(data, args.task_id)

    if inst:
        alive = surface_alive(inst.get("cmuxSurface"))
        if alive is False:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "pane_dead",
                        "taskId": args.task_id,
                        "instance": inst.get("intercomName"),
                        "cmuxSurface": inst.get("cmuxSurface"),
                        "hint": "agency_list reconcile → release → spawn + delegate again",
                    },
                    indent=2,
                )
            )
            return 0

    bus_timeout = max(30.0, float(args.timeout) + 15.0)
    bus_args = [
        "wait",
        "--as",
        args.as_name or HUB,
        "--task-id",
        args.task_id,
        "--timeout",
        str(args.timeout),
        "--interval",
        str(args.interval),
    ]
    if expected_sender is not None:
        bus_args.extend(["--from", expected_sender])
    if args.auto_done_progress is False:
        bus_args.append("--no-auto-done-progress")
    elif args.auto_done_progress is True:
        bus_args.append("--auto-done-progress")

    result = bus_run(root, bus_args, timeout=bus_timeout)

    if result.get("status") == "timeout" and inst:
        alive = surface_alive(inst.get("cmuxSurface"))
        if alive is False:
            result = {
                "ok": True,
                "status": "pane_dead",
                "taskId": args.task_id,
                "instance": inst.get("intercomName"),
                "cmuxSurface": inst.get("cmuxSurface"),
                "hint": "agency_list reconcile → release → spawn + delegate again",
                "prior": result,
            }

    print(json.dumps({**result, "action": "wait"}, indent=2))
    return 0


def reconcile_force(root: Path, names: list[str]) -> dict[str, Any]:
    script = scripts_dir() / "reconcile-sessions.py"
    r = subprocess.run(
        [sys.executable, str(script), "--force-stale", *names],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "AGENCY_ROOT": str(root)},
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return json.loads(r.stdout)


def reconcile_cmux(root: Path) -> dict[str, Any]:
    script = scripts_dir() / "reconcile-sessions.py"
    r = subprocess.run(
        [sys.executable, str(script), "--check-cmux"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "AGENCY_ROOT": str(root)},
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return json.loads(r.stdout)


def ensure_orchestrator(root: Path) -> dict[str, Any]:
    data = load_sessions(root)
    instances = list(data.get("instances") or [])
    surface, pane = caller_surface()
    orch = next((i for i in instances if i.get("role") == HUB or i.get("intercomName") == HUB), None)
    if orch:
        if orch.get("cmuxSurface") and orch["cmuxSurface"] != surface:
            raise RuntimeError(
                f"spawn/release denied: orchestrator is bound to {orch.get('cmuxSurface')}, "
                f"caller is {surface}"
            )
        orch["cmuxSurface"] = surface
        orch["cmuxPane"] = pane
        orch["status"] = orch.get("status") or "idle"
        orch["updatedAt"] = utc_now()
        save_sessions(root, data)
        return orch

    row = {
        "instanceId": f"orchestrator-{secrets.token_hex(4)}",
        "role": HUB,
        "intercomName": HUB,
        "lifecycle": "persistent",
        "status": "idle",
        "cwd": str(project_root()),
        "taskId": None,
        "cmuxSurface": surface,
        "cmuxPane": pane,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
    }
    instances.append(row)
    data["instances"] = instances
    save_sessions(root, data)
    bus_run(root, ["init", HUB])
    return row


def require_orchestrator(root: Path, *, recovery: bool = False) -> dict[str, Any] | None:
    if recovery:
        data = load_sessions(root)
        return next(
            (i for i in (data.get("instances") or []) if i.get("role") == HUB or i.get("intercomName") == HUB),
            None,
        )
    return ensure_orchestrator(root)


def process_alive(pid: int) -> bool:
    """Return whether a positive process ID is alive; fail closed on probe errors."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def require_pipeline_runner_authority(root: Path, pipeline_id: str) -> dict[str, Any]:
    """Authenticate the caller against session, pipeline binding, and lock state."""
    if not isinstance(pipeline_id, str) or not pipeline_id:
        raise RuntimeError("pipeline authority denied: pipeline ID is required")

    surface, _pane = caller_surface()
    data = load_sessions(root)
    runner = find_by_surface(data, surface)
    if runner is None:
        raise RuntimeError(f"pipeline authority denied: unregistered caller surface {surface!r}")
    surface_rows = [
        row for row in (data.get("instances") or []) if row.get("cmuxSurface") == surface
    ]
    if len(surface_rows) != 1:
        raise RuntimeError(
            f"pipeline authority denied: ambiguous caller surface {surface!r} "
            f"has {len(surface_rows)} session rows"
        )
    if runner.get("role") != "pipeline-runner":
        raise RuntimeError(
            "pipeline authority denied: caller role must be pipeline-runner, "
            f"got {runner.get('role')!r}"
        )
    if runner.get("activePipelineId") != pipeline_id:
        raise RuntimeError(
            "pipeline authority denied: session pipeline mismatch: "
            f"expected {pipeline_id!r}, got {runner.get('activePipelineId')!r}"
        )
    if runner.get("status") not in ACTIVE_PIPELINE_RUNNER_STATUSES:
        raise RuntimeError(
            "pipeline authority denied: inactive runner status "
            f"{runner.get('status')!r}"
        )

    runner_name = runner.get("intercomName")
    if not isinstance(runner_name, str) or not runner_name:
        raise RuntimeError("pipeline authority denied: runner intercom name is missing")

    binding = pipeline_state.get_active_runner_binding(root)
    if binding is None:
        raise RuntimeError("pipeline authority denied: active runner binding is missing")
    if binding.get("pipelineId") != pipeline_id:
        raise RuntimeError(
            "pipeline authority denied: binding pipeline mismatch: "
            f"expected {pipeline_id!r}, got {binding.get('pipelineId')!r}"
        )
    if binding.get("runnerInstance") != runner_name:
        raise RuntimeError(
            "pipeline authority denied: binding instance mismatch: "
            f"expected {runner_name!r}, got {binding.get('runnerInstance')!r}"
        )
    if binding.get("runnerSurface") != surface:
        raise RuntimeError(
            "pipeline authority denied: binding surface mismatch: "
            f"expected {surface!r}, got {binding.get('runnerSurface')!r}"
        )

    lock = pipeline_state.read_lock(root)
    if lock is None:
        raise RuntimeError("pipeline authority denied: pipeline lock is missing")
    if lock.get("pipelineId") != pipeline_id:
        raise RuntimeError(
            "pipeline authority denied: lock pipeline mismatch: "
            f"expected {pipeline_id!r}, got {lock.get('pipelineId')!r}"
        )
    if lock.get("ownerId") != runner_name:
        raise RuntimeError(
            "pipeline authority denied: lock owner mismatch: "
            f"expected {runner_name!r}, got {lock.get('ownerId')!r}"
        )
    if lock.get("ownerSurface") != surface:
        raise RuntimeError(
            "pipeline authority denied: lock surface mismatch: "
            f"expected {surface!r}, got {lock.get('ownerSurface')!r}"
        )
    owner_pid = lock.get("ownerPid")
    if not process_alive(owner_pid):
        raise RuntimeError(
            f"pipeline authority denied: lock owner PID is not confirmed alive: {owner_pid!r}"
        )
    if surface_alive(surface) is not True:
        raise RuntimeError(
            f"pipeline authority denied: runner surface is not confirmed alive: {surface!r}"
        )
    return runner


def require_active_pending_stage_role(
    root: Path,
    pipeline_id: str,
    role: str,
) -> dict[str, Any]:
    """Require spawn to target the current pending stage's configured role."""
    run = pipeline_state.get_active_run(root)
    if run is None or run.get("pipelineId") != pipeline_id:
        raise RuntimeError(f"pipeline spawn denied: pipeline {pipeline_id!r} is not active")
    current_stage_id = run.get("currentStageId")
    stage = next(
        (item for item in (run.get("stages") or []) if item.get("id") == current_stage_id),
        None,
    )
    if stage is None:
        raise RuntimeError("pipeline spawn denied: active pipeline has no current stage")
    if stage.get("status") != "pending":
        raise RuntimeError(
            "pipeline spawn denied: current stage is not pending: "
            f"{stage.get('status')!r}"
        )
    if stage.get("role") != role:
        raise RuntimeError(
            "pipeline spawn denied: role does not match current stage: "
            f"expected {stage.get('role')!r}, got {role!r}"
        )
    return stage


def require_active_dispatched_stage(
    root: Path,
    pipeline_id: str,
    task_id: str,
    *,
    expected_sender: str | None = None,
) -> dict[str, Any]:
    """Require exact ownership by the current dispatched stage."""
    ownership = pipeline_state.find_task_ownership(root, task_id, active_only=True)
    if ownership is None:
        raise RuntimeError(f"pipeline operation denied: task {task_id!r} is not active pipeline-owned")
    if ownership.get("pipelineId") != pipeline_id:
        raise RuntimeError("pipeline operation denied: task pipeline mismatch")
    if ownership.get("taskKind") != "stage":
        raise RuntimeError("pipeline operation denied: task is not a stage task")
    run = pipeline_state.get_active_run(root)
    if run is None or run.get("pipelineId") != pipeline_id:
        raise RuntimeError(f"pipeline operation denied: pipeline {pipeline_id!r} is not active")
    if ownership.get("stageId") != run.get("currentStageId"):
        raise RuntimeError("pipeline operation denied: task does not belong to the current stage")
    if ownership.get("stageStatus") != "dispatched":
        raise RuntimeError("pipeline operation denied: current stage is not dispatched")
    sender = ownership.get("expectedSender")
    if not isinstance(sender, str) or not sender:
        raise RuntimeError("pipeline operation denied: dispatched stage has no expected sender")
    if expected_sender is not None and sender != expected_sender:
        raise RuntimeError(
            "pipeline operation denied: target does not match dispatched stage sender: "
            f"expected {sender!r}, got {expected_sender!r}"
        )
    return ownership


def require_operation_authority(
    root: Path,
    *,
    pipeline_id: str | None = None,
    recovery: bool = False,
) -> dict[str, Any] | None:
    """Select authenticated pipeline authority or preserve orchestrator policy."""
    if pipeline_id is not None:
        return require_pipeline_runner_authority(root, pipeline_id)
    return require_orchestrator(root, recovery=recovery)


def cmd_list(_args: argparse.Namespace) -> int:
    root = agency_root()
    try:
        recon = reconcile_cmux(root)
    except Exception as e:
        recon = {"ok": False, "error": str(e)}
    data = load_sessions(root)
    print(
        json.dumps(
            {
                "ok": True,
                "reconcile": recon,
                "instances": data.get("instances") or [],
                "specialistCount": specialist_count(data),
            },
            indent=2,
        )
    )
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    from agency_observe import main as observe_main

    argv: list[str] = []
    if args.root:
        argv.extend(["--root", args.root])
    argv.extend(["--host", args.host, "--port", str(args.port)])
    if args.snapshot:
        argv.append("--snapshot")
    return observe_main(argv)


def cmd_spawn(args: argparse.Namespace) -> int:
    from agent_spawn import spawn_specialist

    result = spawn_specialist(
        args.role,
        lifecycle=args.lifecycle,
        name=args.name,
        direction=args.direction or "right",
        reuse=bool(args.reuse),
        dry_run=bool(args.dry_run),
        boot_wait=float(args.boot_wait),
        cwd=args.cwd,
        nudge=False,
        recovery=bool(getattr(args, "recovery", False)),
        pipeline_id=getattr(args, "pipeline_id", None),
        message=getattr(args, "message", None),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_delegate(args: argparse.Namespace) -> int:
    root = agency_root()
    pipeline_id = getattr(args, "pipeline_id", None)
    require_operation_authority(
        root,
        pipeline_id=pipeline_id,
        recovery=bool(getattr(args, "recovery", False)),
    )
    if pipeline_id is not None:
        require_active_dispatched_stage(
            root,
            pipeline_id,
            args.task_id,
            expected_sender=args.to,
        )
    data = load_sessions(root)
    inst = find_instance(data, args.to)
    if not inst:
        raise RuntimeError(f"no session row for {args.to} — spawn or reuse first")
    if inst.get("status") == "starting":
        raise RuntimeError(f"{args.to} still starting — wait or mark failed")

    agents = load_agents(root)
    agent = (agents.get("agents") or {}).get(inst.get("role") or role_of(args.to)) or {}
    payload: dict[str, Any]
    if args.payload_json:
        payload = json.loads(args.payload_json)
    else:
        payload = {
            "goal": args.goal,
            "contextPaths": json.loads(args.context_paths) if args.context_paths else [],
            "successCriteria": args.success_criteria,
            "constraints": args.constraints,
            "charterPath": args.charter_path or agent.get("charterPath"),
            "skillPath": args.skill_path if args.skill_path is not None else agent.get("skillPath"),
            "outputShape": args.output_shape,
            "stopRules": args.stop_rules,
            "memoryPath": str(agency_root() / "memory" / args.to / "NOTES.md"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}

    if getattr(args, "prepare_only", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "delegate-preflight",
                    "to": args.to,
                    "taskId": args.task_id,
                    "payload": payload,
                    "instance": inst,
                },
                indent=2,
            )
        )
        return 0

    now = utc_now()
    # Process status is lifecycle-owned (agent_start/agent_settled).
    # Delegate mutates task routing metadata only.
    inst["taskId"] = args.task_id
    inst["nudgeCount"] = 0
    inst["silentSettleAt"] = now
    inst["awaitingStartAfterNudge"] = False
    inst["lastDelegate"] = {
        "taskId": args.task_id,
        "workflowId": args.workflow_id,
        "payload": payload,
        "to": args.to,
        "at": now,
    }
    inst["updatedAt"] = now
    save_sessions(root, data)

    bus_args = [
        "send",
        "--from",
        HUB,
        "--to",
        args.to,
        "--type",
        "delegate",
        "--task-id",
        args.task_id,
        "--payload-json",
        json.dumps(payload),
    ]
    if args.workflow_id:
        bus_args.extend(["--workflow-id", args.workflow_id])
    if getattr(args, "no_bus", False):
        result = {
            "ok": True,
            "transport": "broker",
            "delivered": True,
            "notified": False,
        }
    else:
        result = bus_run(root, bus_args)
        result["transport"] = result.get("transport") or "file"

    print(
        json.dumps(
            {
                "ok": True,
                "action": "delegate",
                "to": args.to,
                "taskId": args.task_id,
                "bus": result,
                "instance": inst,
            },
            indent=2,
        )
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold project-local .pi/agency + .pi/agents from the package kit."""
    proj = Path(args.project or Path.cwd()).resolve()
    agency = proj / ".pi" / "agency"
    agents_dir = proj / ".pi" / "agents"
    kit = kit_root()
    pkg = package_root()

    agency.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    def copy_file(src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(str(dest.relative_to(proj)))

    def copy_tree(src: Path, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        copied.append(str(dest.relative_to(proj)) + "/")

    # Always refresh persona files from the package kit so template fixes
    # (e.g. added agency_report/agency_ask/agency_progress tools) propagate
    # to existing projects without a manual --force re-init. State
    # (sessions.json) and config (agents.yaml) are never overwritten here.
    for md in (pkg / "agents").glob("*.md"):
        copy_file(md, agents_dir / md.name)

    if (
        (agency / "agents.yaml").exists()
        and (agency / "sessions.json").exists()
        and not args.force
    ):
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "init",
                    "skipped": True,
                    "reason": "already initialized (persona files refreshed from kit; pass --force to refresh templates/config)",
                    "agencyRoot": str(agency),
                    "projectRoot": str(proj),
                    "packageRoot": str(pkg),
                    "refreshed": copied,
                },
                indent=2,
            )
        )
        return 0

    copy_file(kit / "agents.yaml", agency / "agents.yaml")
    copy_file(kit / "pipelines.yaml", agency / "pipelines.yaml")
    copy_file(kit / "memory-spec.md", agency / "memory-spec.md")
    copy_tree(kit / "charters", agency / "charters")

    sessions = agency / "sessions.json"
    if not sessions.exists() or args.force:
        copy_file(kit / "templates" / "sessions.json", sessions)

    print(
        json.dumps(
            {
                "ok": True,
                "action": "init",
                "projectRoot": str(proj),
                "agencyRoot": str(agency),
                "packageRoot": str(pkg),
                "copied": copied,
                "next": [
                    "Open this project inside cmux",
                    hub_start_command(proj),
                    "/agency-claim → /agency-broker-status → agency_list",
                ],
                "hubTools": HUB_TOOLS,
            },
            indent=2,
        )
    )
    return 0


def hub_start_command(proj: Path | None = None) -> str:
    """Canonical Orchestrator hub launch with project ownership set before Pi starts."""
    root = (proj or project_root()).resolve()
    persona = root / ".pi" / "agents" / "orchestrator.md"
    if not persona.is_file():
        persona = package_root() / "agents" / "orchestrator.md"
    return build_pi_command(
        work=str(root),
        instance_name=HUB,
        tools=HUB_TOOLS,
        agent_path=persona,
        agency_root=str(root / ".pi" / "agency"),
        agency_project_root=str(root),
    )


def cmd_hub_start(args: argparse.Namespace) -> int:
    proj = Path(args.project or Path.cwd()).resolve()
    cmd = hub_start_command(proj)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "hub-start",
                "projectRoot": str(proj),
                "command": cmd,
                "hubTools": HUB_TOOLS,
                "notes": [
                    "Run inside cmux from the project root",
                    "Requires /agency-init once so .pi/agents/orchestrator.md exists",
                    "Hub must not have edit/write/bash — specialists implement",
                    "Then restart the full agency cohort → /agency-claim → /agency-broker-status → agency_list",
                ],
            },
            indent=2,
        )
    )
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    root = agency_root()
    require_orchestrator(root, recovery=bool(getattr(args, "recovery", False)))
    data = load_sessions(root)
    inst = find_instance(data, args.name)
    if not inst:
        raise RuntimeError(f"no instance named {args.name}")
    if inst.get("role") == HUB and not args.force:
        raise RuntimeError("refusing to release orchestrator without --force")

    mode = args.mode
    if mode == "auto":
        mode = "teardown" if inst.get("lifecycle") == "temporary" else "idle"

    if mode == "idle":
        inst["status"] = "idle"
        inst["taskId"] = None
        inst["updatedAt"] = utc_now()
        save_sessions(root, data)
        print(json.dumps({"ok": True, "action": "idle", "instance": inst}, indent=2))
        return 0

    surface = inst.get("cmuxSurface")
    closed = None
    if surface and not args.keep_pane:
        r = close_surface(str(surface))
        closed = {"ok": r.returncode == 0, "stdout": (r.stdout or "").strip(), "stderr": (r.stderr or "").strip()}

    clear_instance(data, inst)
    save_sessions(root, data)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "teardown",
                "cleared": inst.get("intercomName"),
                "closed": closed,
            },
            indent=2,
        )
    )
    return 0


def cmd_claim_orchestrator(_args: argparse.Namespace) -> int:
    root = agency_root()
    row = ensure_orchestrator(root)
    print(json.dumps({"ok": True, "action": "claim-orchestrator", "instance": row}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="agency_ctl", description="Multi-Agency Option C control plane")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List sessions (reconcile cmux first)")

    sp = sub.add_parser("spawn", help="Spawn or reuse a specialist pane")
    sp.add_argument("--role", required=True)
    sp.add_argument("--lifecycle", choices=["temporary", "persistent"])
    sp.add_argument("--name")
    sp.add_argument("--direction", default="right", choices=["left", "right", "up", "down"])
    sp.add_argument("--reuse", action="store_true", help="Reuse idle instance of role if present")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--boot-wait", type=float, default=5.0)
    sp.add_argument("--cwd", help="Pane working directory (Scout reference-repo mode)")
    sp.add_argument("--pipeline-id", help="Select bound pipeline-runner authority")
    sp.add_argument(
        "--message",
        "-m",
        help="Custom first-turn boot message",
    )
    sp.add_argument("--nudge", action=argparse.BooleanOptionalAction, default=False, help=argparse.SUPPRESS)
    sp.add_argument(
        "--recovery",
        action="store_true",
        help="Skip orchestrator surface gate (lifecycle abandon/respawn)",
    )

    d = sub.add_parser("delegate", help="Send bus delegate envelope")
    d.add_argument("--to", required=True)
    d.add_argument("--task-id", required=True)
    d.add_argument("--workflow-id")
    d.add_argument("--pipeline-id", help="Select bound pipeline-runner authority")
    d.add_argument("--goal")
    d.add_argument("--context-paths", help="JSON array of paths")
    d.add_argument("--success-criteria")
    d.add_argument("--constraints")
    d.add_argument("--charter-path")
    d.add_argument("--skill-path")
    d.add_argument("--output-shape")
    d.add_argument("--stop-rules")
    d.add_argument("--payload-json")
    d.add_argument("--no-bus", action="store_true", help="Commit delegate metadata only; live broker already delivered")
    d.add_argument("--prepare-only", action="store_true", help="Validate target and return resolved payload without mutating ledger or bus")
    d.add_argument(
        "--recovery",
        action="store_true",
        help="Skip orchestrator surface gate (lifecycle abandon/respawn)",
    )

    w = sub.add_parser("wait", help="Wait for hub inbox report/ask for a taskId (legacy)")
    w.add_argument("--task-id", required=True)
    w.add_argument("--pipeline-id", help="Select bound pipeline-runner authority")
    w.add_argument("--timeout", type=float, default=120.0)
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument("--as", dest="as_name", default=HUB)
    w.add_argument(
        "--auto-done-progress",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    r = sub.add_parser("release", help="Mark idle or tear down instance")
    r.add_argument("--name", required=True)
    r.add_argument("--mode", choices=["auto", "idle", "teardown"], default="auto")
    r.add_argument("--keep-pane", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--recovery", action="store_true", help="Skip orchestrator surface gate")

    sub.add_parser("claim-orchestrator", help="Bind this cmux surface as orchestrator")

    ini = sub.add_parser("init", help="Scaffold .pi/agency + .pi/agents in a project from this package")
    ini.add_argument("--project", help="Project root (default: cwd)")
    ini.add_argument("--force", action="store_true", help="Refresh templates even if already initialized")

    hs = sub.add_parser(
        "hub-start",
        help="Print the canonical Orchestrator hub pi command (tools lock + persona)",
    )
    hs.add_argument("--project", help="Project root (default: cwd)")

    obs = sub.add_parser("observe", help="Local ops observer UI (roster / bus / timeline)")
    obs.add_argument("--root", help="Agency root (default AGENCY_ROOT)")
    obs.add_argument("--host", default="127.0.0.1")
    obs.add_argument("--port", type=int, default=8765)
    obs.add_argument("--snapshot", action="store_true", help="Print one JSON snapshot and exit")

    lc = sub.add_parser("lifecycle", help="Pi lifecycle bridge (status / tick / delivery / abandon)")
    lc.add_argument(
        "lifecycle_args",
        nargs=argparse.REMAINDER,
        help="Args forwarded to lifecycle_bridge.py (e.g. whoami, status --status working)",
    )

    args = p.parse_args()
    try:
        if args.cmd == "init":
            return cmd_init(args)
        if args.cmd == "hub-start":
            return cmd_hub_start(args)
        if args.cmd == "observe":
            return cmd_observe(args)
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "spawn":
            return cmd_spawn(args)
        if args.cmd == "delegate":
            return cmd_delegate(args)
        if args.cmd == "wait":
            return cmd_wait(args)
        if args.cmd == "release":
            return cmd_release(args)
        if args.cmd == "claim-orchestrator":
            return cmd_claim_orchestrator(args)
        if args.cmd == "lifecycle":
            fwd = list(args.lifecycle_args or [])
            if fwd and fwd[0] == "--":
                fwd = fwd[1:]
            if not fwd:
                raise RuntimeError("lifecycle requires a subcommand (whoami|status|tick|…)")
            out = lifecycle_run(fwd, timeout=180)
            print(json.dumps(out, indent=2))
            return 0
        raise RuntimeError(f"unknown cmd {args.cmd}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
