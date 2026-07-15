from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import agency_ctl as ctl
import pipeline_state
import pytest


PIPELINE = {
    "description": "one stage",
    "onFailure": "stop",
    "stages": [
        {
            "id": "scout",
            "role": "scout",
            "goal": "Scout {topic}",
            "outputs": ["primary"],
            "inputs": [],
        },
        {
            "id": "implement",
            "role": "worker",
            "goal": "Implement {topic}",
            "outputs": ["primary"],
            "inputs": [{"stage": "scout", "artifacts": ["primary"]}],
        },
    ],
}
PIPELINE_ID = "p-123"
RUNNER = "pipeline-runner-t1"
SURFACE = "surface:runner"


def _write_authorized_state(root: Path, *, status: str = "working") -> None:
    (root / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "instanceId": "runner-1",
                        "intercomName": RUNNER,
                        "role": "pipeline-runner",
                        "status": status,
                        "activePipelineId": PIPELINE_ID,
                        "cmuxSurface": SURFACE,
                    }
                ],
            }
        )
        + "\n"
    )
    pipeline_state.acquire_lock(
        root,
        pipeline_id=PIPELINE_ID,
        owner_id=RUNNER,
        owner_pid=123,
        owner_surface=SURFACE,
    )
    pipeline_state.create_run(
        root,
        pipeline_id=PIPELINE_ID,
        pipeline_name="implementation",
        topic="authority",
        definition=PIPELINE,
        lock_owner=RUNNER,
    )
    pipeline_state.bind_runner(
        root,
        PIPELINE_ID,
        lock_owner=RUNNER,
        runner_instance=RUNNER,
        runner_surface=SURFACE,
    )


@pytest.fixture
def authorized(tmp_path: Path, monkeypatch):
    _write_authorized_state(tmp_path)
    monkeypatch.setattr(ctl, "caller_surface", lambda: (SURFACE, "pane:runner"))
    monkeypatch.setattr(ctl, "surface_alive", lambda surface: True)
    monkeypatch.setattr(ctl, "process_alive", lambda pid: True)
    return tmp_path


def test_bound_live_runner_has_pipeline_authority(authorized: Path):
    row = ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)
    assert row["intercomName"] == RUNNER


def test_missing_pipeline_id_is_denied(authorized: Path):
    with pytest.raises(RuntimeError, match="pipeline ID is required"):
        ctl.require_pipeline_runner_authority(authorized, "")


def test_unregistered_surface_is_denied(authorized: Path, monkeypatch):
    monkeypatch.setattr(ctl, "caller_surface", lambda: ("surface:other", "pane:other"))
    with pytest.raises(RuntimeError, match="unregistered caller surface"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_duplicate_surface_rows_are_denied(authorized: Path):
    data = json.loads((authorized / "sessions.json").read_text())
    duplicate = dict(data["instances"][0])
    duplicate["instanceId"] = "runner-duplicate"
    data["instances"].append(duplicate)
    (authorized / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="ambiguous caller surface"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_wrong_role_is_denied(authorized: Path):
    data = json.loads((authorized / "sessions.json").read_text())
    data["instances"][0]["role"] = "scout"
    (authorized / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="role.*pipeline-runner"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_wrong_pipeline_row_is_denied(authorized: Path):
    data = json.loads((authorized / "sessions.json").read_text())
    data["instances"][0]["activePipelineId"] = "p-other"
    (authorized / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="session pipeline mismatch"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_missing_intercom_name_is_denied(authorized: Path):
    data = json.loads((authorized / "sessions.json").read_text())
    data["instances"][0]["intercomName"] = None
    (authorized / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="runner intercom name is missing"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


@pytest.mark.parametrize("status", ["starting", "failed", None])
def test_stale_runner_status_is_denied(authorized: Path, status):
    data = json.loads((authorized / "sessions.json").read_text())
    data["instances"][0]["status"] = status
    (authorized / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="inactive runner status"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


@pytest.mark.parametrize("alive", [False, None])
def test_dead_or_unknown_surface_is_denied(authorized: Path, monkeypatch, alive):
    monkeypatch.setattr(ctl, "surface_alive", lambda surface: alive)
    with pytest.raises(RuntimeError, match="runner surface is not confirmed alive"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_missing_binding_is_denied(tmp_path: Path, monkeypatch):
    _write_authorized_state(tmp_path)
    data = pipeline_state.load_state(tmp_path)
    data["runs"][0]["runnerInstance"] = None
    data["runs"][0]["runnerSurface"] = None
    pipeline_state.save_state(tmp_path, data)
    monkeypatch.setattr(ctl, "caller_surface", lambda: (SURFACE, "pane:runner"))
    monkeypatch.setattr(ctl, "surface_alive", lambda surface: True)
    with pytest.raises(RuntimeError, match="active runner binding is missing"):
        ctl.require_pipeline_runner_authority(tmp_path, PIPELINE_ID)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pipelineId", "p-other", "binding pipeline mismatch"),
        ("runnerInstance", "pipeline-runner-t2", "binding instance mismatch"),
        ("runnerSurface", "surface:other", "binding surface mismatch"),
    ],
)
def test_binding_mismatch_is_denied(authorized: Path, monkeypatch, field, value, message):
    binding = pipeline_state.get_active_runner_binding(authorized)
    assert binding is not None
    binding[field] = value
    monkeypatch.setattr(pipeline_state, "get_active_runner_binding", lambda root: binding)
    with pytest.raises(RuntimeError, match=message):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pipelineId", "p-other", "lock pipeline mismatch"),
        ("ownerId", "pipeline-runner-t2", "lock owner mismatch"),
    ],
)
def test_lock_mismatch_is_denied(authorized: Path, monkeypatch, field, value, message):
    lock = pipeline_state.read_lock(authorized)
    assert lock is not None
    lock[field] = value
    monkeypatch.setattr(pipeline_state, "read_lock", lambda root: lock)
    with pytest.raises(RuntimeError, match=message):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_missing_lock_is_denied(authorized: Path, monkeypatch):
    monkeypatch.setattr(pipeline_state, "read_lock", lambda root: None)
    with pytest.raises(RuntimeError, match="pipeline lock is missing"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_wrong_lock_surface_is_denied(authorized: Path, monkeypatch):
    lock = pipeline_state.read_lock(authorized)
    assert lock is not None
    lock["ownerSurface"] = "surface:other"
    monkeypatch.setattr(pipeline_state, "read_lock", lambda root: lock)
    with pytest.raises(RuntimeError, match="lock surface mismatch"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_dead_lock_owner_pid_is_denied(authorized: Path, monkeypatch):
    monkeypatch.setattr(ctl, "process_alive", lambda pid: False)
    with pytest.raises(RuntimeError, match="lock owner PID is not confirmed alive"):
        ctl.require_pipeline_runner_authority(authorized, PIPELINE_ID)


def test_process_alive_probes_pid_and_fails_closed(monkeypatch):
    probed = []
    monkeypatch.setattr(ctl.os, "kill", lambda pid, signal: probed.append((pid, signal)))
    assert ctl.process_alive(123) is True
    assert probed == [(123, 0)]
    assert ctl.process_alive(0) is False

    def dead(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr(ctl.os, "kill", dead)
    assert ctl.process_alive(123) is False


def test_authority_selector_preserves_orchestrator_and_recovery(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(
        ctl,
        "require_orchestrator",
        lambda root, recovery=False: calls.append((root, recovery)) or {"role": "orchestrator"},
    )
    monkeypatch.setattr(
        ctl,
        "require_pipeline_runner_authority",
        lambda root, pipeline_id: calls.append((root, pipeline_id)) or {"role": "pipeline-runner"},
    )

    assert ctl.require_operation_authority(tmp_path)["role"] == "orchestrator"
    assert ctl.require_operation_authority(tmp_path, recovery=True)["role"] == "orchestrator"
    assert ctl.require_operation_authority(tmp_path, pipeline_id=PIPELINE_ID, recovery=True)["role"] == "pipeline-runner"
    assert calls == [(tmp_path, False), (tmp_path, True), (tmp_path, PIPELINE_ID)]


def _dispatch_current(root: Path, target: str = "scout-t1") -> str:
    pipeline_state.record_dispatched(
        root,
        PIPELINE_ID,
        "scout",
        lock_owner=RUNNER,
        assigned_instance=target,
    )
    task_id = "pl-p-123-s1"
    data = json.loads((root / "sessions.json").read_text())
    data["instances"].append(
        {
            "instanceId": "scout-1",
            "intercomName": target,
            "role": "scout",
            "status": "idle",
            "taskId": task_id,
            "cmuxSurface": "surface:scout",
        }
    )
    (root / "sessions.json").write_text(json.dumps(data) + "\n")
    return task_id


def test_spawn_role_is_scoped_to_current_pending_stage(authorized: Path):
    stage = ctl.require_active_pending_stage_role(authorized, PIPELINE_ID, "scout")
    assert stage["id"] == "scout"
    with pytest.raises(RuntimeError, match="role does not match current stage"):
        ctl.require_active_pending_stage_role(authorized, PIPELINE_ID, "worker")


def test_dispatched_stage_requires_exact_current_task_and_target(authorized: Path):
    with pytest.raises(RuntimeError, match="not active pipeline-owned"):
        ctl.require_active_dispatched_stage(authorized, PIPELINE_ID, "pl-p-123")
    with pytest.raises(RuntimeError, match="not active pipeline-owned"):
        ctl.require_active_dispatched_stage(authorized, PIPELINE_ID, "unknown")
    with pytest.raises(RuntimeError, match="not a stage task"):
        ctl.require_active_dispatched_stage(authorized, PIPELINE_ID, "pipe-done-p-123")
    with pytest.raises(RuntimeError, match="does not belong to the current stage"):
        ctl.require_active_dispatched_stage(authorized, PIPELINE_ID, "pl-p-123-s2")

    task_id = _dispatch_current(authorized)
    ownership = ctl.require_active_dispatched_stage(authorized, PIPELINE_ID, task_id)
    assert ownership["expectedSender"] == "scout-t1"
    with pytest.raises(RuntimeError, match="target does not match"):
        ctl.require_active_dispatched_stage(
            authorized,
            PIPELINE_ID,
            task_id,
            expected_sender="scout-t2",
        )


def test_delegate_and_wait_use_exact_dispatched_ownership(authorized: Path, monkeypatch, capsys):
    task_id = _dispatch_current(authorized)
    monkeypatch.setattr(ctl, "agency_root", lambda: authorized)
    monkeypatch.setattr(ctl, "load_agents", lambda root: {"agents": {"scout": {}}})
    delegate = Namespace(
        pipeline_id=PIPELINE_ID,
        recovery=False,
        to="scout-t1",
        task_id=task_id,
        workflow_id=None,
        payload_json='{"goal": "x"}',
        prepare_only=True,
    )
    assert ctl.cmd_delegate(delegate) == 0

    bus_calls = []
    monkeypatch.setattr(
        ctl,
        "bus_run",
        lambda root, args, timeout: bus_calls.append(args) or {"ok": True, "status": "timeout"},
    )
    wait = Namespace(
        pipeline_id=PIPELINE_ID,
        task_id=task_id,
        timeout=0,
        interval=0,
        as_name=ctl.HUB,
        auto_done_progress=True,
    )
    assert ctl.cmd_wait(wait) == 0
    assert "--from" in bus_calls[0]
    assert bus_calls[0][bus_calls[0].index("--from") + 1] == "scout-t1"
    capsys.readouterr()


def test_pipeline_delegate_denies_wrong_target_before_mutation(authorized: Path, monkeypatch):
    task_id = _dispatch_current(authorized)
    monkeypatch.setattr(ctl, "agency_root", lambda: authorized)
    monkeypatch.setattr(ctl, "save_sessions", lambda *args: pytest.fail("ledger mutated"))
    args = Namespace(
        pipeline_id=PIPELINE_ID,
        recovery=True,
        to="scout-t2",
        task_id=task_id,
    )
    with pytest.raises(RuntimeError, match="target does not match"):
        ctl.cmd_delegate(args)


def test_pipeline_wait_denies_alternate_recipient_and_unknown_task(authorized: Path, monkeypatch):
    task_id = _dispatch_current(authorized)
    monkeypatch.setattr(ctl, "agency_root", lambda: authorized)
    alternate = Namespace(pipeline_id=PIPELINE_ID, as_name=RUNNER, task_id=task_id)
    with pytest.raises(RuntimeError, match="only as orchestrator"):
        ctl.cmd_wait(alternate)

    unknown = Namespace(pipeline_id=PIPELINE_ID, as_name=ctl.HUB, task_id="pl-p-123")
    with pytest.raises(RuntimeError, match="not active pipeline-owned"):
        ctl.cmd_wait(unknown)


def test_pipeline_id_parser_surface_is_limited_to_spawn_delegate_wait():
    script = Path(ctl.__file__)
    for command in ("spawn", "delegate", "wait"):
        result = subprocess.run(
            [sys.executable, str(script), command, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "--pipeline-id" in result.stdout
        assert "--pipeline " not in result.stdout

    result = subprocess.run(
        [sys.executable, str(script), "release", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--pipeline-id" not in result.stdout
