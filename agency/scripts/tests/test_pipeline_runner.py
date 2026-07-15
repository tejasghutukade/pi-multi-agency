from __future__ import annotations

import inspect
import json
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any

import pytest

import agency_ctl as ctl
import ledger
import pipeline_runtime
import pipeline_runner as runner
import pipeline_state as state


OWNER = "pipeline-runner-t1"
PIPELINE_ID = "p-123"


FOUR_STAGE = {
    "description": "four stages",
    "onFailure": "stop",
    "stages": [
        {
            "id": "scout",
            "role": "scout",
            "goal": "Scout {topic}",
            "outputs": ["primary", "notes"],
            "inputs": [],
        },
        {
            "id": "plan",
            "role": "planner",
            "goal": "Plan {topic}",
            "outputs": ["primary"],
            "inputs": [{"stage": "scout", "artifacts": ["notes", "primary"]}],
        },
        {
            "id": "implement",
            "role": "worker",
            "goal": "Implement {topic}",
            "outputs": ["primary"],
            "inputs": [{"stage": "plan", "artifacts": ["primary"]}],
        },
        {
            "id": "review",
            "role": "coderev",
            "goal": "Review {topic}",
            "outputs": ["primary"],
            "inputs": [{"stage": "implement", "artifacts": ["primary"]}],
        },
    ],
}


class FakeControlPlane:
    def __init__(self, reports: dict[str, dict[str, Any]] | None = None):
        self.reports = reports or {}
        self.events: list[tuple] = []
        self.payloads: dict[str, dict[str, Any]] = {}
        self.wait_status: dict[str, str] = {}
        self.reserve_error: str | None = None
        self.spawn_error: str | None = None
        self.delegate_error: str | None = None
        self.find_calls = 0

    def reserve_stage_instance(self, *, pipeline_id: str, role: str, task_id: str) -> runner.SpawnResult:
        self.events.append(("reserve", role, task_id))
        if self.reserve_error:
            raise RuntimeError(self.reserve_error)
        number = sum(event[0] == "reserve" for event in self.events)
        return runner.SpawnResult(f"{role}-t{number}")

    def spawn_stage(self, *, pipeline_id: str, role: str, task_id: str, instance: str) -> None:
        self.events.append(("spawn", role, task_id, instance))
        if self.spawn_error:
            raise RuntimeError(self.spawn_error)

    def delegate_stage(self, *, pipeline_id, instance, task_id, payload) -> None:
        self.events.append(("delegate", task_id, instance))
        self.payloads[task_id] = dict(payload)
        if self.delegate_error:
            raise RuntimeError(self.delegate_error)

    def wait_stage(self, *, pipeline_id, task_id, expected_sender, timeout) -> runner.WaitResult:
        self.events.append(("wait", task_id, expected_sender))
        status = self.wait_status.get(task_id, "report")
        if status != "report":
            return runner.WaitResult(status, detail=f"scripted {status}")
        return runner.WaitResult("report", self.reports[task_id])

    def find_existing_report(self, *, pipeline_id, task_id, expected_sender):
        self.find_calls += 1
        self.events.append(("find", task_id, expected_sender))
        return self.reports.get(task_id)

    def ack_stage_report(self, receipt: str) -> None:
        self.events.append(("ack", receipt))

    def surface_alive(self, instance: str) -> bool:
        # Default: prior instances are reusable; tests can override.
        return True


def stage_definition(stage_id: str = "only", role: str = "scout") -> dict[str, Any]:
    return {
        "description": "one stage",
        "onFailure": "stop",
        "stages": [
            {
                "id": stage_id,
                "role": role,
                "goal": "Do {topic}",
                "outputs": ["primary"],
                "inputs": [],
            }
        ],
    }


def write_catalog(agency: Path, definition: dict[str, Any]) -> None:
    lines = [
        "pipelines:",
        "  flow:",
        f"    description: {json.dumps(definition.get('description', ''))}",
        f"    onFailure: {definition['onFailure']}",
        "    stages:",
    ]
    for stage in definition["stages"]:
        lines.extend(
            [
                f"      - id: {stage['id']}",
                f"        role: {stage['role']}",
                f"        goal: {json.dumps(stage['goal'])}",
                f"        outputs: [{', '.join(stage['outputs'])}]",
            ]
        )
        if not stage["inputs"]:
            lines.append("        inputs: []")
        else:
            lines.append("        inputs:")
            for selector in stage["inputs"]:
                lines.extend(
                    [
                        f"          - stage: {selector['stage']}",
                        f"            artifacts: [{', '.join(selector['artifacts'])}]",
                    ]
                )
    (agency / "pipelines.yaml").write_text("\n".join(lines) + "\n")


def setup_run(tmp_path: Path, definition: dict[str, Any]) -> tuple[Path, Path]:
    agency = tmp_path / "agency"
    project = tmp_path / "project"
    agency.mkdir()
    project.mkdir()
    roles = sorted({stage["role"] for stage in definition["stages"]} | {"pipeline-runner"})
    agent_lines = ["agents:"]
    for role in roles:
        agent_lines.extend([f"  {role}:", "    peers: []"])
    agent_lines.extend(["spawn:", "  maxSpecialistPanes: 6"])
    (agency / "agents.yaml").write_text("\n".join(agent_lines) + "\n")
    write_catalog(agency, definition)
    state.acquire_lock(
        agency,
        pipeline_id=PIPELINE_ID,
        owner_id=OWNER,
        owner_pid=123,
        owner_surface="surface:runner",
    )
    state.create_run(
        agency,
        pipeline_id=PIPELINE_ID,
        pipeline_name="flow",
        topic="determinism",
        definition=definition,
        lock_owner=OWNER,
    )
    state.bind_runner(
        agency,
        PIPELINE_ID,
        lock_owner=OWNER,
        runner_instance=OWNER,
        runner_surface="surface:runner",
    )
    return agency, project


def write_artifact(project: Path, relative: str) -> str:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relative)
    return relative


def report(task_id: str, sender: str, artifacts: dict[str, str], **payload_changes) -> dict[str, Any]:
    payload = {"status": "succeeded", "summary": f"finished {task_id}", "artifacts": artifacts}
    payload.update(payload_changes)
    return {
        "type": "report",
        "taskId": task_id,
        "from": sender,
        "to": "orchestrator",
        "payload": payload,
    }


def scripted_success(project: Path, definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reports = {}
    for index, stage in enumerate(definition["stages"], 1):
        task = f"pl-{PIPELINE_ID}-s{index}"
        sender = f"{stage['role']}-t{index}"
        artifacts = {
            output: write_artifact(project, f"artifacts/{stage['id']}-{output}.md")
            for output in stage["outputs"]
        }
        reports[task] = report(task, sender, artifacts)
    return reports


def test_four_stage_order_named_input_order_payload_and_synthesis(tmp_path: Path, monkeypatch):
    agency, project = setup_run(tmp_path, FOUR_STAGE)
    fake = FakeControlPlane(scripted_success(project, FOUR_STAGE))
    events = fake.events
    real_record = state.record_dispatched

    def record(*args, **kwargs):
        events.append(("record_dispatched", args[2]))
        return real_record(*args, **kwargs)

    monkeypatch.setattr(state, "record_dispatched", record)
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)

    assert [event[0] for event in events] == [
        item
        for _stage in FOUR_STAGE["stages"]
        for item in ("reserve", "record_dispatched", "spawn", "delegate", "wait")
    ]
    assert events[0] == ("reserve", "scout", f"pl-{PIPELINE_ID}-s1")
    assert events[2] == ("spawn", "scout", f"pl-{PIPELINE_ID}-s1", "scout-t1")
    plan_payload = fake.payloads[f"pl-{PIPELINE_ID}-s2"]
    assert list(plan_payload["namedInputs"]) == ["scout.notes", "scout.primary"]
    assert plan_payload["contextPaths"] == [
        "artifacts/scout-notes.md",
        "artifacts/scout-primary.md",
    ]
    assert plan_payload["goal"] == "Plan determinism"
    assert plan_payload["pipeline"] == {
        "pipelineId": PIPELINE_ID,
        "stageId": "plan",
        "taskId": f"pl-{PIPELINE_ID}-s2",
    }
    assert plan_payload["expectedOutputs"] == ["primary"]
    assert "resultContract" in plan_payload
    assert synthesis["status"] == "succeeded"
    assert [stage["id"] for stage in synthesis["stages"]] == ["scout", "plan", "implement", "review"]
    assert synthesis["summary"] == f"finished pl-{PIPELINE_ID}-s4"
    assert list(synthesis["artifacts"]) == ["scout", "plan", "implement", "review"]


@pytest.mark.parametrize("field", ["goal", "outputs", "inputs", "role"])
def test_operational_catalog_drift_halts_before_spawn(tmp_path: Path, field: str):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    changed = json.loads(json.dumps(definition))
    if field == "goal":
        changed["stages"][0][field] = "Changed {topic}"
    elif field == "outputs":
        changed["stages"][0][field] = ["other"]
    elif field == "inputs":
        changed["stages"].insert(
            0,
            {"id": "source", "role": "scout", "goal": "Source", "outputs": ["primary"], "inputs": []},
        )
        changed["stages"][1][field] = [{"stage": "source", "artifacts": ["primary"]}]
    else:
        changed["stages"][0][field] = "pipeline-runner"
    write_catalog(agency, changed)
    fake = FakeControlPlane()
    result = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert result["status"] == "needs_attention"
    assert not fake.events
    # Review finding #7: catalog-drift reason lives on `question`, `error` stays None.
    assert "catalog" in result["stages"][0]["question"]
    assert result["stages"][0]["error"] is None


def test_stored_definition_digest_mismatch_halts_before_spawn(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    data = state.load_state(agency)
    data["runs"][0]["definitionDigest"] = "0" * 64
    state.save_state(agency, data)
    fake = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert synthesis["status"] == "needs_attention"
    # Review finding #7: digest-mismatch reason lives on `question`, `error` stays None.
    assert "digest" in synthesis["stages"][0]["question"]
    assert synthesis["stages"][0]["error"] is None
    assert not fake.events


def test_wrong_lock_owner_fails_closed_without_mutating_state(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    fake = FakeControlPlane()
    with pytest.raises(runner.PipelineRunnerError, match="lock owner"):
        runner.run_pipeline(agency, project, PIPELINE_ID, "wrong-owner", fake)
    assert state.get_run(agency, PIPELINE_ID)["stages"][0]["status"] == "pending"
    assert not fake.events


def test_description_drift_is_not_operational(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    definition["description"] = "new prose"
    write_catalog(agency, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    fake = FakeControlPlane({task: report(task, "scout-t1", {"primary": write_artifact(project, "out.md")})})
    assert runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)["status"] == "succeeded"


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"type": "ask"}, "type"),
        ({"taskId": "wrong"}, "taskId"),
        ({"from": "wrong"}, "sender"),
        ({"to": "wrong"}, "recipient"),
    ],
)
def test_report_identity_validation(tmp_path: Path, change: dict[str, Any], match: str):
    project = tmp_path
    write_artifact(project, "out.md")
    envelope = report("task", "scout-t1", {"primary": "out.md"})
    envelope.update(change)
    with pytest.raises(runner.InvalidStageReport, match=match):
        runner.validate_stage_report(
            envelope=envelope,
            expected_task_id="task",
            expected_sender="scout-t1",
            declared_outputs=["primary"],
            project_root=project,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unknown", "summary": "x", "artifacts": {}},
        {"status": "succeeded", "summary": "", "artifacts": {"primary": "out.md"}},
        {"status": "succeeded", "summary": "   ", "artifacts": {"primary": "out.md"}},
        {"status": "succeeded", "summary": "x", "artifacts": {}, "extra": True},
        {"status": "succeeded", "summary": "x", "artifacts": {"extra": "out.md"}},
        {"status": "succeeded", "summary": "x", "artifacts": {"primary": "out.md"}, "error": "bad"},
        {"status": "failed", "summary": "x", "artifacts": {}},
        {"status": "failed", "summary": "x", "artifacts": {}, "error": "  "},
    ],
)
def test_report_schema_validation(tmp_path: Path, payload: dict[str, Any]):
    write_artifact(tmp_path, "out.md")
    envelope = report("task", "scout-t1", {})
    envelope["payload"] = payload
    with pytest.raises(runner.InvalidStageReport):
        runner.validate_stage_report(
            envelope=envelope,
            expected_task_id="task",
            expected_sender="scout-t1",
            declared_outputs=["primary"],
            project_root=tmp_path,
        )


def test_failed_report_allows_valid_declared_subset(tmp_path: Path):
    write_artifact(tmp_path, "partial.md")
    validated = runner.validate_stage_report(
        envelope=report(
            "task",
            "worker-t1",
            {"notes": "partial.md"},
            status="failed",
            summary="partial",
            error="boom",
        ),
        expected_task_id="task",
        expected_sender="worker-t1",
        declared_outputs=["primary", "notes"],
        project_root=tmp_path,
    )
    assert validated == {
        "status": "failed",
        "summary": "partial",
        "artifacts": {"notes": "partial.md"},
        "error": "boom",
        "question": None,
        "options": None,
    }


@pytest.mark.parametrize("value", ["/absolute", "../escape", "missing.md", ".", "bad\x00path"])
def test_artifact_path_rejects_unsafe_or_missing_values(tmp_path: Path, value: str):
    with pytest.raises(runner.InvalidArtifactPath):
        runner.validate_artifact_path(tmp_path, value)


def test_artifact_path_accepts_files_directories_and_rejects_symlink_escape(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    write_artifact(project, "inside/file.md")
    assert runner.validate_artifact_path(project, "inside/file.md") == "inside/file.md"
    assert runner.validate_artifact_path(project, "inside") == "inside"
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (project / "link.md").symlink_to(outside)
    with pytest.raises(runner.InvalidArtifactPath, match="escapes"):
        runner.validate_artifact_path(project, "link.md")


def test_stop_failure_leaves_later_pending(tmp_path: Path):
    agency, project = setup_run(tmp_path, FOUR_STAGE)
    task = f"pl-{PIPELINE_ID}-s1"
    fake = FakeControlPlane({task: report(task, "scout-t1", {}, status="failed", error="nope")})
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert synthesis["status"] == "failed"
    assert [stage["status"] for stage in synthesis["stages"]] == ["failed", "pending", "pending", "pending"]
    assert [event[0] for event in fake.events] == ["reserve", "spawn", "delegate", "wait"]


def test_continue_marks_direct_transitive_dependencies_and_runs_independent(tmp_path: Path):
    definition = {
        "description": "dependency propagation",
        "onFailure": "continue",
        "stages": [
            {"id": "a", "role": "scout", "goal": "A", "outputs": ["primary"], "inputs": []},
            {"id": "b", "role": "planner", "goal": "B", "outputs": ["primary"], "inputs": [{"stage": "a", "artifacts": ["primary"]}]},
            {"id": "c", "role": "worker", "goal": "C", "outputs": ["primary"], "inputs": [{"stage": "b", "artifacts": ["primary"]}]},
            {"id": "d", "role": "coderev", "goal": "D", "outputs": ["primary"], "inputs": []},
        ],
    }
    agency, project = setup_run(tmp_path, definition)
    task_a, task_d = f"pl-{PIPELINE_ID}-s1", f"pl-{PIPELINE_ID}-s4"
    fake = FakeControlPlane(
        {
            task_a: report(task_a, "scout-t1", {}, status="failed", error="failed a"),
            task_d: report(task_d, "coderev-t2", {"primary": write_artifact(project, "d.md")}),
        }
    )
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert [stage["status"] for stage in synthesis["stages"]] == [
        "failed", "dependency_failed", "dependency_failed", "succeeded"
    ]
    assert [event[1] for event in fake.events if event[0] == "spawn"] == ["scout", "coderev"]
    assert synthesis["status"] == "failed"


@pytest.mark.parametrize("uncertainty", ["reserve", "spawn", "timeout", "pane_dead", "delegate"])
def test_execution_uncertainty_needs_attention(tmp_path: Path, uncertainty: str):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    fake = FakeControlPlane({task: report(task, "scout-t1", {"primary": write_artifact(project, "out.md")})})
    if uncertainty == "reserve":
        fake.reserve_error = "could not reserve"
    elif uncertainty == "spawn":
        fake.spawn_error = "spawn outcome unknown"
    elif uncertainty == "delegate":
        fake.delegate_error = "maybe delivered"
    else:
        fake.wait_status[task] = uncertainty
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert synthesis["status"] == "needs_attention"
    assert synthesis["stages"][0]["status"] == "needs_attention"
    expected_delegates = 0 if uncertainty in {"reserve", "spawn"} else 1
    assert len([event for event in fake.events if event[0] == "delegate"]) == expected_delegates
    if uncertainty == "reserve":
        assert synthesis["stages"][0]["instance"] is None
    else:
        assert synthesis["stages"][0]["instance"] == "scout-t1"


def test_needs_attention_report_options_persist_end_to_end(tmp_path: Path):
    """Regression for review finding #6: a stage agent's offered choices must
    survive from the needs_attention report through the durable stage and the
    synthesis, so the operator entry can present them to ask_user (U8.2
    options contract). Previously options were dropped at _persist_result and
    the serve path hardcoded options=[].
    """
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    # Drive the real report-validation + persist path used by run_pipeline.
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    result = runner.validate_stage_report(
        envelope=report(
            task, "scout-t1", {"primary": write_artifact(project, "out.md")},
            status="needs_attention",
            question="which approach?",
            options=["approach A", "approach B"],
        ),
        expected_task_id=task,
        expected_sender="scout-t1",
        declared_outputs=["primary"],
        project_root=project,
    )
    assert result["options"] == ["approach A", "approach B"]
    runner._persist_result(
        agency, state.get_run(agency, PIPELINE_ID), {"id": "only"}, result, OWNER, reconciled=False
    )
    run = state.get_run(agency, PIPELINE_ID)
    stage = next(s for s in run["stages"] if s["id"] == "only")
    # The durable stage persists the offered choices.
    assert stage["status"] == "needs_attention"
    assert stage["options"] == ["approach A", "approach B"]
    # The synthesis carries them too.
    assert runner.synthesize_run(run)["stages"][0]["options"] == ["approach A", "approach B"]
    # options are only valid on a needs_attention stage; a re-dispatch clears them.
    state.record_operator_response(agency, PIPELINE_ID, "only", "approach A", lock_owner=OWNER)
    state.transition_stage(agency, PIPELINE_ID, "only", "dispatched", lock_owner=OWNER, assigned_instance="scout-t1")
    cleared = next(s for s in state.get_run(agency, PIPELINE_ID)["stages"] if s["id"] == "only")
    assert cleared["options"] is None


def test_needs_attention_report_rejects_non_string_options(tmp_path: Path):
    """validate_stage_report must reject malformed options (defense for #6)."""
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    bad = report(
        task, "scout-t1", {"primary": write_artifact(project, "out.md")},
        status="needs_attention",
        question="which approach?",
        options=["ok", ""],  # blank string is invalid
    )
    with pytest.raises(runner.InvalidStageReport):
        runner.validate_stage_report(
            envelope=bad,
            expected_task_id=task,
            expected_sender="scout-t1",
            declared_outputs=["primary"],
            project_root=project,
        )


def test_needs_attention_question_stored_on_own_field_not_error(tmp_path: Path):
    """Regression for review finding #7: the human-facing question must be persisted
    on its own `question` field, never overloaded onto `error`, so summary/error
    consumers never misrender a question as an error."""
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    result = runner.validate_stage_report(
        envelope=report(
            task, "scout-t1", {"primary": write_artifact(project, "out.md")},
            status="needs_attention",
            question="which approach?",
            error="machine reason: ambiguous scope",  # distinct from the question
            options=["A", "B"],
        ),
        expected_task_id=task,
        expected_sender="scout-t1",
        declared_outputs=["primary"],
        project_root=project,
    )
    assert result["question"] == "which approach?"
    assert result["error"] == "machine reason: ambiguous scope"
    runner._persist_result(
        agency, state.get_run(agency, PIPELINE_ID), {"id": "only"}, result, OWNER, reconciled=False
    )
    stage = next(
        s for s in state.get_run(agency, PIPELINE_ID)["stages"] if s["id"] == "only"
    )
    # question and error are persisted as separate fields.
    assert stage["question"] == "which approach?"
    assert stage["error"] == "machine reason: ambiguous scope"
    # synthesize_run surfaces them distinctly too.
    synthesis = runner.synthesize_run(state.get_run(agency, PIPELINE_ID))
    assert synthesis["stages"][0]["question"] == "which approach?"
    assert synthesis["stages"][0]["error"] == "machine reason: ambiguous scope"


def test_spawn_uncertainty_resume_only_reconciles_and_never_spawns_again(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    first = FakeControlPlane()
    first.spawn_error = "unknown spawn outcome"
    assert runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, first)["status"] == "needs_attention"
    assert [event[0] for event in first.events] == ["reserve", "spawn"]

    resumed = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, resumed, resume=True)
    assert synthesis["status"] == "needs_attention"
    assert [event[0] for event in resumed.events] == ["find"]


def test_dispatch_persistence_failure_occurs_before_external_spawn(tmp_path: Path, monkeypatch):
    agency, project = setup_run(tmp_path, stage_definition())
    fake = FakeControlPlane()

    def fail_dispatch(*_args, **_kwargs):
        raise OSError("durable dispatch failed")

    monkeypatch.setattr(state, "record_dispatched", fail_dispatch)
    with pytest.raises(OSError, match="durable dispatch failed"):
        runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert [event[0] for event in fake.events] == ["reserve"]
    assert state.get_run(agency, PIPELINE_ID)["stages"][0]["status"] == "pending"


def test_execution_guard_allows_only_one_two_thread_driver_to_reserve(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    task = f"pl-{PIPELINE_ID}-s1"
    entered = Event()
    release = Event()
    conflict = Event()
    start = Barrier(3)

    class BlockingFake(FakeControlPlane):
        def reserve_stage_instance(self, **kwargs):
            result = super().reserve_stage_instance(**kwargs)
            entered.set()
            assert release.wait(5)
            return result

    fake = BlockingFake(
        {task: report(task, "scout-t1", {"primary": write_artifact(project, "guarded.md")})}
    )
    outcomes: list[str] = []

    def drive() -> None:
        start.wait()
        try:
            outcomes.append(runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)["status"])
        except state.PipelineExecutionConflict:
            outcomes.append("conflict")
            conflict.set()

    threads = [Thread(target=drive) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    assert entered.wait(5)
    assert conflict.wait(5)
    assert [event[0] for event in fake.events] == ["reserve"]
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["conflict", "succeeded"]
    assert len([event for event in fake.events if event[0] == "spawn"]) == 1


def test_corrupt_primary_never_resumes_active_previous_generation(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    state.record_dispatched(
        agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1"
    )
    (agency / "pipelines.json").write_text("{corrupt")
    fake = FakeControlPlane()
    with pytest.raises(state.PipelineStateCorruption, match="operator reconciliation"):
        runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert not fake.events


def test_missing_selected_input_halts_before_spawn(tmp_path: Path):
    agency, project = setup_run(tmp_path, FOUR_STAGE)
    run = state.get_run(agency, PIPELINE_ID)
    state.record_dispatched(agency, PIPELINE_ID, "scout", lock_owner=OWNER, assigned_instance="scout-t1")
    state.transition_stage(
        agency, PIPELINE_ID, "scout", "succeeded", lock_owner=OWNER, summary="bad durable data", artifacts={"primary": "missing.md", "notes": "missing-notes.md"}
    )
    fake = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert synthesis["stages"][1]["status"] == "needs_attention"
    assert not fake.events


def test_resume_reconciles_existing_report_without_spawn_or_delegate(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    fake = FakeControlPlane({task: report(task, "scout-t1", {"primary": write_artifact(project, "late.md")})})
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["status"] == "succeeded"
    assert fake.find_calls == 1
    assert [event[0] for event in fake.events] == ["find"]


def test_resume_without_report_escalates_once_and_never_redelegates(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    fake = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["status"] == "needs_attention"
    assert fake.find_calls == 1
    assert all(event[0] not in {"spawn", "delegate"} for event in fake.events)


def test_late_report_reconciles_dispatched_attention_once(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    state.transition_stage(agency, PIPELINE_ID, "only", "needs_attention", lock_owner=OWNER, error="timeout", question="timeout")
    fake = FakeControlPlane({task: report(task, "scout-t1", {"primary": write_artifact(project, "late.md")})})
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["status"] == "succeeded"
    assert fake.find_calls == 1
    assert [event[0] for event in fake.events] == ["find"]


def test_resume_re_dispaches_needs_attention_with_operator_answer(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    state.transition_stage(
        agency, PIPELINE_ID, "only", "needs_attention", lock_owner=OWNER,
        error="which way?", question="which way?", summary="scaffolded the module",
    )
    # Operator answered; the answer is bound to the exact stage.
    state.record_operator_response(agency, PIPELINE_ID, "only", "use approach B", lock_owner=OWNER)
    # The re-dispatched stage eventually succeeds.
    fake = FakeControlPlane({task: report(task, "scout-t1", {"primary": write_artifact(project, "out.md")})})
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["status"] == "succeeded"
    # Reuse path: no fresh reserve/spawn when the prior surface is alive.
    dispatch_events = [e for e in fake.events if e[0] in {"reserve", "spawn", "delegate"}]
    assert dispatch_events == [("delegate", task, "scout-t1")], dispatch_events
    # The answer was injected into the re-dispatched delegate payload.
    assert fake.payloads[task]["operatorResponse"] == "use approach B"
    assert fake.payloads[task]["priorSummary"] == "scaffolded the module"


def test_resume_re_dispatch_reuses_instance_when_alive_else_reserves_fresh(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    state.transition_stage(
        agency, PIPELINE_ID, "only", "needs_attention", lock_owner=OWNER, error="q", question="q"
    )
    state.record_operator_response(agency, PIPELINE_ID, "only", "ans", lock_owner=OWNER)
    # Surface not alive -> reserve + spawn a fresh instance, then delegate.
    class DeadSurfaceFake(FakeControlPlane):
        def surface_alive(self, instance: str) -> bool:
            return False
    fake = DeadSurfaceFake({task: report(task, "scout-t1", {"primary": write_artifact(project, "out.md")})})
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["status"] == "succeeded"
    dispatch_events = [e[0] for e in fake.events]
    assert dispatch_events == ["reserve", "spawn", "delegate", "wait"], dispatch_events
    assert fake.payloads[task]["operatorResponse"] == "ans"


def test_real_control_plane_surface_alive_resolves_instance_to_cmux_surface(tmp_path: Path, monkeypatch):
    """Regression: AgencyControlPlane.surface_alive must resolve an intercomName
    through the ledger to its bound cmuxSurface before consulting cmux.

    The original implementation called the unimported `cmux_pane.surface_alive`
    directly with the intercomName, which both raised NameError and (with the
    import fixed) never matched a surface ref. U8.3 resume reuse depended on it.
    This test drives the REAL method, not the FakeControlPlane mock.
    """
    agency, project = setup_run(tmp_path, stage_definition())
    # Bind a stage instance row to a cmux surface in the live ledger.
    sessions = {
        "version": 1,
        "instances": [
            {
                "instanceId": "i-scout-t1",
                "intercomName": "scout-t1",
                "role": "scout",
                "status": "working",
                "cmuxSurface": "surface:12",
                "taskId": f"pl-{PIPELINE_ID}-s1",
            }
        ],
    }
    (agency / "sessions.json").write_text(json.dumps(sessions))

    # Simulate cmux surface liveness by intercepting the wrapped primitive.
    live = {"surface:12": True, "surface:99": False}
    monkeypatch.setattr(ctl, "surface_alive", lambda surface: live.get(surface))

    cplane = pipeline_runtime.AgencyControlPlane(agency, project=project)

    # intercomName 'scout-t1' -> cmuxSurface 'surface:12' -> alive
    assert cplane.surface_alive("scout-t1") is True
    # unknown instance is not reusable
    assert cplane.surface_alive("ghost-t9") is False
    # empty / non-string instance is rejected
    assert cplane.surface_alive("") is False
    assert cplane.surface_alive(None) is False
    # bound to surface:99 (simulated dead) -> not reusable
    sessions["instances"][0]["cmuxSurface"] = "surface:99"
    (agency / "sessions.json").write_text(json.dumps(sessions))
    assert cplane.surface_alive("scout-t1") is False



def test_dispatched_attention_without_late_report_remains_unchanged(tmp_path: Path):
    definition = stage_definition()
    agency, project = setup_run(tmp_path, definition)
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    state.transition_stage(agency, PIPELINE_ID, "only", "needs_attention", lock_owner=OWNER, error="original", question="original")
    fake = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake, resume=True)
    assert synthesis["stages"][0]["error"] == "original"
    assert fake.find_calls == 1


def test_normal_invocation_never_retries_dispatched_work(tmp_path: Path):
    agency, project = setup_run(tmp_path, stage_definition())
    state.record_dispatched(agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1")
    fake = FakeControlPlane()
    synthesis = runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)
    assert synthesis["status"] == "needs_attention"
    assert not fake.events


def test_report_is_persisted_before_ack_and_ack_failure_does_not_reexecute(
    tmp_path: Path, monkeypatch
):
    agency, project = setup_run(tmp_path, stage_definition())
    task = f"pl-{PIPELINE_ID}-s1"
    events = []

    class ReceiptFake(FakeControlPlane):
        def wait_stage(self, **kwargs):
            self.events.append(("wait", kwargs["task_id"], kwargs["expected_sender"]))
            return runner.WaitResult(
                "report", self.reports[kwargs["task_id"]], receipt="processing/report.json"
            )

        def ack_stage_report(self, receipt: str) -> None:
            events.append(("ack", receipt))
            raise OSError("ack crash")

    real_transition = state.transition_stage

    def tracked_transition(*args, **kwargs):
        result = real_transition(*args, **kwargs)
        if kwargs.get("summary"):
            events.append(("persist", result["status"]))
        return result

    monkeypatch.setattr(state, "transition_stage", tracked_transition)
    fake = ReceiptFake(
        {task: report(task, "scout-t1", {"primary": write_artifact(project, "out.md")})}
    )
    assert runner.run_pipeline(agency, project, PIPELINE_ID, OWNER, fake)["status"] == "succeeded"
    assert events == [("persist", "succeeded"), ("ack", "processing/report.json")]
    assert [event[0] for event in fake.events].count("spawn") == 1


def test_resume_report_is_persisted_before_processing_ack(tmp_path: Path, monkeypatch):
    agency, project = setup_run(tmp_path, stage_definition())
    task = f"pl-{PIPELINE_ID}-s1"
    state.record_dispatched(
        agency, PIPELINE_ID, "only", lock_owner=OWNER, assigned_instance="scout-t1"
    )
    events = []

    class ResumeReceiptFake(FakeControlPlane):
        def find_existing_report(self, **kwargs):
            self.find_calls += 1
            return runner.WaitResult(
                "report", self.reports[kwargs["task_id"]], receipt="processing/late.json"
            )

        def ack_stage_report(self, receipt: str) -> None:
            events.append(("ack", receipt))

    real_reconcile = state.record_reconciled_result

    def tracked_reconcile(*args, **kwargs):
        result = real_reconcile(*args, **kwargs)
        events.append(("persist", result["status"]))
        return result

    monkeypatch.setattr(state, "record_reconciled_result", tracked_reconcile)
    fake = ResumeReceiptFake(
        {task: report(task, "scout-t1", {"primary": write_artifact(project, "late.md")})}
    )
    assert runner.run_pipeline(
        agency, project, PIPELINE_ID, OWNER, fake, resume=True
    )["status"] == "succeeded"
    assert events == [("persist", "succeeded"), ("ack", "processing/late.json")]
    assert all(event[0] not in {"spawn", "delegate"} for event in fake.events)


def test_module_dependency_and_protocol_have_no_retry_surface():
    source = Path(runner.__file__).read_text()
    for forbidden in ("agency_ctl", "agent_spawn", "cmux", "subprocess"):
        assert forbidden not in source
    assert "retry" not in runner.ControlPlane.__dict__
    assert "redelegate" not in runner.ControlPlane.__dict__
    assert set(name for name, value in inspect.getmembers(runner.ControlPlane, inspect.isfunction) if not name.startswith("_")) == {
        "reserve_stage_instance", "spawn_stage", "delegate_stage", "wait_stage", "find_existing_report",
        "ack_stage_report", "surface_alive"
    }
