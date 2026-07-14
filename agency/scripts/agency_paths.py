#!/usr/bin/env python3
"""Path / env resolution for agency scripts (KTD9)."""

from __future__ import annotations

import os
from pathlib import Path


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def package_root() -> Path:
    """Repo / pi-package root (parent of agency/)."""
    return Path(__file__).resolve().parent.parent.parent


def kit_root() -> Path:
    return package_root() / "agency"


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    proj = Path(os.environ.get("AGENCY_PROJECT_ROOT") or Path.cwd()).resolve()
    local = proj / ".pi" / "agency"
    if local.is_dir():
        return local.resolve()
    raise RuntimeError(
        f"No agency root resolved: {proj} has no .pi/agency and AGENCY_ROOT is unset. "
        "Run agency_init in the project, or set AGENCY_ROOT / AGENCY_PROJECT_ROOT."
    )


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
