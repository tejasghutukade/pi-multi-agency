from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import agent_spawn as asp
from pi_launch import shell_quote
import pytest


class FakeCtl:
    def __init__(self, root: Path):
        self.root = root
        self.authority_calls = []
        self.stage_role_calls = []

    def require_operation_authority(self, root, *, pipeline_id=None, recovery=False):
        self.authority_calls.append({"pipeline_id": pipeline_id, "recovery": recovery})
        return {"intercomName": "orchestrator"}

    def require_active_pending_stage_role(self, root, pipeline_id, role):
        self.stage_role_calls.append({"pipeline_id": pipeline_id, "role": role})
        return {"id": "stage", "role": role, "status": "pending"}

    def reconcile_cmux(self, root):
        return {"ok": True}

    def bus_run(self, root, args):
        return {"ok": True}


@pytest.fixture
def spawn_env(tmp_path: Path, monkeypatch):
    project = tmp_path / "owner project"
    root = project / ".pi" / "agency"
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENCY_ROOT", str(root))
    monkeypatch.setenv("AGENCY_PROJECT_ROOT", str(project))
    (root / "agents.yaml").write_text(
        """agents:
  scout:
    lifecycleDefault: temporary
    peers: [orchestrator]
  planner:
    lifecycleDefault: persistent
    peers: [orchestrator]
  worker:
    lifecycleDefault: persistent
    peers: [orchestrator]
spawn:
  maxSpecialistPanes: 2
  allowPlanTempTwin: true
  allowWorkTwin: false
"""
    )
    (root / "sessions.json").write_text(json.dumps({"version": 1, "instances": []}) + "\n")
    monkeypatch.setattr(asp, "_ctl", lambda: FakeCtl(root))
    return root


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
    with pytest.raises(RuntimeError, match="max specialist panes"):
        asp.spawn_specialist("scout", dry_run=True, pipeline_id="p-123")


def test_pipeline_authority_still_enforces_worker_sole_writer(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "worker", "role": "worker", "status": "idle"},
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="sole writer"):
        asp.spawn_specialist("worker", dry_run=True, pipeline_id="p-123")


def test_spawn_threads_pipeline_authority_before_policy(spawn_env: Path, monkeypatch):
    fake = FakeCtl(spawn_env)
    monkeypatch.setattr(asp, "_ctl", lambda: fake)
    asp.spawn_specialist("scout", dry_run=True, pipeline_id="p-123")
    assert fake.authority_calls == [{"pipeline_id": "p-123", "recovery": False}]
    assert fake.stage_role_calls == [{"pipeline_id": "p-123", "role": "scout"}]


def test_dry_run_creates_idle_row(spawn_env: Path):
    result = asp.spawn_specialist("scout", dry_run=True, boot_wait=0, nudge=False)
    assert result["action"] == "spawn-dry-run"
    assert result["instance"]["status"] == "idle"
    data = json.loads((spawn_env / "sessions.json").read_text())
    assert len(data["instances"]) == 1


def test_persistent_specialist_uses_canonical_role_name_and_lifecycle(spawn_env: Path):
    result = asp.spawn_specialist("planner", dry_run=True, boot_wait=0)
    assert result["instance"]["intercomName"] == "planner"
    assert result["instance"]["role"] == "planner"
    assert result["instance"]["lifecycle"] == "persistent"


def test_recovery_launch_passes_explicit_surface_gate_override(spawn_env: Path, monkeypatch):
    recoveries: list[bool] = []

    class RecoveryCtl(FakeCtl):
        def require_orchestrator(self, root, *, recovery=False):
            recoveries.append(recovery)
            return {"intercomName": "orchestrator"}

    monkeypatch.setattr(asp, "_ctl", lambda: RecoveryCtl(spawn_env))
    result = asp.spawn_specialist("scout", recovery=True, dry_run=True, boot_wait=0)
    assert result["action"] == "spawn-dry-run"
    assert recoveries == [True]


def test_spawn_nudge_is_noop_compatibility_arg():
    assert asp.spawn_specialist.__kwdefaults__["nudge"] is False
    text = asp.bootstrap_text("worker", None, ".pi/agency/charters/worker.md", None, "/tmp/agency")
    assert "nudge" not in text.lower()
    assert "$BUS" not in text


def test_bootstrap_uses_broker_tools_only(spawn_env: Path):
    text = asp.bootstrap_text(
        "worker",
        None,
        ".pi/agency/charters/worker.md",
        None,
        str(spawn_env),
    )
    assert f"Agency ownership was established before Pi started at {spawn_env}." in text
    assert 'export AGENCY_ROOT=' not in text
    assert 'export BUS=' not in text
    assert f'export MEMORY="{asp.scripts_dir() / "memory.py"}"' in text
    assert 'python3 "$BUS" recv --as work --wait 60 --interval 2' not in text
    assert "agency_report / agency_ask / agency_progress" in text
    assert "$BUS" not in text
    assert "fallback" not in text.lower()


def test_packaged_prompts_do_not_recommend_project_script_paths():
    repo = Path(__file__).resolve().parents[3]
    offenders = []
    needles = (
        "python3 .pi/agency/scripts/",
        'BUS="python3 .pi/agency/scripts/',
        'CTL="python3 .pi/agency/scripts/',
    )
    for base in (repo / "agency", repo / "agents"):
        for path in base.rglob("*"):
            if "scripts/tests" in path.as_posix():
                continue
            if path.is_file() and path.suffix in {".md", ".py", ".sh"}:
                text = path.read_text(errors="ignore")
                if any(needle in text for needle in needles):
                    offenders.append(str(path.relative_to(repo)))
    assert offenders == []


def test_reference_repo_spawn_keeps_owner_context_before_pi(spawn_env: Path, tmp_path: Path, monkeypatch):
    reference = tmp_path / "reference repo"
    reference.mkdir()
    sent: list[str] = []
    monkeypatch.setattr(asp, "open_pane", lambda *args, **kwargs: {"surface": "surface:1", "pane": "pane:1"})
    monkeypatch.setattr(asp, "send_to_surface", lambda _surface, command: sent.append(command))

    result = asp.spawn_specialist("scout", cwd=str(reference), boot_wait=0)
    owner = spawn_env.parent.parent
    assert result["instance"]["cwd"] == str(reference.resolve())
    assert sent[0].startswith(
        f"cd {shell_quote(str(reference.resolve()))} && "
        f"AGENCY_ROOT={shell_quote(str(spawn_env.resolve()))} "
        f"AGENCY_PROJECT_ROOT={shell_quote(str(owner.resolve()))} pi "
    )


def test_managed_guidance_uses_broker_status_not_prompt_time_exports():
    repo = Path(__file__).resolve().parents[3]
    guidance = [
        repo / "skills" / "agency-orchestrator" / "SKILL.md",
        repo / "agency" / "skills" / "orchestrator" / "SKILL.md",
        *[
            repo / "agency" / "charters" / f"{name}.md"
            for name in ("orchestrator", "scout", "brainstorm", "planner", "worker", "debug", "coderev", "docrev")
        ],
    ]
    for path in guidance:
        text = path.read_text()
        assert "/agency-broker-status" in text, path
    for path in guidance[2:]:
        on_each = path.read_text().split("## On each delegation", 1)[-1]
        assert '`export AGENCY_ROOT="<project>/.pi/agency"`' not in on_each, path


def test_agent_spawn_cli_exposes_pipeline_id_for_spawn():
    result = subprocess.run(
        [sys.executable, str(Path(asp.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--pipeline-id" in result.stdout
    assert "--pipeline " not in result.stdout


def test_dual_entry_same_function():
    import specialist_spawn as ss

    assert ss.spawn_specialist is asp.spawn_specialist
