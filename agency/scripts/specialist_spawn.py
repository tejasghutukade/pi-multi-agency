#!/usr/bin/env python3
"""Spawn / reuse a Multi-Agency specialist pane (importable + CLI)."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _ctl():
    import agency_ctl as ctl

    return ctl


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def bootstrap_text(
    instance_name: str,
    agent_path: Path | None,
    charter: str,
    skill: str | None,
    agency_export: str,
) -> str:
    ctl = _ctl()
    if agent_path:
        try:
            persona_ref = str(agent_path.relative_to(ctl.project_root()))
        except ValueError:
            persona_ref = str(agent_path)
        persona = f"Persona loaded via --append-system-prompt {persona_ref}."
    else:
        persona = f"Read charter {charter}."
    skill_resolved = ctl.resolve_resource(skill) if skill else None
    skill_disp = str(skill_resolved) if skill_resolved else skill
    skill_line = f"On each delegate, also read skillPath: {skill_disp}." if skill_disp else ""
    bus = str(ctl.bus_py())
    return (
        f"{persona} {skill_line} "
        f"Your bus instance name is {instance_name}. "
        f'export AGENCY_ROOT="{agency_export}". '
        f"Immediately run: python3 {bus} recv --as {instance_name} --wait 60 --interval 2 "
        "and process any pending delegate (report/ask to orchestrator, then bus done). "
        "Do not wait for another human message. Do not talk to the end user."
    )


def write_boot_prompt(root: Path, instance_name: str, text: str) -> Path:
    boot_dir = root / "artifacts" / "_boot"
    boot_dir.mkdir(parents=True, exist_ok=True)
    path = boot_dir / f"{instance_name}.txt"
    path.write_text(text)
    return path


def build_pi_command(
    *,
    work: str,
    instance_name: str,
    agent_path: Path | None,
    tools: str | None,
    boot_path: Path,
) -> str:
    parts = [f"cd {shell_quote(work)} && pi --approve --name {shell_quote(instance_name)}"]
    if agent_path:
        parts.append(f"--append-system-prompt {shell_quote(str(agent_path))}")
    if tools:
        cleaned = ",".join(t.strip() for t in tools.split(",") if t.strip())
        if cleaned:
            parts.append(f"--tools {shell_quote(cleaned)}")
    parts.append(f'"$(cat {shell_quote(str(boot_path))})"')
    return " ".join(parts)


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
    nudge: bool = True,
    recovery: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """Open (or reuse) a specialist pane and boot pi. Must run inside cmux.

    If `message` is set, it replaces the default bus-recv boot prompt.
    """
    from cmux_pane import open_pane, send_to_surface

    ctl = _ctl()
    root = ctl.agency_root()
    ctl.require_orchestrator(root, recovery=recovery)
    try:
        ctl.reconcile_cmux(root)
    except Exception:
        pass

    agents = ctl.load_agents(root)
    agent = (agents.get("agents") or {}).get(role)
    if not agent and role != ctl.HUB:
        raise RuntimeError(f"unknown role: {role}")

    data = ctl.load_sessions(root)
    max_panes = int(((agents.get("spawn") or {}).get("maxSpecialistPanes")) or 6)

    if reuse:
        idle = ctl.find_idle_role(data, role)
        if idle:
            return {"ok": True, "action": "reuse", "instance": idle}

    if ctl.specialist_count(data) >= max_panes:
        raise RuntimeError(f"max specialist panes reached ({max_panes})")

    spawn_cfg = agents.get("spawn") or {}
    allow_plan_twin = bool(spawn_cfg.get("allowPlanTempTwin", True))
    allow_work_twin = bool(spawn_cfg.get("allowWorkTwin", False))
    max_twins = int(spawn_cfg.get("maxTempTwinsPerRole") or 1)

    role_rows = [i for i in (data.get("instances") or []) if i.get("role") == role]
    working_rows = [i for i in role_rows if i.get("status") == "working"]
    temp_rows = [i for i in role_rows if i.get("lifecycle") == "temporary"]

    if role == "work":
        if role_rows and not allow_work_twin:
            raise RuntimeError("Work already registered — sole writer; queue (allowWorkTwin=false)")
        if working_rows:
            raise RuntimeError("Work already working — queue; do not spawn a second Work")

    resolved_lifecycle = lifecycle or (agent or {}).get("lifecycleDefault") or "temporary"
    if resolved_lifecycle not in ("temporary", "persistent"):
        raise RuntimeError("lifecycle must be temporary|persistent")

    if role == "plan":
        persistent = next((i for i in role_rows if i.get("lifecycle") == "persistent"), None)
        if resolved_lifecycle == "persistent" and persistent:
            if persistent.get("status") == "idle":
                raise RuntimeError("persistent Plan already exists — use --reuse")
            if allow_plan_twin:
                raise RuntimeError(
                    "Plan is busy — spawn a temporary twin with --lifecycle temporary "
                    "(allowPlanTempTwin=true), or wait/queue"
                )
            raise RuntimeError("Plan busy — queue (allowPlanTempTwin=false)")
        if resolved_lifecycle == "temporary" and working_rows:
            if not allow_plan_twin:
                raise RuntimeError("Plan busy — queue (allowPlanTempTwin=false)")
            if len(temp_rows) >= max_twins:
                raise RuntimeError(f"Plan temp twin limit reached ({max_twins})")

    instance_name = name or ctl.make_instance_name(role, resolved_lifecycle)
    if ctl.find_instance(data, instance_name):
        raise RuntimeError(f"instance name already claimed: {instance_name}")

    charter = (agent or {}).get("charterPath") or f".pi/agency/charters/{role}.md"
    skill = (agent or {}).get("skillPath")
    agent_path = ctl.agent_file_for(role, agent)
    fm = ctl.parse_agent_frontmatter(agent_path) if agent_path else {}
    tools = fm.get("tools")
    spawn_cwd = Path(cwd).resolve() if cwd else ctl.project_root()
    if not spawn_cwd.is_dir():
        raise RuntimeError(f"spawn cwd is not a directory: {spawn_cwd}")
    agency_export = str(ctl.agency_root())
    now = ctl.utc_now()
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
    ctl.save_sessions(root, data)
    ctl.bus_run(root, ["init", instance_name])
    if resolved_lifecycle == "persistent" or role in ("plan", "work"):
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(ctl.memory_py()),
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
                    "AGENCY_PROJECT_ROOT": str(ctl.project_root()),
                },
            )
        except Exception:
            pass

    if dry_run:
        row["status"] = "idle"
        row["updatedAt"] = ctl.utc_now()
        ctl.save_sessions(root, data)
        return {"ok": True, "action": "spawn-dry-run", "instance": row}

    try:
        opened = open_pane(direction or "right", focus=False)
    except RuntimeError as e:
        row["status"] = "failed"
        row["updatedAt"] = ctl.utc_now()
        ctl.save_sessions(root, data)
        raise RuntimeError(str(e)) from e

    surface = opened["surface"]
    row["cmuxSurface"] = surface
    row["cmuxPane"] = opened["pane"]
    row["updatedAt"] = ctl.utc_now()
    ctl.save_sessions(root, data)

    work = str(spawn_cwd)
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
    )

    try:
        send_to_surface(surface, pi_cmd)
    except RuntimeError as e:
        row["status"] = "failed"
        row["updatedAt"] = ctl.utc_now()
        ctl.save_sessions(root, data)
        raise RuntimeError(f"cmux send pi failed: {e}") from e

    nudged = False
    if nudge and boot_wait > 0:
        time.sleep(boot_wait)
        bus = str(ctl.bus_py())
        nudge_body = (
            f"If idle: export AGENCY_ROOT={agency_export}; "
            f"python3 {bus} recv --as {instance_name} "
            f"--wait 60 --interval 2; process any delegate now."
        )
        try:
            send_to_surface(surface, nudge_body)
            nudged = True
        except RuntimeError:
            nudged = False

    row["status"] = "idle"
    row["updatedAt"] = ctl.utc_now()
    ctl.save_sessions(root, data)
    return {
        "ok": True,
        "action": "spawn",
        "instance": row,
        "bootWaitSec": boot_wait,
        "bootPromptPath": str(boot_path),
        "nudged": nudged,
        "piCommand": pi_cmd,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="specialist_spawn",
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
    p.add_argument(
        "--message",
        "-m",
        help="Custom first-turn boot message (replaces default bus-recv prompt)",
    )
    p.add_argument(
        "--nudge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After boot-wait, send one fallback kick to start bus poll (default: true)",
    )
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
            nudge=args.nudge,
            recovery=args.recovery,
            message=args.message,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
