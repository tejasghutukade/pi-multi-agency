from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import agent_spawn as asp
import pytest


class FakeCtl:
    def __init__(self, root: Path):
        self.root = root

    def require_orchestrator(self, root, *, recovery=False):
        return {"intercomName": "orchestrator"}

    def reconcile_cmux(self, root):
        return {"ok": True}

    def bus_run(self, root, args):
        return {"ok": True}


@pytest.fixture
def spawn_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENCY_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "agents.yaml").write_text(
        """agents:
  scout:
    lifecycleDefault: temporary
    peers: [orchestrator]
  plan:
    lifecycleDefault: persistent
    peers: [orchestrator]
spawn:
  maxSpecialistPanes: 2
  allowPlanTempTwin: true
  allowWorkTwin: false
"""
    )
    (tmp_path / "sessions.json").write_text(json.dumps({"version": 1, "instances": []}) + "\n")
    monkeypatch.setattr(asp, "_ctl", lambda: FakeCtl(tmp_path))
    return tmp_path


def test_reuse_idle(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-told", "role": "scout", "status": "idle", "lifecycle": "temporary"}
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    result = asp.spawn_specialist("scout", reuse=True, dry_run=True)
    assert result["action"] == "reuse"


def test_max_panes(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-t1", "role": "scout", "status": "idle"},
            {"intercomName": "scout-t2", "role": "scout", "status": "idle"},
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="max specialist panes"):
        asp.spawn_specialist("scout", dry_run=True)


def test_dry_run_creates_idle_row(spawn_env: Path):
    result = asp.spawn_specialist("scout", dry_run=True, boot_wait=0, nudge=False)
    assert result["action"] == "spawn-dry-run"
    assert result["instance"]["status"] == "idle"
    data = json.loads((spawn_env / "sessions.json").read_text())
    assert len(data["instances"]) == 1


def test_dual_entry_same_function():
    import specialist_spawn as ss

    assert ss.spawn_specialist is asp.spawn_specialist
