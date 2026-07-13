#!/usr/bin/env python3
"""Reconcile sessions.json — mark/clear stale instances.

Phase 1 stale recovery: if a row claims a cmux surface/pane that no longer
exists (or force --stale-names), clear it so the next spawn is fresh.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def load_sessions(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "instances": []}
    return json.loads(path.read_text())


def save_sessions(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def cmux_tree_text() -> str:
    cmux = shutil.which("cmux")
    if not cmux:
        for c in (
            Path.home() / "bin" / "cmux",
            Path("/Applications/cmux.app/Contents/Resources/bin/cmux"),
        ):
            if c.exists():
                cmux = str(c)
                break
    if not cmux:
        return ""
    try:
        r = subprocess.run(
            [cmux, "tree", "--all"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    p = argparse.ArgumentParser(description="Reconcile agency sessions.json for stale rows")
    p.add_argument(
        "--force-stale",
        nargs="*",
        default=[],
        help="Instance intercomName(s) to force-clear as stale",
    )
    p.add_argument(
        "--check-cmux",
        action="store_true",
        help="If cmux tree works, clear rows whose surface/pane id is missing from tree",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = agency_root()
    path = root / "sessions.json"
    data = load_sessions(path)
    instances = list(data.get("instances") or [])
    force = set(args.force_stale)
    tree = cmux_tree_text() if args.check_cmux else ""
    cmux_ok = bool(tree) and "Access denied" not in tree and "Broken pipe" not in tree

    kept = []
    cleared = []
    for inst in instances:
        name = inst.get("intercomName") or inst.get("instanceId")
        surface = str(inst.get("cmuxSurface") or "")
        pane = str(inst.get("cmuxPane") or "")
        stale = name in force
        if not stale and cmux_ok and (surface or pane):
            if surface and surface not in tree:
                stale = True
            if pane and pane not in tree:
                stale = True
        if stale:
            cleared.append(inst)
        else:
            kept.append(inst)

    result = {
        "ok": True,
        "cmuxCheck": cmux_ok,
        "before": len(instances),
        "cleared": [
            {
                "intercomName": i.get("intercomName"),
                "status": i.get("status"),
                "cmuxSurface": i.get("cmuxSurface"),
                "cmuxPane": i.get("cmuxPane"),
            }
            for i in cleared
        ],
        "after": len(kept),
        "dryRun": args.dry_run,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    if not args.dry_run:
        data["instances"] = kept
        save_sessions(path, data)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
