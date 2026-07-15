#!/usr/bin/env python3
"""Reconcile sessions.json — mark/clear stale instances.

Phase 1 stale recovery: if a row claims a cmux surface/pane that no longer
exists (or force --stale-names), clear it so the next spawn is fresh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cmux_pane import cmux_json  # noqa: E402
from ledger import load_sessions, save_sessions  # noqa: E402


def _alive_surface_pane_refs() -> tuple[set[str], set[str]] | None:
    """Return (live_surfaces, live_panes) exact ref sets from the cmux tree.

    Uses the structured `tree --json` output so staleness is decided by exact
    surface/pane ref membership, never by raw-text substring match (which could
    tear down a live session whose ref merely appears inside an unrelated line).
    Returns None when cmux is unavailable so callers fail safe.
    """
    try:
        tree = cmux_json(["tree", "--json"])
    except Exception:
        return None
    if not isinstance(tree, dict):
        return None
    surfaces: set[str] = set()
    panes: set[str] = set()
    for w in tree.get("windows") or []:
        for ws in w.get("workspaces") or []:
            for p in ws.get("panes") or []:
                if p.get("ref") is not None:
                    panes.add(str(p["ref"]))
                for s in p.get("surfaces") or []:
                    if s.get("ref") is not None:
                        surfaces.add(str(s["ref"]))
    return surfaces, panes


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def run_reconcile(argv: list[str]) -> dict[str, Any]:
    """Reconcile sessions.json for stale rows; return the result dict.

    Testable entry point. `main()` is the thin CLI wrapper that passes sys.argv.
    """
    p = argparse.ArgumentParser(description="Reconcile agency sessions.json for stale rows")
    p.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Agency root (defaults to AGENCY_ROOT env or the parent of this script)",
    )
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
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else agency_root()
    data = load_sessions(root)
    instances = list(data.get("instances") or [])
    force = set(args.force_stale)
    # Exact surface/pane ref membership from cmux tree --json. None means cmux is
    # unavailable, in which case we must NOT treat missing refs as stale (fail safe).
    refs = _alive_surface_pane_refs() if args.check_cmux else None
    cmux_ok = refs is not None
    live_surfaces, live_panes = refs if refs is not None else (set(), set())

    kept = []
    cleared = []
    for inst in instances:
        name = inst.get("intercomName") or inst.get("instanceId")
        surface = str(inst.get("cmuxSurface") or "")
        pane = str(inst.get("cmuxPane") or "")
        stale = name in force
        if not stale and cmux_ok and (surface or pane):
            # Exact membership only — never substring against raw tree text, which
            # could flag a live session stale because its short ref happens to appear
            # inside an unrelated line and tear it down.
            if surface and surface not in live_surfaces:
                stale = True
            if pane and pane not in live_panes:
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
        save_sessions(root, data)

    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    run_reconcile(sys.argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
