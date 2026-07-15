from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import pipeline_state as state


PIPELINE = {
    "description": "Scout then implement",
    "onFailure": "stop",
    "stages": [
        {"id": "scout", "role": "scout", "goal": "Scout {topic}", "outputs": ["primary"], "inputs": []},
        {
            "id": "implement",
            "role": "worker",
            "goal": "Implement {topic}",
            "outputs": ["primary"],
            "inputs": [{"stage": "scout", "artifacts": ["primary"]}],
        },
    ],
}


def acquire(tmp_path: Path, pipeline_id: str = "p-123", owner_id: str = "owner-1") -> None:
    state.acquire_lock(
        tmp_path,
        pipeline_id=pipeline_id,
        owner_id=owner_id,
        owner_pid=1234,
        owner_surface="surface:runner",
    )


def create(tmp_path: Path, *, pipeline_id: str = "p-123", owner_id: str = "owner-1") -> dict:
    acquire(tmp_path, pipeline_id, owner_id)
    return state.create_run(
        tmp_path,
        pipeline_id=pipeline_id,
        pipeline_name="implementation",
        topic="crash safety",
        definition=PIPELINE,
        lock_owner=owner_id,
    )


def test_state_version_is_bumped_without_silent_migration(tmp_path: Path):
    assert state.STATE_VERSION == 3
    assert state.empty_state()["version"] == 3
    (tmp_path / "pipelines.json").write_text(
        json.dumps({"version": 1, "activePipelineId": None, "runs": []})
    )
    with pytest.raises(state.PipelineStateCorruption, match="unsupported version 1"):
        state.load_state(tmp_path)


def test_lock_is_exclusive_durable_and_release_is_owner_checked(tmp_path: Path):
    acquire(tmp_path)
    lock = state.read_lock(tmp_path)
    assert lock == {
        "version": 1,
        "pipelineId": "p-123",
        "ownerId": "owner-1",
        "ownerPid": 1234,
        "ownerSurface": "surface:runner",
        "createdAt": lock["createdAt"],
    }

    with pytest.raises(state.PipelineLockConflict) as exc:
        state.acquire_lock(tmp_path, pipeline_id="p-456", owner_id="owner-2")
    assert exc.value.ownership["pipelineId"] == "p-123"
    assert exc.value.ownership["ownerId"] == "owner-1"
    assert "p-123" in str(exc.value)

    with pytest.raises(state.PipelineLockOwnershipError):
        state.release_lock(tmp_path, owner_id="owner-2")
    assert state.read_lock(tmp_path) is not None
    state.release_lock(tmp_path, owner_id="owner-1", pipeline_id="p-123")
    assert state.read_lock(tmp_path) is None


def test_bind_lock_runtime_is_owner_checked_atomic_and_preserves_creation(tmp_path: Path):
    acquire(tmp_path)
    before = state.read_lock(tmp_path)
    assert before is not None

    bound = state.bind_lock_runtime(
        tmp_path,
        pipeline_id="p-123",
        owner_id="owner-1",
        owner_pid=9876,
        owner_surface="surface:live",
    )
    assert bound == {
        **before,
        "ownerPid": 9876,
        "ownerSurface": "surface:live",
    }
    assert state.read_lock(tmp_path) == bound
    assert state.bind_lock_runtime(
        tmp_path,
        pipeline_id="p-123",
        owner_id="owner-1",
        owner_pid=9876,
        owner_surface="surface:live",
    ) == bound
    assert not list(tmp_path.glob(".pipelines.lock.*.tmp"))

    with pytest.raises(state.PipelineLockOwnershipError):
        state.bind_lock_runtime(
            tmp_path,
            pipeline_id="p-123",
            owner_id="wrong",
            owner_pid=1,
            owner_surface="surface:wrong",
        )
    with pytest.raises(state.PipelineLockOwnershipError):
        state.bind_lock_runtime(
            tmp_path,
            pipeline_id="p-other",
            owner_id="owner-1",
            owner_pid=1,
            owner_surface="surface:wrong",
        )
    assert state.read_lock(tmp_path) == bound


def test_concurrent_lock_contenders_have_exactly_one_owner(tmp_path: Path):
    barrier = Barrier(2)

    def contend(number: int) -> tuple[str, str]:
        pipeline_id = f"p-{number}"
        barrier.wait()
        try:
            state.acquire_lock(tmp_path, pipeline_id=pipeline_id, owner_id=f"owner-{number}")
            return "acquired", pipeline_id
        except state.PipelineLockConflict as exc:
            return "conflict", exc.ownership["pipelineId"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contend, (1, 2)))
    assert [result[0] for result in results].count("acquired") == 1
    assert [result[0] for result in results].count("conflict") == 1
    winning_id = next(pipeline_id for outcome, pipeline_id in results if outcome == "acquired")
    assert next(pipeline_id for outcome, pipeline_id in results if outcome == "conflict") == winning_id


def test_atomic_save_preserves_previous_generation_and_ignores_temp(tmp_path: Path):
    first = state.empty_state()
    state.save_state(tmp_path, first)
    second = state.empty_state()
    second["runs"].append(_valid_terminal_run("p-old"))
    state.save_state(tmp_path, second)

    assert state.load_state(tmp_path) == second
    assert json.loads((tmp_path / "pipelines.json.prev").read_text()) == first
    (tmp_path / ".pipelines.json.interrupted.tmp").write_text("{not-json")
    assert state.load_state(tmp_path) == second


def test_load_falls_back_for_missing_or_corrupt_primary(tmp_path: Path):
    prior = state.empty_state()
    prior["runs"].append(_valid_terminal_run("p-prior"))
    (tmp_path / "pipelines.json.prev").write_text(json.dumps(prior))
    assert state.load_state(tmp_path) == prior

    (tmp_path / "pipelines.json").write_text("truncated")
    assert state.load_state(tmp_path) == prior


def test_invalid_save_is_rejected_before_replacing_valid_state(tmp_path: Path):
    valid = state.empty_state()
    state.save_state(tmp_path, valid)
    invalid = {"version": 99, "activePipelineId": None, "runs": []}
    with pytest.raises(state.PipelineStateValidationError, match="unsupported version"):
        state.save_state(tmp_path, invalid)
    assert state.load_state(tmp_path) == valid
    assert not list(tmp_path.glob(".pipelines.json.*.tmp"))


def test_interrupted_replace_keeps_readable_prior_generation(tmp_path: Path, monkeypatch):
    first = state.empty_state()
    state.save_state(tmp_path, first)
    second = state.empty_state()
    second["runs"].append(_valid_terminal_run("p-next"))
    real_replace = os.replace
    calls = 0

    def fail_second_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash")
        return real_replace(src, dst)

    monkeypatch.setattr(state.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated crash"):
        state.save_state(tmp_path, second)
    assert state.load_state(tmp_path) == first


def test_unknown_or_unrecoverably_corrupt_state_is_clear(tmp_path: Path):
    (tmp_path / "pipelines.json").write_text('{"version": 99, "activePipelineId": null, "runs": []}')
    with pytest.raises(state.PipelineStateCorruption, match="unsupported version"):
        state.load_state(tmp_path)

    (tmp_path / "pipelines.json.prev").write_text("also broken")
    with pytest.raises(state.PipelineStateCorruption, match="primary.*previous"):
        state.load_state(tmp_path)


def test_corrupt_lock_fails_without_guessing_stale_ownership(tmp_path: Path):
    (tmp_path / "pipelines.lock").write_text("partial")
    with pytest.raises(state.PipelineLockCorruption, match="invalid pipeline lock"):
        state.read_lock(tmp_path)
    with pytest.raises(state.PipelineLockCorruption):
        state.acquire_lock(tmp_path, pipeline_id="p-new", owner_id="owner-new")


def test_create_and_get_active_run_with_stable_stage_records(tmp_path: Path):
    run = create(tmp_path)
    assert run["pipelineId"] == "p-123"
    assert run["pipelineName"] == "implementation"
    assert run["topic"] == "crash safety"
    assert run["status"] == "running"
    assert run["definitionDigest"] == state.pipeline_definition_digest(PIPELINE)
    assert len(run["definitionDigest"]) == 64
    assert run["currentStageId"] == "scout"
    assert run["runnerInstance"] is None and run["runnerSurface"] is None
    assert run["finalTaskId"] == "pipe-done-p-123"
    assert [(s["id"], s["role"], s["taskId"], s["status"], s["assignedInstance"]) for s in run["stages"]] == [
        ("scout", "scout", "pl-p-123-s1", "pending", None),
        ("implement", "worker", "pl-p-123-s2", "pending", None),
    ]
    assert state.get_run(tmp_path, "p-123") == run
    assert state.get_active_run(tmp_path) == run
    with pytest.raises(state.UnknownPipelineError):
        state.get_run(tmp_path, "missing")


def test_definition_digest_is_operational_canonical_and_validated(tmp_path: Path):
    create(tmp_path)
    run = state.get_run(tmp_path, "p-123")
    reordered_metadata = {
        "description": "changed only",
        "stages": PIPELINE["stages"],
        "onFailure": "stop",
    }
    assert state.pipeline_definition_digest(reordered_metadata) == run["definitionDigest"]
    changed = dict(reordered_metadata)
    changed["stages"] = [dict(stage) for stage in PIPELINE["stages"]]
    changed["stages"][0]["goal"] = "Different {topic}"
    assert state.pipeline_definition_digest(changed) != run["definitionDigest"]

    data = state.load_state(tmp_path)
    data["runs"][0]["definitionDigest"] = "not-a-digest"
    with pytest.raises(state.PipelineStateValidationError, match="canonical SHA-256"):
        state.save_state(tmp_path, data)


def test_final_task_id_is_stable_and_validated(tmp_path: Path):
    create(tmp_path)
    data = state.load_state(tmp_path)
    data["runs"][0]["finalTaskId"] = "pipe-done-forged"
    with pytest.raises(state.PipelineStateValidationError, match="finalTaskId must be stable"):
        state.save_state(tmp_path, data)
    assert state.get_run(tmp_path, "p-123")["finalTaskId"] == "pipe-done-p-123"


def test_terminal_final_delivery_progress_is_stable_owner_checked_and_idempotent(
    tmp_path: Path
):
    create(tmp_path)
    state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    state.transition_stage(
        tmp_path,
        "p-123",
        "scout",
        "failed",
        lock_owner="owner-1",
        error="failed",
    )
    run = state.get_run(tmp_path, "p-123")
    assert run["finalDelivery"] == {
        "messageId": "pipe-final-p-123",
        "publishedAt": None,
        "cleanupStartedAt": None,
    }
    with pytest.raises(state.PipelineLockOwnershipError):
        state.mark_final_published(tmp_path, "p-123", lock_owner="wrong")
    published = state.mark_final_published(
        tmp_path, "p-123", lock_owner="owner-1"
    )
    assert published["publishedAt"]
    assert state.mark_final_published(
        tmp_path, "p-123", lock_owner="owner-1"
    ) == published
    cleanup = state.mark_final_cleanup_started(
        tmp_path, "p-123", lock_owner="owner-1"
    )
    assert cleanup["cleanupStartedAt"]
    assert state.mark_final_cleanup_started(
        tmp_path, "p-123", lock_owner="owner-1"
    ) == cleanup


def test_create_requires_matching_lock_and_only_one_active_run(tmp_path: Path):
    acquire(tmp_path)
    with pytest.raises(state.PipelineLockOwnershipError):
        state.create_run(
            tmp_path,
            pipeline_id="p-123",
            pipeline_name="implementation",
            topic="x",
            definition=PIPELINE,
            lock_owner="wrong",
        )
    state.create_run(
        tmp_path,
        pipeline_id="p-123",
        pipeline_name="implementation",
        topic="x",
        definition=PIPELINE,
        lock_owner="owner-1",
    )
    with pytest.raises(state.ActivePipelineError, match="p-123"):
        state.create_run(
            tmp_path,
            pipeline_id="p-123",
            pipeline_name="implementation",
            topic="again",
            definition=PIPELINE,
            lock_owner="owner-1",
        )


def test_runner_binding_and_exact_task_ownership_query(tmp_path: Path):
    create(tmp_path)
    state.bind_runner(
        tmp_path,
        "p-123",
        lock_owner="owner-1",
        runner_instance="pipeline-runner-t1",
        runner_surface="surface:runner",
    )
    binding = state.get_active_runner_binding(tmp_path)
    assert binding == {
        "pipelineId": "p-123",
        "finalTaskId": "pipe-done-p-123",
        "runnerInstance": "pipeline-runner-t1",
        "runnerSurface": "surface:runner",
    }

    state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")
    owner = state.find_task_ownership(tmp_path, "pl-p-123-s1")
    assert owner == {
        "pipelineId": "p-123",
        "pipelineName": "implementation",
        "stageId": "scout",
        "role": "scout",
        "taskKind": "stage",
        "taskId": "pl-p-123-s1",
        "runStatus": "running",
        "stageStatus": "dispatched",
        "expectedSender": "scout-t1",
        "runnerInstance": "pipeline-runner-t1",
        "runnerSurface": "surface:runner",
    }
    assert state.find_task_ownership(tmp_path, "pipe-done-p-123") == {
        "pipelineId": "p-123",
        "pipelineName": "implementation",
        "stageId": None,
        "role": "pipeline-runner",
        "taskKind": "final",
        "taskId": "pipe-done-p-123",
        "runStatus": "running",
        "stageStatus": None,
        "expectedSender": "pipeline-runner-t1",
        "runnerInstance": "pipeline-runner-t1",
        "runnerSurface": "surface:runner",
    }
    assert state.find_task_ownership(tmp_path, "pipe-done-p-123-extra") is None
    assert state.find_task_ownership(tmp_path, "pl-p-123-s10") is None
    assert state.find_task_ownership(tmp_path, "PL-p-123-s1") is None


def test_dispatch_requires_and_atomically_persists_assigned_instance(tmp_path: Path):
    create(tmp_path)
    with pytest.raises(TypeError, match="assigned_instance"):
        state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1")
    pending = state.get_run(tmp_path, "p-123")["stages"][0]
    assert pending["status"] == "pending"
    assert pending["assignedInstance"] is None

    dispatched = state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    assert dispatched["status"] == "dispatched"
    assert dispatched["assignedInstance"] == "scout-t1"


def test_legal_transitions_advance_run_and_illegal_transitions_fail(tmp_path: Path):
    create(tmp_path)
    dispatched = state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    assert dispatched["status"] == "dispatched"
    assert dispatched["dispatchedAt"] is not None
    with pytest.raises(state.IllegalStageTransition):
        state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")

    succeeded = state.transition_stage(
        tmp_path,
        "p-123",
        "scout",
        "succeeded",
        lock_owner="owner-1",
        summary="scouted",
        artifacts={"primary": "artifacts/scout.md"},
    )
    assert succeeded["status"] == "succeeded"
    assert state.get_active_run(tmp_path)["currentStageId"] == "implement"
    with pytest.raises(state.IllegalStageTransition):
        state.transition_stage(tmp_path, "p-123", "scout", "failed", lock_owner="owner-1", error="late")

    state.record_dispatched(tmp_path, "p-123", "implement", lock_owner="owner-1", assigned_instance="worker-t1")
    state.transition_stage(
        tmp_path,
        "p-123",
        "implement",
        "failed",
        lock_owner="owner-1",
        summary="failed",
        error="boom",
    )
    assert state.get_run(tmp_path, "p-123")["status"] == "failed"
    assert state.get_active_run(tmp_path) is None


def test_pending_can_enter_undispatched_attention(tmp_path: Path):
    create(tmp_path)
    stage = state.transition_stage(
        tmp_path,
        "p-123",
        "scout",
        "needs_attention",
        lock_owner="owner-1",
        error="catalog changed",
    )
    assert stage["assignedInstance"] is None
    assert stage["dispatchedAt"] is None
    assert stage["completedAt"] is not None
    assert state.get_active_run(tmp_path)["status"] == "needs_attention"


def test_dependency_failed_is_only_legal_from_current_pending(tmp_path: Path):
    definition = dict(PIPELINE)
    definition["onFailure"] = "continue"
    acquire(tmp_path)
    state.create_run(
        tmp_path,
        pipeline_id="p-123",
        pipeline_name="implementation",
        topic="dependencies",
        definition=definition,
        lock_owner="owner-1",
    )
    state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    state.transition_stage(
        tmp_path, "p-123", "scout", "failed", lock_owner="owner-1", error="scout failed"
    )
    stage = state.transition_stage(
        tmp_path,
        "p-123",
        "implement",
        "dependency_failed",
        lock_owner="owner-1",
        error="scout failed",
    )
    assert stage["status"] == "dependency_failed"
    assert stage["assignedInstance"] is None
    with pytest.raises(state.PipelineStateError):
        state.record_dispatched(tmp_path, "p-123", "implement", lock_owner="owner-1", assigned_instance="worker-t1")


def test_continue_policy_keeps_independent_pending_work_active(tmp_path: Path):
    definition = dict(PIPELINE)
    definition["onFailure"] = "continue"
    acquire(tmp_path)
    state.create_run(
        tmp_path,
        pipeline_id="p-123",
        pipeline_name="implementation",
        topic="continue",
        definition=definition,
        lock_owner="owner-1",
    )
    state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")
    state.transition_stage(
        tmp_path,
        "p-123",
        "scout",
        "failed",
        lock_owner="owner-1",
        error="scout failed",
    )
    active = state.get_active_run(tmp_path)
    assert active["status"] == "running"
    assert active["currentStageId"] == "implement"


def test_resume_classification_reconciles_or_escalates_without_retry(tmp_path: Path):
    create(tmp_path)
    stage = state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")
    calls: list[str] = []

    def report_exists(task_id: str) -> bool:
        calls.append(task_id)
        return True

    assert state.classify_resume(stage, report_exists) == state.ResumeAction.RECONCILE
    assert calls == ["pl-p-123-s1"]
    assert state.reconcile_resume(
        tmp_path, "p-123", "scout", report_exists, lock_owner="owner-1"
    ) == state.ResumeAction.RECONCILE
    assert state.get_run(tmp_path, "p-123")["stages"][0]["status"] == "dispatched"

    assert state.reconcile_resume(
        tmp_path, "p-123", "scout", lambda _task: False, lock_owner="owner-1"
    ) == state.ResumeAction.NEEDS_ATTENTION
    escalated = state.get_run(tmp_path, "p-123")["stages"][0]
    assert escalated["status"] == "needs_attention"
    assert "No report" in escalated["error"]
    with pytest.raises(state.IllegalStageTransition):
        state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")


def test_late_report_reconciliation_is_narrow_and_requires_prior_dispatch(tmp_path: Path):
    create(tmp_path)
    with pytest.raises(state.IllegalStageTransition, match="never dispatched"):
        state.record_reconciled_result(
            tmp_path,
            "p-123",
            "scout",
            lock_owner="owner-1",
            status="succeeded",
            summary="late",
            artifacts={},
            error=None,
        )

    state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    state.transition_stage(
        tmp_path,
        "p-123",
        "scout",
        "needs_attention",
        lock_owner="owner-1",
        error="timeout",
    )
    attention = state.get_run(tmp_path, "p-123")["stages"][0]
    calls = []
    assert state.classify_resume(attention, lambda task: calls.append(task) or True) == state.ResumeAction.RECONCILE
    assert calls == ["pl-p-123-s1"]
    assert state.reconcile_resume(
        tmp_path, "p-123", "scout", lambda _task: False, lock_owner="owner-1"
    ) == state.ResumeAction.NEEDS_ATTENTION
    assert state.get_run(tmp_path, "p-123")["stages"][0]["error"] == "timeout"
    reconciled = state.record_reconciled_result(
        tmp_path,
        "p-123",
        "scout",
        lock_owner="owner-1",
        status="succeeded",
        summary="late success",
        artifacts={"primary": "artifacts/scout.md"},
        error=None,
    )
    assert reconciled["status"] == "succeeded"
    assert state.get_active_run(tmp_path)["status"] == "running"


def test_undispatched_attention_does_not_query_for_resume(tmp_path: Path):
    create(tmp_path)
    state.transition_stage(
        tmp_path, "p-123", "scout", "needs_attention", lock_owner="owner-1", error="before spawn"
    )
    stage = state.get_run(tmp_path, "p-123")["stages"][0]
    assert state.classify_resume(
        stage, lambda _task: pytest.fail("undispatched attention must not query")
    ) == state.ResumeAction.TERMINAL
    assert "retry" not in {action.value for action in state.ResumeAction}


def test_resume_skips_succeeded_and_never_calls_report_lookup(tmp_path: Path):
    create(tmp_path)
    state.record_dispatched(tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1")
    state.transition_stage(tmp_path, "p-123", "scout", "succeeded", lock_owner="owner-1", summary="ok")

    def unexpected(_task_id: str) -> bool:
        raise AssertionError("succeeded stages must not query or redelegate")

    stage = state.get_run(tmp_path, "p-123")["stages"][0]
    assert state.classify_resume(stage, unexpected) == state.ResumeAction.SKIP


def test_schema_rejects_forged_task_ids_and_active_stage_layouts(tmp_path: Path):
    create(tmp_path)
    original = state.load_state(tmp_path)

    forged_task = json.loads(json.dumps(original))
    forged_task["runs"][0]["stages"][0]["taskId"] = "pl-p-123-s2"
    with pytest.raises(state.PipelineStateValidationError, match="taskId must be stable"):
        state.validate_state(forged_task)

    wrong_current = json.loads(json.dumps(original))
    wrong_current["runs"][0]["currentStageId"] = "implement"
    with pytest.raises(state.PipelineStateValidationError, match="terminal prefix and pending suffix"):
        state.validate_state(wrong_current)

    no_current = json.loads(json.dumps(original))
    no_current["runs"][0]["currentStageId"] = None
    with pytest.raises(state.PipelineStateValidationError, match="actionable currentStageId"):
        state.validate_state(no_current)

    two_dispatched = json.loads(json.dumps(original))
    timestamp = two_dispatched["runs"][0]["createdAt"]
    for index, instance in ((0, "scout-t1"), (1, "worker-t1")):
        stage = two_dispatched["runs"][0]["stages"][index]
        stage["status"] = "dispatched"
        stage["assignedInstance"] = instance
        stage["dispatchedAt"] = timestamp
    with pytest.raises(state.PipelineStateValidationError, match="pending suffix|only the running"):
        state.validate_state(two_dispatched)

    stopped_attention = json.loads(json.dumps(original))
    timestamp = stopped_attention["runs"][0]["createdAt"]
    failed = stopped_attention["runs"][0]["stages"][0]
    failed.update(
        status="failed",
        assignedInstance="scout-t1",
        dispatchedAt=timestamp,
        completedAt=timestamp,
        error="boom",
    )
    attention = stopped_attention["runs"][0]["stages"][1]
    attention.update(status="needs_attention", completedAt=timestamp, error="forged continuation")
    stopped_attention["runs"][0]["status"] = "needs_attention"
    stopped_attention["runs"][0]["currentStageId"] = "implement"
    with pytest.raises(state.PipelineStateValidationError, match="stopped policy cannot continue"):
        state.validate_state(stopped_attention)


def test_schema_rejects_incoherent_terminal_outcomes(tmp_path: Path):
    create(tmp_path)
    data = state.load_state(tmp_path)
    run = data["runs"][0]
    run["status"] = "succeeded"
    run["currentStageId"] = None
    run["completedAt"] = run["updatedAt"]
    data["activePipelineId"] = None
    with pytest.raises(state.PipelineStateValidationError, match="every stage succeeded"):
        state.validate_state(data)

    # Produce a legitimate stopped failure, then forge its pending suffix.
    state.record_dispatched(
        tmp_path, "p-123", "scout", lock_owner="owner-1", assigned_instance="scout-t1"
    )
    state.transition_stage(
        tmp_path, "p-123", "scout", "failed", lock_owner="owner-1", error="boom"
    )
    failed = state.load_state(tmp_path)
    later = failed["runs"][0]["stages"][1]
    later["status"] = "dependency_failed"
    later["error"] = "forged"
    later["completedAt"] = failed["runs"][0]["completedAt"]
    with pytest.raises(state.PipelineStateValidationError, match="pending suffix"):
        state.validate_state(failed)


def test_only_current_stage_can_transition_or_reconcile(tmp_path: Path):
    create(tmp_path)
    with pytest.raises(state.IllegalStageTransition, match="only current stage 'scout'"):
        state.transition_stage(
            tmp_path,
            "p-123",
            "implement",
            "needs_attention",
            lock_owner="owner-1",
            error="out of order",
        )
    with pytest.raises(state.IllegalStageTransition, match="only current stage 'scout'"):
        state.record_reconciled_result(
            tmp_path,
            "p-123",
            "implement",
            lock_owner="owner-1",
            status="succeeded",
            summary="forged",
            artifacts={},
            error=None,
        )


def test_state_module_has_no_runner_or_cli_dependency():
    source = Path(state.__file__).read_text()
    assert "import pipeline_runner" not in source
    assert "from pipeline_runner" not in source
    assert "import agency_ctl" not in source
    assert "from agency_ctl" not in source


def _valid_terminal_run(pipeline_id: str) -> dict:
    timestamp = "2026-07-14T00:00:00Z"
    return {
        "pipelineId": pipeline_id,
        "pipelineName": "implementation",
        "topic": "topic",
        "status": "succeeded",
        "onFailure": "stop",
        "definitionDigest": state.pipeline_definition_digest(
            {
                "onFailure": "stop",
                "stages": [
                    {
                        "id": "only",
                        "role": "scout",
                        "goal": "Only {topic}",
                        "outputs": ["primary"],
                        "inputs": [],
                    }
                ],
            }
        ),
        "currentStageId": None,
        "runnerInstance": None,
        "runnerSurface": None,
        "finalTaskId": f"pipe-done-{pipeline_id}",
        "finalDelivery": {
            "messageId": f"pipe-final-{pipeline_id}",
            "publishedAt": None,
            "cleanupStartedAt": None,
        },
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "completedAt": timestamp,
        "stages": [
            {
                "id": "only",
                "role": "scout",
                "taskId": f"pl-{pipeline_id}-s1",
                "assignedInstance": "scout-t1",
                "status": "succeeded",
                "summary": "done",
                "artifacts": {},
                "error": None,
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "dispatchedAt": timestamp,
                "completedAt": timestamp,
            }
        ],
    }
