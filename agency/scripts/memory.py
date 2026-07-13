#!/usr/bin/env python3
"""Multi-Agency instance memory helpers — see memory-spec.md"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def memory_dir(root: Path, name: str) -> Path:
    return root / "memory" / name


def notes_path(root: Path, name: str) -> Path:
    return memory_dir(root, name) / "NOTES.md"


def cmd_path(args: argparse.Namespace) -> int:
    root = agency_root()
    p = notes_path(root, args.as_name)
    print(json.dumps({"ok": True, "path": str(p), "exists": p.is_file()}))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = agency_root()
    d = memory_dir(root, args.as_name)
    d.mkdir(parents=True, exist_ok=True)
    p = notes_path(root, args.as_name)
    if not p.exists():
        role = args.role or args.as_name.split("-t")[0]
        p.write_text(
            f"# Memory — {args.as_name} ({role})\n\n"
            f"## Active\n"
            f"- Workflow / feature:\n"
            f"- Plan / key artifact paths:\n"
            f"- Decisions locked:\n"
            f"- Open blockers:\n\n"
            f"## Log\n"
        )
    print(json.dumps({"ok": True, "path": str(p), "created": True}))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    root = agency_root()
    p = notes_path(root, args.as_name)
    if not p.exists():
        cmd_init(args)
    line = f"- {date.today().isoformat()} {args.task_id}: {args.note}\n"
    with p.open("a") as f:
        f.write(line)
    print(json.dumps({"ok": True, "path": str(p), "appended": line.strip()}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Agency instance memory")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_ in (
        ("path", "Print NOTES.md path"),
        ("init", "Create NOTES.md if missing"),
        ("log", "Append a Log line"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--as", dest="as_name", required=True)
        if name == "init":
            sp.add_argument("--role")
        if name == "log":
            sp.add_argument("--task-id", required=True)
            sp.add_argument("--note", required=True)
            sp.add_argument("--role")

    args = p.parse_args()
    if args.cmd == "path":
        return cmd_path(args)
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "log":
        return cmd_log(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
