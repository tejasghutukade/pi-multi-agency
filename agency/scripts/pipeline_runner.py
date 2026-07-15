#!/usr/bin/env python3
"""Pure deterministic driver for an already-created declarative pipeline run."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import catalog
import pipeline_state


class PipelineRunnerError(RuntimeError):
    """Base error for deterministic pipeline execution."""


class PipelineDefinitionMismatch(PipelineRunnerError):
    """The current catalog no longer matches the committed run definition."""


class InvalidStageReport(PipelineRunnerError):
    """A stage report does not satisfy its exact identity/result contract."""


class InvalidArtifactPath(InvalidStageReport):
    """A reported or inherited artifact is unsafe or unavailable."""


class InvalidStageInput(PipelineRunnerError):
    """A selected durable stage input cannot be resolved safely."""


@dataclass(frozen=True)
class SpawnResult:
    instance: str


@dataclass(frozen=True)
class WaitResult:
    status: Literal["report", "timeout", "pane_dead"]
    envelope: Mapping[str, Any] | None = None
    detail: str | None = None
    receipt: str | None = None


class ControlPlane(Protocol):
    def reserve_stage_instance(
        self, *, pipeline_id: str, role: str, task_id: str
    ) -> SpawnResult:
        """Choose an exact instance identifier without performing external effects."""
        ...

    def spawn_stage(
        self, *, pipeline_id: str, role: str, task_id: str, instance: str
    ) -> None: ...

    def delegate_stage(
        self,
        *,
        pipeline_id: str,
        instance: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> None: ...

    def wait_stage(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        expected_sender: str,
        timeout: float,
    ) -> WaitResult: ...

    def find_existing_report(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        expected_sender: str,
    ) -> WaitResult | None: ...

    def ack_stage_report(self, receipt: str) -> None:
        """Idempotently acknowledge a processing receipt after durable state."""
        ...


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _mapping_keys(value: Mapping[str, Any], context: str) -> list[Any]:
    keys = list(value.keys())
    if len(keys) != len(set(keys)):
        raise InvalidStageReport(f"{context} contains duplicate fields")
    return keys


def validate_artifact_path(project_root: Path, value: Any) -> str:
    """Validate existence and symlink-aware project containment, then normalize."""
    if not isinstance(value, str) or not value:
        raise InvalidArtifactPath("artifact path must be a non-empty string")
    if "\x00" in value:
        raise InvalidArtifactPath("artifact path must not contain NUL")
    candidate = Path(value)
    if candidate.is_absolute():
        raise InvalidArtifactPath(f"artifact path must be relative: {value!r}")
    if ".." in candidate.parts:
        raise InvalidArtifactPath(f"artifact path must not contain '..': {value!r}")
    try:
        root = Path(project_root).resolve(strict=True)
        resolved = (root / candidate).resolve(strict=True)
        relative = resolved.relative_to(root)
    except FileNotFoundError as exc:
        raise InvalidArtifactPath(f"artifact path does not exist: {value!r}") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidArtifactPath(f"artifact path escapes project root: {value!r}") from exc
    if not relative.parts:
        raise InvalidArtifactPath("artifact path must not be the project root")
    return relative.as_posix()


def resolve_named_inputs(
    *,
    project_root: Path,
    stage_definition: Mapping[str, Any],
    stage_records: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    """Resolve selected artifacts in catalog input and selector order."""
    named: dict[str, str] = {}
    paths: list[str] = []
    for selector in stage_definition.get("inputs", []):
        source_id = selector["stage"]
        source = stage_records.get(source_id)
        if source is None:
            raise InvalidStageInput(f"input source stage {source_id!r} has no durable record")
        if source.get("status") != "succeeded":
            raise InvalidStageInput(
                f"input source stage {source_id!r} is {source.get('status')!r}, not succeeded"
            )
        source_artifacts = source.get("artifacts")
        if not isinstance(source_artifacts, Mapping):
            raise InvalidStageInput(f"input source stage {source_id!r} has invalid artifacts")
        for artifact_name in selector["artifacts"]:
            key = f"{source_id}.{artifact_name}"
            if artifact_name not in source_artifacts:
                raise InvalidStageInput(f"selected input {key!r} is missing")
            try:
                path = validate_artifact_path(project_root, source_artifacts[artifact_name])
            except InvalidArtifactPath as exc:
                raise InvalidStageInput(f"selected input {key!r} is invalid: {exc}") from exc
            named[key] = path
            paths.append(path)
    return named, paths


def build_delegate_payload(
    *,
    pipeline_id: str,
    topic: str,
    stage_definition: Mapping[str, Any],
    task_id: str,
    named_inputs: Mapping[str, str],
    context_paths: Sequence[str],
) -> dict[str, Any]:
    """Build the complete deterministic delegate/result contract."""
    return {
        "goal": stage_definition["goal"].format(topic=topic),
        "contextPaths": list(context_paths),
        "namedInputs": dict(named_inputs),
        "pipeline": {
            "pipelineId": pipeline_id,
            "stageId": stage_definition["id"],
            "taskId": task_id,
        },
        "expectedOutputs": list(stage_definition["outputs"]),
        "resultContract": {
            "status": "succeeded|failed",
            "summary": "non-empty string",
            "artifacts": {"declared-output-name": "project-relative-existing-path"},
            "error": "required when failed",
        },
    }


def validate_stage_report(
    *,
    envelope: Mapping[str, Any],
    expected_task_id: str,
    expected_sender: str,
    declared_outputs: Sequence[str],
    project_root: Path,
) -> dict[str, Any]:
    """Validate exact report identity, schema, result semantics, and artifacts."""
    if not isinstance(envelope, Mapping):
        raise InvalidStageReport("report envelope must be a mapping")
    if envelope.get("type") != "report":
        raise InvalidStageReport("envelope type must be 'report'")
    if envelope.get("taskId") != expected_task_id:
        raise InvalidStageReport("report taskId does not match dispatched task")
    if envelope.get("from") != expected_sender:
        raise InvalidStageReport("report sender does not match dispatched instance")
    if envelope.get("to") != catalog.HUB:
        raise InvalidStageReport("report recipient must be orchestrator")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise InvalidStageReport("report payload must be a mapping")
    keys = _mapping_keys(payload, "report payload")
    allowed = {"status", "summary", "artifacts", "error", "question", "options"}
    if not {"status", "summary", "artifacts"}.issubset(keys) or not set(keys).issubset(allowed):
        raise InvalidStageReport("report payload has missing or unsupported fields")
    status = payload.get("status")
    if status not in {"succeeded", "failed", "needs_attention"}:
        raise InvalidStageReport("report status must be succeeded, failed, or needs_attention")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidStageReport("report summary must be a non-blank string")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise InvalidStageReport("report artifacts must be a mapping")
    artifact_keys = _mapping_keys(artifacts, "report artifacts")
    if any(not isinstance(key, str) or not _IDENTIFIER.fullmatch(key) for key in artifact_keys):
        raise InvalidStageReport("report contains an invalid artifact name")
    declared = list(declared_outputs)
    if any(key not in declared for key in artifact_keys):
        raise InvalidStageReport("report contains an undeclared artifact")
    if status == "succeeded" and (len(artifact_keys) != len(declared) or set(artifact_keys) != set(declared)):
        raise InvalidStageReport("successful report must contain every declared artifact exactly")
    error = payload.get("error")
    question = payload.get("question")
    options = payload.get("options")
    if status == "needs_attention":
        # The human-facing question is mandatory; it is stored as the stage error
        # (the machine-facing reason) so existing attention handling applies.
        if not isinstance(question, str) or not question.strip():
            raise InvalidStageReport("needs_attention report requires a non-blank question")
        if options is not None:
            if not isinstance(options, list) or not all(isinstance(o, str) and o.strip() for o in options):
                raise InvalidStageReport("needs_attention options must be a list of non-blank strings")
        # error is optional here; fall back to the question for storage.
        error = error or question
    elif status == "failed":
        if not isinstance(error, str) or not error.strip():
            raise InvalidStageReport("failed report requires a non-blank error")
    if status == "succeeded" and error is not None:
        raise InvalidStageReport("successful report must not contain an error")
    normalized: dict[str, str] = {}
    for name, path in artifacts.items():
        normalized[name] = validate_artifact_path(project_root, path)
    return {
        "status": status,
        "summary": summary,
        "artifacts": normalized,
        "error": error,
        "question": question if status == "needs_attention" else None,
        "options": options if status == "needs_attention" else None,
    }


def load_committed_execution(
    agency_root: Path,
    pipeline_id: str,
    *,
    lock_owner: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one active bound run and its exact validated catalog definition."""
    data = pipeline_state.load_state(agency_root)
    if data.get("activePipelineId") != pipeline_id:
        raise PipelineRunnerError(f"pipeline {pipeline_id!r} is not the active run")
    run = pipeline_state.get_run(agency_root, pipeline_id)
    if run.get("status") not in {"running", "needs_attention"}:
        raise PipelineRunnerError(f"pipeline {pipeline_id!r} is not active")
    if not run.get("runnerInstance") or not run.get("runnerSurface"):
        raise PipelineRunnerError("active pipeline runner is not bound")
    lock = pipeline_state.read_lock(agency_root)
    if lock is None or lock.get("pipelineId") != pipeline_id:
        raise PipelineRunnerError("active pipeline lock does not match the run")
    if lock_owner is not None and lock.get("ownerId") != lock_owner:
        raise PipelineRunnerError("active pipeline lock owner does not match")
    try:
        loaded = catalog.load_pipelines(agency_root)
    except Exception as exc:
        raise PipelineDefinitionMismatch(f"pipeline catalog is invalid: {exc}") from exc
    definitions = loaded.get("pipelines", {}) if isinstance(loaded, Mapping) else {}
    definition = definitions.get(run["pipelineName"])
    if not isinstance(definition, Mapping):
        raise PipelineDefinitionMismatch(
            f"pipeline {run['pipelineName']!r} is missing from the validated catalog"
        )
    stages = definition.get("stages", [])
    expected = [
        (stage["id"], stage["role"], f"pl-{pipeline_id}-s{index}")
        for index, stage in enumerate(stages, 1)
    ]
    committed = [(stage["id"], stage["role"], stage["taskId"]) for stage in run["stages"]]
    if definition.get("onFailure") != run.get("onFailure") or expected != committed:
        raise PipelineDefinitionMismatch("catalog stage order, identity, role, or failure policy changed")
    digest = pipeline_state.pipeline_definition_digest(definition)
    if digest != run.get("definitionDigest"):
        raise PipelineDefinitionMismatch("catalog operational definition digest changed")
    return dict(definition), run


def _attention(
    agency_root: Path,
    run: Mapping[str, Any],
    stage: Mapping[str, Any],
    lock_owner: str,
    detail: str,
) -> None:
    if stage["status"] == "needs_attention":
        return
    pipeline_state.transition_stage(
        agency_root,
        run["pipelineId"],
        stage["id"],
        "needs_attention",
        lock_owner=lock_owner,
        error=detail or "pipeline execution requires operator attention",
    )


def _dependency_failures(
    stage_definition: Mapping[str, Any], stage_records: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    failed: list[str] = []
    for selector in stage_definition.get("inputs", []):
        source = stage_records.get(selector["stage"])
        if source and source.get("status") in {"failed", "dependency_failed"}:
            failed.append(selector["stage"])
    return list(dict.fromkeys(failed))


def _persist_result(
    agency_root: Path,
    run: Mapping[str, Any],
    stage: Mapping[str, Any],
    result: Mapping[str, Any],
    lock_owner: str,
    *,
    reconciled: bool,
) -> None:
    kwargs = {
        "lock_owner": lock_owner,
        "summary": result["summary"],
        "artifacts": result["artifacts"],
        "error": result["error"],
    }
    if result["status"] == "needs_attention":
        # A needs_attention report carries the human-facing question in `error`
        # (see validate_stage_report). No operator response exists yet.
        kwargs["operator_response"] = None
    if reconciled:
        pipeline_state.record_reconciled_result(
            agency_root,
            run["pipelineId"],
            stage["id"],
            status=result["status"],
            **kwargs,
        )
    else:
        pipeline_state.transition_stage(
            agency_root,
            run["pipelineId"],
            stage["id"],
            result["status"],
            **kwargs,
        )


def _received_report(value: WaitResult | Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """Normalize concrete receipt results while retaining older in-memory fakes."""
    if isinstance(value, WaitResult):
        if value.status != "report" or value.envelope is None:
            raise InvalidStageReport("report lookup did not return a report envelope")
        return value.envelope, value.receipt
    if isinstance(value, Mapping):
        return value, None
    raise InvalidStageReport("report lookup returned an invalid result")


def _ack_persisted_report(control_plane: ControlPlane, receipt: str | None) -> None:
    """Best-effort ack only after the stage result is durably committed.

    A failed ack leaves a recoverable processing receipt.  It must not turn
    already-persisted work into execution uncertainty or trigger a retry.
    """
    if receipt is None:
        return
    try:
        control_plane.ack_stage_report(receipt)
    except Exception:
        pass


def synthesize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable durable state only; this function performs no delivery."""
    stages = [
        {
            "id": stage["id"],
            "role": stage["role"],
            "taskId": stage["taskId"],
            "instance": stage["assignedInstance"],
            "status": stage["status"],
            "summary": stage["summary"],
            "artifacts": dict(stage["artifacts"]),
            "error": stage["error"],
        }
        for stage in run["stages"]
    ]
    decisive = next((stage for stage in reversed(stages) if stage["status"] != "pending"), None)
    summary = ""
    if decisive is not None:
        summary = decisive["summary"] or decisive["error"] or decisive["status"]
    artifacts = {
        stage["id"]: dict(stage["artifacts"])
        for stage in stages
        if stage["artifacts"]
    }
    return {
        "pipelineId": run["pipelineId"],
        "pipelineName": run["pipelineName"],
        "topic": run["topic"],
        "status": run["status"],
        "summary": summary,
        "stages": stages,
        "artifacts": artifacts,
    }


def _run_pipeline_locked(
    agency_root: Path,
    project_root: Path,
    pipeline_id: str,
    lock_owner: str,
    control_plane: ControlPlane,
    resume: bool = False,
    wait_timeout: float = 120.0,
) -> dict[str, Any]:
    """Advance one run while the caller holds the project execution guard."""
    try:
        definition, run = load_committed_execution(
            agency_root, pipeline_id, lock_owner=lock_owner
        )
    except PipelineDefinitionMismatch as exc:
        # Catalog drift is durable pre-dispatch uncertainty when ownership is valid.
        run = pipeline_state.get_run(agency_root, pipeline_id)
        lock = pipeline_state.read_lock(agency_root)
        if lock and lock.get("pipelineId") == pipeline_id and lock.get("ownerId") == lock_owner:
            current = next(
                (stage for stage in run["stages"] if stage["id"] == run.get("currentStageId")),
                None,
            )
            if current and current["status"] in {"pending", "dispatched"}:
                _attention(agency_root, run, current, lock_owner, str(exc))
        return synthesize_run(pipeline_state.get_run(agency_root, pipeline_id))

    definitions = {stage["id"]: stage for stage in definition["stages"]}
    for stage_id in [stage["id"] for stage in definition["stages"]]:
        run = pipeline_state.get_run(agency_root, pipeline_id)
        stage = next(record for record in run["stages"] if record["id"] == stage_id)
        stage_definition = definitions[stage_id]
        status = stage["status"]

        if status == "succeeded":
            continue
        if status == "failed":
            if run["onFailure"] == "stop":
                break
            continue
        if status == "dependency_failed":
            continue
        if status == "needs_attention":
            if not resume or not stage.get("assignedInstance") or not stage.get("dispatchedAt"):
                break
            try:
                found = control_plane.find_existing_report(
                    pipeline_id=pipeline_id,
                    task_id=stage["taskId"],
                    expected_sender=stage["assignedInstance"],
                )
                if found is None:
                    break
                envelope, receipt = _received_report(found)
                result = validate_stage_report(
                    envelope=envelope,
                    expected_task_id=stage["taskId"],
                    expected_sender=stage["assignedInstance"],
                    declared_outputs=stage_definition["outputs"],
                    project_root=project_root,
                )
                _persist_result(agency_root, run, stage, result, lock_owner, reconciled=True)
                _ack_persisted_report(control_plane, receipt)
            except Exception:
                break
            if result["status"] == "failed" and run["onFailure"] == "stop":
                break
            continue
        if status == "dispatched":
            if not resume:
                _attention(
                    agency_root,
                    run,
                    stage,
                    lock_owner,
                    "Dispatched work is uncertain; resume reconciliation is required",
                )
                break
            try:
                found = control_plane.find_existing_report(
                    pipeline_id=pipeline_id,
                    task_id=stage["taskId"],
                    expected_sender=stage["assignedInstance"],
                )
            except Exception as exc:
                _attention(agency_root, run, stage, lock_owner, f"Report reconciliation failed: {exc}")
                break
            if found is None:
                _attention(
                    agency_root,
                    run,
                    stage,
                    lock_owner,
                    f"No report exists for dispatched task {stage['taskId']}; automatic retry is prohibited",
                )
                break
            try:
                envelope, receipt = _received_report(found)
                result = validate_stage_report(
                    envelope=envelope,
                    expected_task_id=stage["taskId"],
                    expected_sender=stage["assignedInstance"],
                    declared_outputs=stage_definition["outputs"],
                    project_root=project_root,
                )
                _persist_result(agency_root, run, stage, result, lock_owner, reconciled=True)
                _ack_persisted_report(control_plane, receipt)
            except Exception as exc:
                _attention(agency_root, run, stage, lock_owner, f"Invalid reconciled report: {exc}")
                break
            if result["status"] == "failed" and run["onFailure"] == "stop":
                break
            continue

        stage_records = {record["id"]: record for record in run["stages"]}
        failed_sources = _dependency_failures(stage_definition, stage_records)
        if failed_sources:
            pipeline_state.transition_stage(
                agency_root,
                pipeline_id,
                stage_id,
                "dependency_failed",
                lock_owner=lock_owner,
                error="Required input stages failed: " + ", ".join(failed_sources),
            )
            continue
        try:
            named_inputs, context_paths = resolve_named_inputs(
                project_root=project_root,
                stage_definition=stage_definition,
                stage_records=stage_records,
            )
        except Exception as exc:
            _attention(agency_root, run, stage, lock_owner, f"Input resolution failed: {exc}")
            break
        try:
            reserved = control_plane.reserve_stage_instance(
                pipeline_id=pipeline_id,
                role=stage["role"],
                task_id=stage["taskId"],
            )
            instance = reserved.instance
            if not isinstance(instance, str) or not _IDENTIFIER.fullmatch(instance):
                raise PipelineRunnerError("reservation returned an invalid instance identifier")
        except Exception as exc:
            _attention(agency_root, run, stage, lock_owner, f"Stage reservation failed: {exc}")
            break
        pipeline_state.record_dispatched(
            agency_root,
            pipeline_id,
            stage_id,
            lock_owner=lock_owner,
            assigned_instance=instance,
        )
        try:
            control_plane.spawn_stage(
                pipeline_id=pipeline_id,
                role=stage["role"],
                task_id=stage["taskId"],
                instance=instance,
            )
        except Exception as exc:
            dispatched = next(
                record
                for record in pipeline_state.get_run(agency_root, pipeline_id)["stages"]
                if record["id"] == stage_id
            )
            _attention(agency_root, run, dispatched, lock_owner, f"Stage spawn is uncertain: {exc}")
            break
        payload = build_delegate_payload(
            pipeline_id=pipeline_id,
            topic=run["topic"],
            stage_definition=stage_definition,
            task_id=stage["taskId"],
            named_inputs=named_inputs,
            context_paths=context_paths,
        )
        try:
            control_plane.delegate_stage(
                pipeline_id=pipeline_id,
                instance=instance,
                task_id=stage["taskId"],
                payload=payload,
            )
        except Exception as exc:
            dispatched = next(
                record
                for record in pipeline_state.get_run(agency_root, pipeline_id)["stages"]
                if record["id"] == stage_id
            )
            _attention(agency_root, run, dispatched, lock_owner, f"Delegate delivery is uncertain: {exc}")
            break
        try:
            waited = control_plane.wait_stage(
                pipeline_id=pipeline_id,
                task_id=stage["taskId"],
                expected_sender=instance,
                timeout=wait_timeout,
            )
        except Exception as exc:
            waited = WaitResult("timeout", detail=f"wait failed: {exc}")
        if waited.status != "report":
            current = next(
                record
                for record in pipeline_state.get_run(agency_root, pipeline_id)["stages"]
                if record["id"] == stage_id
            )
            detail = waited.detail or f"stage wait ended with {waited.status!r}"
            _attention(agency_root, run, current, lock_owner, detail)
            break
        try:
            result = validate_stage_report(
                envelope=waited.envelope,
                expected_task_id=stage["taskId"],
                expected_sender=instance,
                declared_outputs=stage_definition["outputs"],
                project_root=project_root,
            )
            current = next(
                record
                for record in pipeline_state.get_run(agency_root, pipeline_id)["stages"]
                if record["id"] == stage_id
            )
            _persist_result(agency_root, run, current, result, lock_owner, reconciled=False)
            _ack_persisted_report(control_plane, waited.receipt)
        except Exception as exc:
            current = next(
                record
                for record in pipeline_state.get_run(agency_root, pipeline_id)["stages"]
                if record["id"] == stage_id
            )
            _attention(agency_root, run, current, lock_owner, f"Invalid stage report: {exc}")
            break
        if result["status"] == "failed" and run["onFailure"] == "stop":
            break
        if result["status"] == "needs_attention":
            # Human-in-the-loop: stop the run; the runtime notifies the hub and
            # the operator answers, then resumes (which re-dispatches this stage
            # with the answer injected). The stage is NOT advanced.
            break

    return synthesize_run(pipeline_state.get_run(agency_root, pipeline_id))


def run_pipeline(
    agency_root: Path,
    project_root: Path,
    pipeline_id: str,
    lock_owner: str,
    control_plane: ControlPlane,
    resume: bool = False,
    wait_timeout: float = 120.0,
) -> dict[str, Any]:
    """Serialize and advance one run without retry, delivery, teardown, or release."""
    with pipeline_state.pipeline_execution_guard(agency_root):
        return _run_pipeline_locked(
            agency_root,
            project_root,
            pipeline_id,
            lock_owner,
            control_plane,
            resume=resume,
            wait_timeout=wait_timeout,
        )
