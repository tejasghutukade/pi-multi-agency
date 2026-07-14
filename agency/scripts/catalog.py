#!/usr/bin/env python3
"""Static agent config — agents.yaml + persona frontmatter (not live roster)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agency_paths import kit_root, resolve_resource

HUB = "orchestrator"


def parse_agents_fallback(text: str) -> dict[str, Any]:
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
            if "maxSpecialistPanes:" in line:
                try:
                    agents["spawn"]["maxSpecialistPanes"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
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
        return data if isinstance(data, dict) else parse_agents_fallback(text)
    except ImportError:
        return parse_agents_fallback(text)


def role_of(instance: str) -> str:
    if instance == HUB:
        return HUB
    if "-t" in instance:
        return instance.split("-t", 1)[0]
    return instance


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


def role_defaults(agents: dict[str, Any], role: str) -> dict[str, Any]:
    return dict((agents.get("agents") or {}).get(role) or {})


def max_specialist_panes(agents: dict[str, Any], default: int = 6) -> int:
    spawn = agents.get("spawn") or {}
    try:
        return int(spawn.get("maxSpecialistPanes", default))
    except (TypeError, ValueError):
        return default
