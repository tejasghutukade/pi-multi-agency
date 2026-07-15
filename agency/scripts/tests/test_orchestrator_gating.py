from __future__ import annotations

import json
from pathlib import Path

import agency_ctl as ctl
import pytest

HUB = "orchestrator"
HUB_SURFACE = "surface:hub"
SCOUT_SURFACE = "surface:scout"
OTHER_SURFACE = "surface:other"


def _write_sessions(root: Path, instances: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions.json").write_text(json.dumps({"version": 1, "instances": instances}) + "\n")


def _hub_row(surface: str | None) -> dict:
    return {
        "instanceId": "orchestrator-1",
        "intercomName": HUB,
        "role": HUB,
        "lifecycle": "persistent",
        "status": "idle",
        "cwd": "/tmp",
        "taskId": None,
        "cmuxSurface": surface,
        "cmuxPane": "pane:hub",
    }


def _scout_row(surface: str) -> dict:
    return {
        "instanceId": "scout-1",
        "intercomName": "scout-t1",
        "role": "scout",
        "lifecycle": "temporary",
        "status": "idle",
        "cwd": "/tmp",
        "taskId": None,
        "cmuxSurface": surface,
        "cmuxPane": "pane:scout",
    }


@pytest.fixture
def hub_surface(monkeypatch):
    monkeypatch.setattr(ctl, "caller_surface", lambda: (HUB_SURFACE, "pane:hub"))


@pytest.fixture
def scout_surface(monkeypatch):
    monkeypatch.setattr(ctl, "caller_surface", lambda: (SCOUT_SURFACE, "pane:scout"))


def test_hub_surface_can_claim_orchestrator_first_time(tmp_path: Path, hub_surface):
    """Regression for review finding #3: the orchestrator (HUB) surface may claim
    the orchestrator role on first use."""
    _write_sessions(tmp_path, [_hub_row(None)])
    row = ctl.ensure_orchestrator(tmp_path)
    assert row["intercomName"] == HUB
    assert row["cmuxSurface"] == HUB_SURFACE


def test_non_hub_surface_cannot_claim_orchestrator(tmp_path: Path, scout_surface):
    """A registered non-orchestrator pane must never bootstrap itself as the
    orchestrator."""
    _write_sessions(tmp_path, [_scout_row(SCOUT_SURFACE)])
    with pytest.raises(RuntimeError, match="orchestrator denied"):
        ctl.ensure_orchestrator(tmp_path)


def test_non_hub_surface_cannot_rebind_bound_orchestrator(tmp_path: Path, scout_surface):
    """A specialist pane calling an operator command must not rebind the already
    bound orchestrator to its own surface."""
    _write_sessions(tmp_path, [_hub_row(HUB_SURFACE), _scout_row(SCOUT_SURFACE)])
    with pytest.raises(RuntimeError, match="orchestrator denied|spawn/release denied"):
        ctl.ensure_orchestrator(tmp_path)


def test_hub_surface_can_rebind_its_own_orchestrator(tmp_path: Path, hub_surface):
    """The same HUB surface may refresh its pane binding."""
    _write_sessions(tmp_path, [_hub_row(HUB_SURFACE)])
    row = ctl.ensure_orchestrator(tmp_path)
    assert row["cmuxSurface"] == HUB_SURFACE


def test_other_surface_cannot_hijack_bound_orchestrator(tmp_path: Path, monkeypatch):
    """An entirely different surface (not the HUB role) cannot take over the
    orchestrator binding."""
    monkeypatch.setattr(ctl, "caller_surface", lambda: (OTHER_SURFACE, "pane:other"))
    _write_sessions(tmp_path, [_hub_row(HUB_SURFACE)])
    with pytest.raises(RuntimeError, match="spawn/release denied"):
        ctl.ensure_orchestrator(tmp_path)
