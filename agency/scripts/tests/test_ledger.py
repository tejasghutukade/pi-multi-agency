from __future__ import annotations

import json
from pathlib import Path

import agency_paths
import ledger


def test_ledger_round_trip(tmp_path: Path):
    data = ledger.load_sessions(tmp_path)
    assert data["instances"] == []
    inst = {
        "intercomName": "scout-t01",
        "instanceId": "id-1",
        "role": "scout",
        "status": "idle",
        "taskId": "t1",
        "cmuxSurface": "surface:1",
    }
    data["instances"].append(inst)
    ledger.save_sessions(tmp_path, data)
    loaded = ledger.load_sessions(tmp_path)
    assert ledger.find_instance(loaded, "scout-t01") == inst
    assert ledger.find_instance_by_task(loaded, "t1") == inst
    assert ledger.find_by_surface(loaded, "surface:1") == inst
    assert ledger.find_idle_role(loaded, "scout") == inst
    assert ledger.specialist_count(loaded) == 1


def test_load_sessions_retries_transient_empty(tmp_path: Path, monkeypatch):
    """Race: concurrent write_text can leave a brief empty file → JSONDecodeError."""
    path = tmp_path / "sessions.json"
    path.write_text('{"version": 1, "instances": []}\n')
    reads: list[int] = []
    real = Path.read_text

    def flaky(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        if self.name == "sessions.json":
            reads.append(1)
            if len(reads) == 1:
                return ""
        return text

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(ledger.time, "sleep", lambda _s: None)
    data = ledger.load_sessions(tmp_path)
    assert data["instances"] == []
    assert len(reads) >= 2


def test_save_sessions_atomic_leaves_no_tmp(tmp_path: Path):
    ledger.save_sessions(tmp_path, {"version": 1, "instances": [{"intercomName": "orchestrator"}]})
    assert not list(tmp_path.glob("sessions.json.*.tmp"))
    assert not list(tmp_path.glob(".sessions.json*.tmp"))
    assert json.loads((tmp_path / "sessions.json").read_text())["instances"][0]["intercomName"] == "orchestrator"


def test_ledger_clear_and_empty_finds():
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-t01", "instanceId": "a", "role": "scout", "status": "working"},
            {"intercomName": "orchestrator", "instanceId": "h", "role": "orchestrator", "status": "idle"},
        ],
    }
    ledger.clear_instance(data, data["instances"][0])
    assert ledger.find_instance(data, "scout-t01") is None
    assert ledger.specialist_count(data) == 0
    assert ledger.find_idle_role(data, "scout") is None
    assert ledger.find_instance_by_task(data, "missing") is None


def test_ledger_make_instance_name(monkeypatch):
    monkeypatch.setattr(ledger.secrets, "token_hex", lambda n: "ab")
    assert ledger.make_instance_name("scout", "temporary") == "scout-tab"
    assert ledger.make_instance_name("plan", "persistent") == "plan"


def test_ledger_and_paths_do_not_import_agency_ctl():
    for mod in (agency_paths, ledger):
        src = Path(mod.__file__).read_text()
        assert "import agency_ctl" not in src
        assert "from agency_ctl" not in src
