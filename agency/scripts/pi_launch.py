#!/usr/bin/env python3
"""Build and start a pi TUI turn in a cmux pane (no agency policy)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cmux_pane import open_pane


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


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
    agent_path: Path | None = None,
    tools: str | None = None,
    boot_path: Path | None = None,
    message: str | None = None,
) -> str:
    parts = [f"cd {shell_quote(work)} && pi --approve --name {shell_quote(instance_name)}"]
    if agent_path:
        parts.append(f"--append-system-prompt {shell_quote(str(agent_path))}")
    if tools:
        cleaned = ",".join(t.strip() for t in tools.split(",") if t.strip())
        if cleaned:
            parts.append(f"--tools {shell_quote(cleaned)}")
    if boot_path is not None:
        parts.append(f'"$(cat {shell_quote(str(boot_path))})"')
    elif message is not None:
        parts.append(shell_quote(message))
    return " ".join(parts)


def launch_pi(
    cwd: str,
    name: str,
    *,
    tools: str | None = None,
    persona_path: Path | None = None,
    message: str | None = None,
    boot_path: Path | None = None,
    direction: str = "right",
    focus: bool = False,
    enter: bool = True,
) -> dict[str, Any]:
    """Open a pane and send a pi command. Returns {surface, pane, command, ...open_pane fields}."""
    command = build_pi_command(
        work=cwd,
        instance_name=name,
        agent_path=persona_path,
        tools=tools,
        boot_path=boot_path,
        message=message,
    )
    opened = open_pane(direction, command=command, focus=focus, enter=enter)
    return {**opened, "command": command}
