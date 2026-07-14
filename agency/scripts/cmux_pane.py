#!/usr/bin/env python3
"""Open / send / close cmux terminal panes (must run inside cmux)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

CmuxRunner = Callable[..., subprocess.CompletedProcess[str]]


def find_cmux() -> str:
    cmux = shutil.which("cmux")
    if cmux:
        return cmux
    for c in (
        Path.home() / "bin" / "cmux",
        Path("/Applications/cmux.app/Contents/Resources/bin/cmux"),
    ):
        if c.exists():
            return str(c)
    raise RuntimeError("cmux not found on PATH")


def _default_runner(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    cmux = find_cmux()
    return subprocess.run(
        [cmux, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_runner: CmuxRunner = _default_runner


def set_runner(runner: CmuxRunner | None) -> None:
    """Override subprocess runner (tests). Pass None to restore default."""
    global _runner
    _runner = runner if runner is not None else _default_runner


def cmux_run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return _runner(args, timeout=timeout)


def cmux_json(args: list[str]) -> Any:
    r = cmux_run(args)
    if r.returncode != 0:
        raise RuntimeError(f"cmux {' '.join(args)} failed: {r.stderr or r.stdout}")
    text = (r.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_new_split_surface(stdout: str) -> str:
    m = re.search(r"surface:\d+", stdout)
    if not m:
        raise RuntimeError(f"could not parse surface from new-split output: {stdout!r}")
    return m.group(0)


def tree_text(*, all_surfaces: bool = True, timeout: float = 5) -> str:
    """Raw cmux tree text (stdout+stderr). Empty string on failure."""
    args = ["tree", "--all"] if all_surfaces else ["tree"]
    try:
        r = cmux_run(args, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def identify() -> dict[str, Any]:
    data = cmux_json(["identify"])
    if not isinstance(data, dict):
        raise RuntimeError("cmux identify did not return JSON")
    return data


def caller_surface() -> tuple[str, str]:
    data = identify()
    caller = data.get("caller") or data.get("focused") or {}
    surface = (
        caller.get("surface_ref")
        or caller.get("surface_id")
        or data.get("surfaceId")
        or data.get("surface")
    )
    pane = (
        caller.get("pane_ref")
        or caller.get("pane_id")
        or data.get("paneId")
        or data.get("pane")
    )
    if not surface or not pane:
        raise RuntimeError("could not resolve caller surface/pane from cmux identify")
    return str(surface), str(pane)


def pane_for_surface(surface: str) -> str:
    tree = cmux_json(["tree", "--json"])
    if not isinstance(tree, dict):
        raise RuntimeError("cmux tree --json failed")
    for w in tree.get("windows") or []:
        for ws in w.get("workspaces") or []:
            for p in ws.get("panes") or []:
                for s in p.get("surfaces") or []:
                    if s.get("ref") == surface:
                        return str(p.get("ref"))
    raise RuntimeError(f"pane not found for {surface}")


def surface_alive(surface: str | None) -> bool | None:
    """True/False if cmux tree available; None if tree unavailable."""
    if not surface:
        return None
    try:
        text = tree_text(all_surfaces=True, timeout=5)
        if not text.strip():
            return None
        return str(surface) in text
    except Exception:
        return None


def send_to_surface(surface: str, text: str, *, enter: bool = True) -> subprocess.CompletedProcess[str]:
    """Inject text into a cmux terminal surface. Appends \\n (Enter) unless already present."""
    payload = text
    if enter and not payload.endswith("\\n") and not payload.endswith("\n"):
        payload = payload + "\\n"
    elif enter and payload.endswith("\n") and not payload.endswith("\\n"):
        payload = payload[:-1] + "\\n"
    r = cmux_run(["send", "--surface", surface, payload])
    if r.returncode != 0:
        raise RuntimeError(f"cmux send failed: {r.stderr or r.stdout}")
    return r


def close_surface(surface: str) -> subprocess.CompletedProcess[str]:
    r = cmux_run(["close-surface", "--surface", str(surface)])
    try:
        from agency_events import emit
        from agency_paths import agency_root

        emit("cmux.closed", root=agency_root(), surface=str(surface), ok=r.returncode == 0)
    except Exception:
        pass
    return r


def open_pane(
    direction: str = "right",
    *,
    command: str | None = None,
    focus: bool = False,
    enter: bool = True,
) -> dict[str, Any]:
    """Open a cmux split; optionally send `command` into it (must run inside cmux).

    Example: open_pane(command='echo "Hello World"')
    Returns {"surface", "pane", "direction", "command", "sent"}.
    """
    split = cmux_run(
        ["new-split", direction or "right", "--focus", "true" if focus else "false"]
    )
    if split.returncode != 0:
        raise RuntimeError(split.stderr or split.stdout or "new-split failed")
    surface = parse_new_split_surface(split.stdout)
    pane = pane_for_surface(surface)
    sent = False
    if command is not None:
        send_to_surface(surface, command, enter=enter)
        sent = True
    result = {
        "surface": surface,
        "pane": pane,
        "direction": direction or "right",
        "command": command,
        "sent": sent,
    }
    try:
        from agency_events import emit
        from agency_paths import agency_root

        emit("cmux.opened", root=agency_root(), surface=surface, pane=pane, direction=result["direction"])
    except Exception:
        pass
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="cmux_pane", description="Open / send / close cmux panes")
    sub = p.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open", help="Open a new split; optionally run a command")
    op.add_argument("--direction", default="right", choices=["left", "right", "up", "down"])
    op.add_argument("--command", "-c", help='Command to send (e.g. echo "Hello World")')
    op.add_argument("--focus", action="store_true")
    op.add_argument("--no-enter", action="store_true", help="Do not append Enter after command")

    sd = sub.add_parser("send", help="Send text to an existing surface")
    sd.add_argument("--surface", required=True)
    sd.add_argument("--text", "-t", required=True)
    sd.add_argument("--no-enter", action="store_true")

    cl = sub.add_parser("close", help="Close a surface")
    cl.add_argument("--surface", required=True)

    al = sub.add_parser("alive", help="Check whether a surface appears in cmux tree")
    al.add_argument("--surface", required=True)

    args = p.parse_args(argv)
    try:
        if args.cmd == "open":
            result = open_pane(
                args.direction,
                command=args.command,
                focus=args.focus,
                enter=not args.no_enter,
            )
            print(json.dumps({"ok": True, "action": "open", **result}, indent=2))
            return 0
        if args.cmd == "send":
            send_to_surface(args.surface, args.text, enter=not args.no_enter)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "action": "send",
                        "surface": args.surface,
                        "text": args.text,
                    },
                    indent=2,
                )
            )
            return 0
        if args.cmd == "close":
            r = close_surface(args.surface)
            print(
                json.dumps(
                    {
                        "ok": r.returncode == 0,
                        "action": "close",
                        "surface": args.surface,
                        "stdout": (r.stdout or "").strip(),
                        "stderr": (r.stderr or "").strip(),
                    },
                    indent=2,
                )
            )
            return 0 if r.returncode == 0 else 1
        if args.cmd == "alive":
            alive = surface_alive(args.surface)
            print(json.dumps({"ok": True, "action": "alive", "surface": args.surface, "alive": alive}, indent=2))
            return 0
        raise RuntimeError(f"unknown {args.cmd}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
