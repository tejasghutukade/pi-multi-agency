from __future__ import annotations

import json
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import bus
import pipeline_runtime as runtime
import pipeline_state
import pytest


PIPELINE_ID = "p-report"
TASK_ID = f"pl-{PIPELINE_ID}-s1"
FINAL_TASK_ID = f"pipe-done-{PIPELINE_ID}"
SENDER = "worker-t1"
SURFACE = "surface:worker"
DEFINITION = {
    "description": "report flow",
    "onFailure": "stop",
    "stages": [
        {
            "id": "work",
            "role": "worker",
            "goal": "Work {topic}",
            "outputs": ["primary"],
            "inputs": [],
        }
    ],
}


def setup_report(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / "agency"
    project = tmp_path / "project"
    root.mkdir()
    project.mkdir()
    (root / "agents.yaml").write_text(
        "agents:\n  worker:\n    lifecycleDefault: temporary\n"
        "  pipeline-runner:\n    lifecycleDefault: temporary\n"
        "spawn:\n  maxSpecialistPanes: 6\n"
    )
    (root / "pipelines.yaml").write_text(
        "pipelines:\n  flow:\n    description: report flow\n    onFailure: stop\n"
        "    stages:\n      - id: work\n        role: worker\n"
        '        goal: "Work {topic}"\n        outputs: [primary]\n        inputs: []\n'
    )
    (root / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "instanceId": "worker-1",
                        "intercomName": SENDER,
                        "role": "worker",
                        "status": "working",
                        "taskId": TASK_ID,
                        "cmuxSurface": SURFACE,
                    },
                    {
                        "instanceId": "orchestrator-1",
                        "intercomName": "orchestrator",
                        "role": "orchestrator",
                        "status": "idle",
                        "taskId": None,
                        "cmuxSurface": "surface:orchestrator",
                    },
                ],
            }
        )
        + "\n"
    )
    pipeline_state.acquire_lock(
        root, pipeline_id=PIPELINE_ID, owner_id="runner", owner_pid=111
    )
    pipeline_state.create_run(
        root,
        pipeline_id=PIPELINE_ID,
        pipeline_name="flow",
        topic="reports",
        definition=DEFINITION,
        lock_owner="runner",
        runner_instance="pipeline-runner-t1",
        runner_surface="surface:runner",
    )
    pipeline_state.record_dispatched(
        root,
        PIPELINE_ID,
        "work",
        lock_owner="runner",
        assigned_instance=SENDER,
    )
    monkeypatch.setattr(runtime.ctl, "caller_surface", lambda: (SURFACE, "pane:worker"))
    monkeypatch.setattr(runtime.ctl, "surface_alive", lambda surface: True)
    return root, project


def valid_payload(path: str = "result.md") -> dict:
    return {
        "status": "succeeded",
        "summary": "completed",
        "artifacts": {"primary": path},
    }


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_ordinary_task_falls_back_without_authentication_or_mutation(tmp_path: Path, monkeypatch):
    root = tmp_path / "agency"
    project = tmp_path / "project"
    root.mkdir()
    project.mkdir()
    before = tree_snapshot(tmp_path)
    monkeypatch.setattr(
        runtime.ctl,
        "caller_surface",
        lambda: pytest.fail("ordinary report fallback must not authenticate"),
    )

    assert runtime.prepare_pipeline_report(
        root,
        project,
        from_instance="ordinary-worker",
        task_id="ordinary-task",
        payload={"output": "legacy prose"},
    ) == {"pipelineOwned": False}
    assert tree_snapshot(tmp_path) == before


def test_happy_structured_report_preflight_and_authenticated_send(tmp_path: Path, monkeypatch):
    root, project = setup_report(tmp_path, monkeypatch)
    (project / "result.md").write_text("done")
    before = tree_snapshot(root)

    prepared = runtime.prepare_pipeline_report(
        root,
        project,
        from_instance=SENDER,
        task_id=TASK_ID,
        payload=valid_payload(),
    )
    assert prepared["pipelineOwned"] is True
    assert prepared["payload"] == valid_payload()
    assert prepared["ownership"]["taskId"] == TASK_ID
    assert tree_snapshot(root) == before
    assert not (root / "inbox").exists()

    def real_bus_run(call_root: Path, args: list[str], timeout: float = 60):
        assert call_root == root
        assert "--require-caller" in args
        assert args[args.index("--task-id") + 1] == TASK_ID
        monkeypatch.setattr(bus, "agency_root", lambda: root)
        monkeypatch.setattr(bus, "caller_surface", lambda: (SURFACE, "pane:worker"))
        monkeypatch.setattr(bus, "surface_alive", lambda surface: True)
        ns = Namespace(
            type=args[args.index("--type") + 1],
            from_name=args[args.index("--from") + 1],
            to=args[args.index("--to") + 1],
            task_id=args[args.index("--task-id") + 1],
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            payload_json=args[args.index("--payload-json") + 1],
            payload_file=None,
            payload_path=None,
            ttl=3600,
            priority="normal",
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
            require_caller=True,
            message_id=None,
        )
        output = StringIO()
        with redirect_stdout(output):
            assert bus.cmd_send(ns) == 0
        return json.loads(output.getvalue())

    monkeypatch.setattr(runtime.ctl, "bus_run", real_bus_run)
    sent = runtime.send_pipeline_report(
        root,
        project,
        from_instance=SENDER,
        task_id=TASK_ID,
        payload=valid_payload(),
    )
    assert sent["bus"]["ok"] is True
    envelope = json.loads(bus.list_pending(root, "orchestrator")[0].read_text())
    assert envelope["taskId"] == TASK_ID
    assert envelope["payload"] == valid_payload()
    assert envelope["senderAuth"] == {
        "instanceId": "worker-1",
        "intercomName": SENDER,
        "surface": SURFACE,
    }


def test_needs_attention_stage_accepts_a_corrected_report(tmp_path: Path, monkeypatch):
    root, project = setup_report(tmp_path, monkeypatch)
    (project / "result.md").write_text("corrected")
    pipeline_state.transition_stage(
        root,
        PIPELINE_ID,
        "work",
        "needs_attention",
        lock_owner="runner",
        error="first report was malformed",
    )

    prepared = runtime.prepare_pipeline_report(
        root,
        project,
        from_instance=SENDER,
        task_id=TASK_ID,
        payload=valid_payload(),
    )
    assert prepared["pipelineOwned"] is True
    assert prepared["ownership"]["stageStatus"] == "needs_attention"


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"status": "succeeded"}, "missing or unsupported"),
        ({"output": "prose only"}, "missing or unsupported"),
        (
            {**valid_payload(), "unknown": "field"},
            "missing or unsupported",
        ),
    ],
)
def test_pipeline_report_rejects_missing_prose_only_and_unknown_fields(
    tmp_path: Path, monkeypatch, payload: dict, match: str
):
    root, project = setup_report(tmp_path, monkeypatch)
    (project / "result.md").write_text("done")
    with pytest.raises(runtime.pipeline_runner.InvalidStageReport, match=match):
        runtime.prepare_pipeline_report(
            root,
            project,
            from_instance=SENDER,
            task_id=TASK_ID,
            payload=payload,
        )


def test_pipeline_report_rejects_wrong_sender_surface_row_task_and_final_id(
    tmp_path: Path, monkeypatch
):
    root, project = setup_report(tmp_path, monkeypatch)
    (project / "result.md").write_text("done")

    with pytest.raises(runtime.PipelineRuntimeError, match="sender mismatch"):
        runtime.prepare_pipeline_report(
            root, project, from_instance="worker-t2", task_id=TASK_ID, payload=valid_payload()
        )

    monkeypatch.setattr(
        runtime.ctl, "caller_surface", lambda: ("surface:orchestrator", "pane:orchestrator")
    )
    with pytest.raises(runtime.PipelineRuntimeError, match="belongs to another sender"):
        runtime.prepare_pipeline_report(
            root, project, from_instance=SENDER, task_id=TASK_ID, payload=valid_payload()
        )

    monkeypatch.setattr(runtime.ctl, "caller_surface", lambda: (SURFACE, "pane:worker"))
    sessions = json.loads((root / "sessions.json").read_text())
    sessions["instances"][0]["taskId"] = "other-task"
    (root / "sessions.json").write_text(json.dumps(sessions) + "\n")
    with pytest.raises(runtime.PipelineRuntimeError, match="row taskId"):
        runtime.prepare_pipeline_report(
            root, project, from_instance=SENDER, task_id=TASK_ID, payload=valid_payload()
        )

    with pytest.raises(runtime.PipelineRuntimeError, match="final pipeline task IDs"):
        runtime.prepare_pipeline_report(
            root,
            project,
            from_instance="pipeline-runner-t1",
            task_id=FINAL_TASK_ID,
            payload=valid_payload(),
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "status": "succeeded",
                "summary": "done",
                "artifacts": {"primary": "result.md", "extra": "result.md"},
            },
            "undeclared artifact",
        ),
        (
            {"status": "succeeded", "summary": "done", "artifacts": {}},
            "every declared artifact",
        ),
        (valid_payload("missing.md"), "does not exist"),
        (valid_payload("../outside.md"), "must not contain '..'"),
    ],
)
def test_pipeline_report_rejects_undeclared_missing_and_escaping_artifacts(
    tmp_path: Path, monkeypatch, payload: dict, match: str
):
    root, project = setup_report(tmp_path, monkeypatch)
    (project / "result.md").write_text("done")
    (tmp_path / "outside.md").write_text("outside")
    with pytest.raises(Exception, match=match):
        runtime.prepare_pipeline_report(
            root,
            project,
            from_instance=SENDER,
            task_id=TASK_ID,
            payload=payload,
        )
