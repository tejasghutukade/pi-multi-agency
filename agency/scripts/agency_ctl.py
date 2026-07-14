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

from cmux_pane import (  # noqa: E402
    caller_surface,
    close_surface,
    cmux_json,
    cmux_run,
    identify,
    surface_alive,
)

HUB = "orchestrator"
STARTING_TIMEOUT_SEC = 90
# Hub process allowlist: read/search + agency_* — no edit/write/bash (see docs/architecture.md).
HUB_TOOLS = (
    "read,grep,find,ls,"
    "agency_init,agency_list,agency_spawn,agency_delegate,agency_wait,agency_release"
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


def package_root() -> Path:
    """Repo / pi-package root (parent of agency/)."""
    return Path(__file__).resolve().parent.parent.parent


def kit_root() -> Path:
    return package_root() / "agency"


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    proj = Path(os.environ.get("AGENCY_PROJECT_ROOT") or Path.cwd()).resolve()
    local = proj / ".pi" / "agency"
    if local.is_dir():
        return local.resolve()
    return kit_root()


def project_root() -> Path:
    env = os.environ.get("AGENCY_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    root = agency_root()
    if root.name == "agency" and root.parent.name == ".pi":
        return root.parent.parent.resolve()
    return Path.cwd().resolve()


def resolve_resource(rel: str | None) -> Path | None:
    """Resolve a path against project, then package root, then kit."""
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute():
        return p
    for base in (project_root(), package_root(), kit_root()):
        cand = (base / p).resolve()
        if cand.exists():
            return cand
    return (project_root() / p).resolve()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_sessions(root: Path) -> dict[str, Any]:
    path = root / "sessions.json"
    if not path.exists():
        return {"version": 1, "instances": []}
    return json.loads(path.read_text())


def save_sessions(root: Path, data: dict[str, Any]) -> None:
    (root / "sessions.json").write_text(json.dumps(data, indent=2) + "\n")


def _parse_agents_fallback(text: str) -> dict[str, Any]:
    agents: dict[str, Any] = {"agents": {}, "spawn": {"maxSpecialistPanes": 6}}
    current: str | None = None
    in_agents = False
    for line in text.splitlines():
        if line.startswith("agents:"):
            in_agents = True
            continue
        if line.startswith("spawn:"):
            in_agents = False
            current = None
            continue
        if in_agents and line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            current = line.strip().rstrip(":")
            if current.startswith("#"):
                current = None
                continue
            agents["agents"][current] = {"peers": [], "lifecycleDefault": "temporary"}
            continue
        if not current:
            continue
        if "lifecycleDefault:" in line:
            agents["agents"][current]["lifecycleDefault"] = line.split(":", 1)[1].strip()
        elif "charterPath:" in line:
            agents["agents"][current]["charterPath"] = line.split(":", 1)[1].strip()
        elif "skillPath:" in line:
            val = line.split(":", 1)[1].strip()
            agents["agents"][current]["skillPath"] = None if val in ("null", "~", "") else val
        elif "agentPath:" in line:
            agents["agents"][current]["agentPath"] = line.split(":", 1)[1].strip()
        elif "peers:" in line:
            rest = line.split("peers:", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                agents["agents"][current]["peers"] = [p.strip() for p in inner.split(",") if p.strip()]
        elif "maxSpecialistPanes:" in line:
            try:
                agents["spawn"]["maxSpecialistPanes"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return agents


def load_agents(root: Path) -> dict[str, Any]:
    path = root / "agents.yaml"
    if not path.exists():
        path = kit_root() / "agents.yaml"
    if not path.exists():
        return {"agents": {}, "spawn": {"maxSpecialistPanes": 6}}
    text = path.read_text()
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else _parse_agents_fallback(text)
    except ImportError:
        return _parse_agents_fallback(text)


def role_of(instance: str) -> str:
    if instance == HUB:
        return HUB
    if "-t" in instance:
        return instance.split("-t", 1)[0]
    return instance


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


def find_instance_by_task(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for inst in data.get("instances") or []:
        if inst.get("taskId") == task_id:
            return inst
    return None


def cmd_wait(args: argparse.Namespace) -> int:
    root = agency_root()
    require_orchestrator(root)
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


def find_instance(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for i in data.get("instances") or []:
        if i.get("intercomName") == name or i.get("instanceId") == name:
            return i
    return None


def find_idle_role(data: dict[str, Any], role: str) -> dict[str, Any] | None:
    for i in data.get("instances") or []:
        if i.get("role") == role and i.get("status") == "idle" and i.get("intercomName") != HUB:
            return i
    return None


def specialist_count(data: dict[str, Any]) -> int:
    return sum(1 for i in (data.get("instances") or []) if i.get("role") != HUB)


def make_instance_name(role: str, lifecycle: str) -> str:
    if lifecycle == "persistent":
        return role
    return f"{role}-t{secrets.token_hex(2)}"


def agent_file_for(role: str, agent: dict[str, Any] | None) -> Path | None:
    rel = (agent or {}).get("agentPath") or f".pi/agents/{role}.md"
    path = resolve_resource(rel)
    if path and path.is_file():
        return path
    alt = resolve_resource(f"agents/{role}.md")
    return alt if alt and alt.is_file() else None


def parse_agent_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip()
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


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


def cmd_spawn(args: argparse.Namespace) -> int:
    from specialist_spawn import spawn_specialist

    result = spawn_specialist(
        args.role,
        lifecycle=args.lifecycle,
        name=args.name,
        direction=args.direction or "right",
        reuse=bool(args.reuse),
        dry_run=bool(args.dry_run),
        boot_wait=float(args.boot_wait),
        cwd=args.cwd,
        nudge=bool(args.nudge),
        recovery=bool(getattr(args, "recovery", False)),
        message=getattr(args, "message", None),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_delegate(args: argparse.Namespace) -> int:
    root = agency_root()
    require_orchestrator(root, recovery=bool(getattr(args, "recovery", False)))
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

    inst["status"] = "working"
    inst["taskId"] = args.task_id
    inst["nudgeCount"] = 0
    inst["silentSettleAt"] = None
    inst["awaitingStartAfterNudge"] = False
    inst["lastDelegate"] = {
        "taskId": args.task_id,
        "workflowId": args.workflow_id,
        "payload": payload,
        "to": args.to,
        "at": utc_now(),
    }
    inst["updatedAt"] = utc_now()
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
    result = bus_run(root, bus_args)
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

    if agency.exists() and not args.force:
        if (agency / "agents.yaml").exists() and (agency / "sessions.json").exists():
            print(
                json.dumps(
                    {
                        "ok": True,
                        "action": "init",
                        "skipped": True,
                        "reason": "already initialized (pass --force to refresh templates)",
                        "agencyRoot": str(agency),
                        "projectRoot": str(proj),
                        "packageRoot": str(pkg),
                    },
                    indent=2,
                )
            )
            return 0

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

    copy_file(kit / "agents.yaml", agency / "agents.yaml")
    copy_file(kit / "bus-spec.md", agency / "bus-spec.md")
    copy_file(kit / "memory-spec.md", agency / "memory-spec.md")
    copy_tree(kit / "charters", agency / "charters")

    for md in (pkg / "agents").glob("*.md"):
        copy_file(md, agents_dir / md.name)

    sessions = agency / "sessions.json"
    if not sessions.exists() or args.force:
        copy_file(kit / "templates" / "sessions.json", sessions)

    (agency / ".package-root").write_text(str(pkg) + "\n")
    copied.append(".pi/agency/.package-root")

    env = os.environ.copy()
    env["AGENCY_ROOT"] = str(agency)
    env["AGENCY_PROJECT_ROOT"] = str(proj)
    subprocess.run(
        [sys.executable, str(bus_py()), "init", HUB],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(proj),
    )

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
                    "/reload → /agency-claim → agency_list",
                ],
                "hubTools": HUB_TOOLS,
            },
            indent=2,
        )
    )
    return 0


def hub_start_command(proj: Path | None = None) -> str:
    """Canonical Orchestrator hub launch (persona + tools lock)."""
    root = (proj or project_root()).resolve()
    persona = root / ".pi" / "agents" / "orchestrator.md"
    if not persona.is_file():
        persona = package_root() / "agents" / "orchestrator.md"
    return (
        f"pi --approve --name {HUB} --tools {HUB_TOOLS} "
        f"--append-system-prompt {persona}"
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
                    "Then /reload → /agency-claim → agency_list",
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

    data["instances"] = [
        i
        for i in (data.get("instances") or [])
        if i.get("intercomName") != inst.get("intercomName")
        and i.get("instanceId") != inst.get("instanceId")
    ]
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
    sp.add_argument(
        "--message",
        "-m",
        help="Custom first-turn boot message (replaces default bus-recv prompt)",
    )
    sp.add_argument(
        "--nudge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After boot-wait, send one fallback kick to start bus poll (default: true)",
    )
    sp.add_argument(
        "--recovery",
        action="store_true",
        help="Skip orchestrator surface gate (lifecycle abandon/respawn)",
    )

    d = sub.add_parser("delegate", help="Send bus delegate envelope")
    d.add_argument("--to", required=True)
    d.add_argument("--task-id", required=True)
    d.add_argument("--workflow-id")
    d.add_argument("--goal")
    d.add_argument("--context-paths", help="JSON array of paths")
    d.add_argument("--success-criteria")
    d.add_argument("--constraints")
    d.add_argument("--charter-path")
    d.add_argument("--skill-path")
    d.add_argument("--output-shape")
    d.add_argument("--stop-rules")
    d.add_argument("--payload-json")
    d.add_argument(
        "--recovery",
        action="store_true",
        help="Skip orchestrator surface gate (lifecycle abandon/respawn)",
    )

    w = sub.add_parser("wait", help="Wait for hub inbox report/ask for a taskId (legacy)")
    w.add_argument("--task-id", required=True)
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
