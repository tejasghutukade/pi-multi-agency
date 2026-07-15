#!/usr/bin/env python3
"""Compose an agency specialist spawn over catalog / ledger / pi_launch."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agency_paths import agency_root, project_root, resolve_resource, scripts_dir  # noqa: E402
from catalog import (  # noqa: E402
    HUB,
    agent_file_for,
    load_agents,
    load_pipelines,
    max_specialist_panes,
    parse_agent_frontmatter,
    role_defaults,
)
from cmux_pane import open_pane, send_to_surface  # noqa: E402
from ledger import (  # noqa: E402
    find_idle_role,
    find_instance,
    load_sessions,
    make_instance_name,
    save_sessions,
    specialist_count,
)
from pipeline_state import acquire_lock, create_run, load_state  # noqa: E402
from pi_launch import build_pi_command, write_boot_prompt  # noqa: E402


def _ctl():
    import agency_ctl as ctl

    return ctl


def _allocate_pipeline_id(root: Path, pipeline_name: str) -> str:
    """Allocate a unique, safe pipeline id for an init request."""
    try:
        data = load_state(root)
    except Exception:
        data = {"runs": []}
    for _ in range(16):
        candidate = f"{pipeline_name}-{secrets.token_hex(4)}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", candidate):
            continue
        if any(run.get("pipelineId") == candidate for run in data.get("runs", [])):
            continue
        return candidate
    raise RuntimeError("could not allocate a unique pipeline id")


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_pipeline_runner_command(
    *,
    work: str | os.PathLike[str],
    root: str | os.PathLike[str],
    project: str | os.PathLike[str],
    instance: str,
) -> str:
    """Build the fixed pipeline-runner serve command for a future launch seam."""
    if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", instance):
        raise ValueError("invalid pipeline-runner instance identifier")

    process = shlex.join(
        [
            "env",
            f"AGENCY_ROOT={os.fspath(root)}",
            f"AGENCY_PROJECT_ROOT={os.fspath(project)}",
            sys.executable,
            str((scripts_dir() / "agency_ctl.py").resolve()),
            "pipeline-runner",
            "serve",
            "--instance",
            instance,
        ]
    )
    return f"cd {shlex.quote(os.fspath(work))} && {process}"


def bootstrap_text(
    instance_name: str,
    agent_path: Path | None,
    charter: str,
    skill: str | None,
    agency_export: str,
) -> str:
    if agent_path:
        try:
            persona_ref = str(agent_path.relative_to(project_root()))
        except ValueError:
            persona_ref = str(agent_path)
        persona = f"Persona loaded via --append-system-prompt {persona_ref}."
    else:
        persona = f"Read charter {charter}."
    skill_resolved = resolve_resource(skill) if skill else None
    skill_disp = str(skill_resolved) if skill_resolved else skill
    skill_line = f"On each delegate, also read skillPath: {skill_disp}." if skill_disp else ""
    memory = str(scripts_dir() / "memory.py")
    return (
        f"{persona} {skill_line} "
        f"Your broker instance name is {instance_name}. "
        f"Agency ownership was established before Pi started at {agency_export}. "
        f'export MEMORY="{memory}"; '
        "All agency messages go through the agency_report / agency_ask / agency_progress tools — "
        "the live broker is the sole agency transport, so never read or write the file-bus directly. "
        "Use agency_report / agency_ask / agency_progress for all agency messages. "
        "Prefer built-in tools (read, grep, find, ls) over bash for read-only exploration; "
        "use bash only when no built-in tool can do the job. "
        "Wait for broker-injected delegates/replies in this Pi session. "
        "Do not wait for another human message. Do not talk to the end user."
    )


def spawn_specialist(
    role: str,
    *,
    lifecycle: str | None = None,
    name: str | None = None,
    direction: str = "right",
    reuse: bool = False,
    dry_run: bool = False,
    boot_wait: float = 5.0,
    cwd: str | None = None,
    nudge: bool = False,
    recovery: bool = False,
    pipeline_id: str | None = None,
    message: str | None = None,
    pipeline_name: str | None = None,
    topic: str | None = None,
    pipeline_init: bool = False,
) -> dict[str, Any]:
    """Open (or reuse) a specialist pane and boot pi. Must run inside cmux.

    If `message` is set, it replaces the default broker boot prompt.
    """
    ctl = _ctl()
    root = agency_root()
    if not pipeline_init:
        ctl.require_operation_authority(root, pipeline_id=pipeline_id, recovery=recovery)
    if pipeline_id is not None:
        if not name:
            raise RuntimeError("pipeline spawn denied: explicit --name is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise RuntimeError("pipeline spawn denied: name must be a safe identifier")
        ctl.require_active_dispatched_stage_spawn(root, pipeline_id, role, name)
    try:
        ctl.reconcile_cmux(root)
    except Exception:
        pass

    agents = load_agents(root)
    agent = role_defaults(agents, role)
    if not agent and role != HUB:
        raise RuntimeError(f"unknown role: {role}")

    data = load_sessions(root)
    max_panes = max_specialist_panes(agents)

    if reuse:
        if pipeline_id is not None:
            exact = find_instance(data, name)
            if exact is not None:
                if exact.get("role") != role or exact.get("status") != "idle":
                    raise RuntimeError(
                        f"reserved instance {name!r} is not an idle {role!r} instance"
                    )
                return {"ok": True, "action": "reuse", "instance": exact}
        else:
            idle = find_idle_role(data, role)
            if idle:
                return {"ok": True, "action": "reuse", "instance": idle}

    if specialist_count(data) >= max_panes:
        raise RuntimeError(f"max specialist panes reached ({max_panes})")

    spawn_cfg = agents.get("spawn") or {}
    allow_plan_twin = bool(spawn_cfg.get("allowPlanTempTwin", True))
    allow_work_twin = bool(spawn_cfg.get("allowWorkTwin", False))
    max_twins = int(spawn_cfg.get("maxTempTwinsPerRole") or 1)

    role_rows = [i for i in (data.get("instances") or []) if i.get("role") == role]
    working_rows = [i for i in role_rows if i.get("status") == "working"]
    temp_rows = [i for i in role_rows if i.get("lifecycle") == "temporary"]

    if role == "worker":
        if role_rows and not allow_work_twin:
            raise RuntimeError("Worker already registered — sole writer; queue (allowWorkTwin=false)")
        if working_rows:
            raise RuntimeError("Worker already working — queue; do not spawn a second Worker")

    if role == "pipeline-runner":
        if lifecycle not in (None, "temporary"):
            raise RuntimeError("pipeline-runner lifecycle must be temporary")
        resolved_lifecycle = "temporary"
    else:
        resolved_lifecycle = lifecycle or agent.get("lifecycleDefault") or "temporary"
    if resolved_lifecycle not in ("temporary", "persistent"):
        raise RuntimeError("lifecycle must be temporary|persistent")

    if role == "planner":
        persistent = next((i for i in role_rows if i.get("lifecycle") == "persistent"), None)
        if resolved_lifecycle == "persistent" and persistent:
            if persistent.get("status") == "idle":
                raise RuntimeError("persistent Planner already exists — use --reuse")
            if allow_plan_twin:
                raise RuntimeError(
                    "Planner is busy — spawn a temporary twin with --lifecycle temporary "
                    "(allowPlanTempTwin=true), or wait/queue"
                )
            raise RuntimeError("Plan busy — queue (allowPlanTempTwin=false)")
        if resolved_lifecycle == "temporary" and working_rows:
            if not allow_plan_twin:
                raise RuntimeError("Plan busy — queue (allowPlanTempTwin=false)")
            if len(temp_rows) >= max_twins:
                raise RuntimeError(f"Plan temp twin limit reached ({max_twins})")

    instance_name = name or make_instance_name(role, resolved_lifecycle)
    if find_instance(data, instance_name):
        raise RuntimeError(f"instance name already claimed: {instance_name}")

    fixed_runner = role == "pipeline-runner"
    charter = agent.get("charterPath") or f".pi/agency/charters/{role}.md"
    skill = None if fixed_runner else agent.get("skillPath")
    agent_path = None if fixed_runner else agent_file_for(role, agent)
    fm = parse_agent_frontmatter(agent_path) if agent_path else {}
    tools = fm.get("tools")
    spawn_cwd = Path(cwd).resolve() if cwd else project_root()
    if not spawn_cwd.is_dir():
        raise RuntimeError(f"spawn cwd is not a directory: {spawn_cwd}")
    agency_export = str(agency_root())
    now = utc_now()
    row = {
        "instanceId": f"{role}-{secrets.token_hex(4)}",
        "role": role,
        "intercomName": instance_name,
        "lifecycle": resolved_lifecycle,
        "status": "starting",
        "cwd": str(spawn_cwd),
        "taskId": None,
        "cmuxSurface": None,
        "cmuxPane": None,
        "agentPath": str(agent_path) if agent_path else None,
        "createdAt": now,
        "updatedAt": now,
    }
    data.setdefault("instances", []).append(row)
    save_sessions(root, data)
    ctl.bus_run(root, ["init", instance_name])
    if resolved_lifecycle == "persistent" or role in ("planner", "worker"):
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(scripts_dir() / "memory.py"),
                    "init",
                    "--as",
                    instance_name,
                    "--role",
                    role,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env={
                    **os.environ,
                    "AGENCY_ROOT": str(root),
                    "AGENCY_PROJECT_ROOT": str(project_root()),
                },
            )
        except Exception:
            pass

    if dry_run:
        row["status"] = "idle"
        row["updatedAt"] = utc_now()
        save_sessions(root, data)
        result = {"ok": True, "action": "spawn-dry-run", "instance": row}
        if fixed_runner:
            result["processCommand"] = build_pipeline_runner_command(
                work=spawn_cwd,
                root=root,
                project=project_root(),
                instance=instance_name,
            )
        return result

    try:
        opened = open_pane(direction or "right", focus=False)
    except RuntimeError as e:
        row["status"] = "failed"
        row["updatedAt"] = utc_now()
        save_sessions(root, data)
        raise RuntimeError(str(e)) from e

    surface = opened["surface"]
    row["cmuxSurface"] = surface
    row["cmuxPane"] = opened["pane"]
    row["updatedAt"] = utc_now()
    save_sessions(root, data)

    pipeline_id_out = pipeline_id
    final_task_id: str | None = None
    if role == "pipeline-runner" and (pipeline_name or topic):
        if pipeline_id is not None:
            raise RuntimeError("pipeline init cannot also carry --pipeline-id")
        if not pipeline_name or not topic:
            raise RuntimeError("pipeline init requires --pipeline <name> and --topic <text>")
        loaded = load_pipelines(root)
        definition = (loaded.get("pipelines") or {}).get(pipeline_name)
        if not isinstance(definition, Mapping):
            raise RuntimeError(f"pipeline {pipeline_name!r} is not defined in pipelines.yaml")
        pipeline_id_out = _allocate_pipeline_id(root, pipeline_name)
        acquire_lock(
            root,
            pipeline_id=pipeline_id_out,
            owner_id=instance_name,
            owner_surface=surface,
        )
        create_run(
            root,
            pipeline_id=pipeline_id_out,
            pipeline_name=pipeline_name,
            topic=topic,
            definition=definition,
            lock_owner=instance_name,
            runner_instance=instance_name,
            runner_surface=surface,
        )
        final_task_id = f"pipe-done-{pipeline_id_out}"

    work = str(spawn_cwd)
    if fixed_runner:
        process_cmd = build_pipeline_runner_command(
            work=spawn_cwd,
            root=root,
            project=project_root(),
            instance=instance_name,
        )
        try:
            send_to_surface(surface, process_cmd)
        except RuntimeError as e:
            row["status"] = "failed"
            row["updatedAt"] = utc_now()
            save_sessions(root, data)
            raise RuntimeError(f"cmux send pipeline runner failed: {e}") from e
        row["status"] = "idle"
        row["updatedAt"] = utc_now()
        save_sessions(root, data)
        result = {
            "ok": True,
            "action": "spawn",
            "instance": row,
            "bootWaitSec": boot_wait,
            "processCommand": process_cmd,
        }
        if final_task_id is not None:
            result["pipelineId"] = pipeline_id_out
            result["finalTaskId"] = final_task_id
        return result

    boot = (
        message
        if message is not None
        else bootstrap_text(instance_name, agent_path, charter, skill, agency_export)
    )
    boot_path = write_boot_prompt(root, instance_name, boot)
    pi_cmd = build_pi_command(
        work=work,
        instance_name=instance_name,
        agent_path=agent_path,
        tools=tools,
        boot_path=boot_path,
        agency_root=str(root.resolve()),
        agency_project_root=str(project_root().resolve()),
    )

    try:
        send_to_surface(surface, pi_cmd)
    except RuntimeError as e:
        row["status"] = "failed"
        row["updatedAt"] = utc_now()
        save_sessions(root, data)
        raise RuntimeError(f"cmux send pi failed: {e}") from e

    row["status"] = "idle"
    row["updatedAt"] = utc_now()
    save_sessions(root, data)
    return {
        "ok": True,
        "action": "spawn",
        "instance": row,
        "bootWaitSec": boot_wait,
        "bootPromptPath": str(boot_path),
        "piCommand": pi_cmd,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agent_spawn",
        description="Spawn or reuse a Multi-Agency specialist pane",
    )
    p.add_argument("--role", required=True)
    p.add_argument("--lifecycle", choices=["temporary", "persistent"])
    p.add_argument("--name")
    p.add_argument("--direction", default="right", choices=["left", "right", "up", "down"])
    p.add_argument("--reuse", action="store_true", help="Reuse idle instance of role if present")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--boot-wait", type=float, default=5.0)
    p.add_argument("--cwd", help="Pane working directory (Scout reference-repo mode)")
    p.add_argument("--pipeline-id", help="Select bound pipeline-runner authority")
    p.add_argument(
        "--message",
        "-m",
        help="Custom first-turn boot message",
    )
    p.add_argument("--nudge", action=argparse.BooleanOptionalAction, default=False, help=argparse.SUPPRESS)
    p.add_argument(
        "--recovery",
        action="store_true",
        help="Skip orchestrator surface gate (lifecycle abandon/respawn)",
    )
    args = p.parse_args(argv)
    try:
        result = spawn_specialist(
            args.role,
            lifecycle=args.lifecycle,
            name=args.name,
            direction=args.direction,
            reuse=args.reuse,
            dry_run=args.dry_run,
            boot_wait=args.boot_wait,
            cwd=args.cwd,
            nudge=False,
            recovery=args.recovery,
            pipeline_id=args.pipeline_id,
            message=args.message,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
