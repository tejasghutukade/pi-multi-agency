#!/usr/bin/env python3
"""Concrete file-bus control plane and lifecycle for the fixed pipeline runner."""

from __future__ import annotations

import json
import math
import os
import re
import time
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import agency_ctl as ctl
import agent_spawn
import bus
import catalog
import ledger
import pipeline_runner
import pipeline_state
from agency_paths import agency_root as configured_agency_root
from agency_paths import project_root as configured_project_root

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PipelineRuntimeError(RuntimeError):
    """A concrete runtime or runner claim failed closed."""


def _safe_identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PipelineRuntimeError(f"{context} must be a safe identifier")
    return value


def _read_envelope(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _verify_sender_auth(root: Path, envelope: Mapping[str, Any], expected_sender: str) -> dict[str, Any]:
    auth = envelope.get("senderAuth")
    if not isinstance(auth, Mapping) or set(auth) != {"instanceId", "intercomName", "surface"}:
        raise PipelineRuntimeError("pipeline envelope is missing exact sender authentication")
    if auth.get("intercomName") != expected_sender or envelope.get("from") != expected_sender:
        raise PipelineRuntimeError("pipeline envelope sender authentication name mismatch")
    sessions = ledger.load_sessions(root)
    rows = [
        row
        for row in (sessions.get("instances") or [])
        if row.get("cmuxSurface") == auth.get("surface")
    ]
    if len(rows) != 1:
        raise PipelineRuntimeError("pipeline envelope sender is not uniquely bound in the ledger")
    row = rows[0]
    if row.get("intercomName") != expected_sender:
        raise PipelineRuntimeError("pipeline envelope sender surface belongs to another instance")
    if row.get("instanceId") != auth.get("instanceId"):
        raise PipelineRuntimeError("pipeline envelope sender instanceId mismatch")
    if ctl.surface_alive(auth.get("surface")) is not True:
        raise PipelineRuntimeError("pipeline envelope sender surface is not confirmed alive")
    return row


def _quarantine(root: Path, inbox_name: str, path: Path) -> Path:
    rejected = bus.ensure_inbox(root, inbox_name) / "rejected" / path.name
    try:
        path.replace(rejected)
    except FileNotFoundError:
        if not rejected.is_file():
            raise
    return rejected


def prepare_pipeline_report(
    root: Path,
    project: Path,
    *,
    from_instance: str,
    task_id: str,
    payload: Any,
) -> dict[str, Any]:
    """Validate an active stage report and its live, exact sender binding."""
    ownership = pipeline_state.find_task_ownership(root, task_id, active_only=True)
    if ownership is None:
        return {"pipelineOwned": False}
    if ownership.get("taskKind") != "stage":
        raise PipelineRuntimeError("final pipeline task IDs cannot use pipeline-report")
    if ownership.get("stageStatus") not in {"dispatched", "needs_attention"}:
        raise PipelineRuntimeError("pipeline report task is not dispatched or awaiting attention")
    expected_sender = ownership.get("expectedSender")
    if not isinstance(expected_sender, str) or not expected_sender:
        raise PipelineRuntimeError("pipeline report task has no expected sender")
    if from_instance != expected_sender:
        raise PipelineRuntimeError(
            f"pipeline report sender mismatch: expected {expected_sender!r}, got {from_instance!r}"
        )

    surface, _pane = ctl.caller_surface()
    sessions = ledger.load_sessions(root)
    rows = [
        row
        for row in (sessions.get("instances") or [])
        if row.get("cmuxSurface") == surface
    ]
    if len(rows) != 1:
        raise PipelineRuntimeError(
            f"pipeline report caller surface {surface!r} maps to {len(rows)} rows"
        )
    row = rows[0]
    if row.get("intercomName") != expected_sender:
        raise PipelineRuntimeError("pipeline report caller surface belongs to another sender")
    instance_id = row.get("instanceId")
    if not isinstance(instance_id, str) or not instance_id:
        raise PipelineRuntimeError("pipeline report sender instanceId is missing")
    if row.get("taskId") != task_id:
        raise PipelineRuntimeError("pipeline report sender row taskId does not match exactly")
    if ctl.surface_alive(surface) is not True:
        raise PipelineRuntimeError("pipeline report sender surface is not confirmed alive")

    loaded = catalog.load_pipelines(root)
    definition = (loaded.get("pipelines") or {}).get(ownership.get("pipelineName"))
    if not isinstance(definition, Mapping):
        raise PipelineRuntimeError("pipeline report definition is unavailable")
    stage_definition = next(
        (
            stage
            for stage in (definition.get("stages") or [])
            if stage.get("id") == ownership.get("stageId")
        ),
        None,
    )
    if not isinstance(stage_definition, Mapping):
        raise PipelineRuntimeError("pipeline report stage definition is unavailable")

    normalized = pipeline_runner.validate_stage_report(
        envelope={
            "type": "report",
            "from": from_instance,
            "to": catalog.HUB,
            "taskId": task_id,
            "payload": payload,
        },
        expected_task_id=task_id,
        expected_sender=expected_sender,
        declared_outputs=stage_definition["outputs"],
        project_root=project,
    )
    if normalized.get("error") is None:
        normalized.pop("error", None)
    return {
        "pipelineOwned": True,
        "payload": normalized,
        "ownership": ownership,
    }


def send_pipeline_report(
    root: Path,
    project: Path,
    *,
    from_instance: str,
    task_id: str,
    payload: Any,
) -> dict[str, Any]:
    """Validate then publish one authenticated, durable stage report."""
    prepared = prepare_pipeline_report(
        root,
        project,
        from_instance=from_instance,
        task_id=task_id,
        payload=payload,
    )
    if prepared["pipelineOwned"] is False:
        return prepared
    result = ctl.bus_run(
        root,
        [
            "send",
            "--from",
            from_instance,
            "--to",
            catalog.HUB,
            "--type",
            "report",
            "--task-id",
            task_id,
            "--payload-json",
            json.dumps(prepared["payload"]),
            "--require-caller",
        ],
    )
    return {**prepared, "bus": result}


class AgencyControlPlane:
    """Effectful adapter over authenticated ctl operations and the durable bus."""

    def __init__(self, root: Path, project: Path | None = None):
        self.root = Path(root)
        self.project = Path(project) if project is not None else configured_project_root()

    def _authorize_stage(
        self, pipeline_id: str, task_id: str, expected_sender: str | None = None
    ) -> dict[str, Any]:
        ctl.require_operation_authority(self.root, pipeline_id=pipeline_id)
        return ctl.require_active_dispatched_stage(
            self.root,
            pipeline_id,
            task_id,
            expected_sender=expected_sender,
        )

    def _authorize_report_lookup(
        self, pipeline_id: str, task_id: str, expected_sender: str
    ) -> dict[str, Any]:
        ctl.require_operation_authority(self.root, pipeline_id=pipeline_id)
        ownership = pipeline_state.find_task_ownership(
            self.root, task_id, active_only=True
        )
        if (
            ownership is None
            or ownership.get("pipelineId") != pipeline_id
            or ownership.get("taskKind") != "stage"
            or ownership.get("stageStatus") not in {"dispatched", "needs_attention"}
            or ownership.get("expectedSender") != expected_sender
        ):
            raise PipelineRuntimeError("report lookup does not match dispatched stage ownership")
        return ownership

    def reserve_stage_instance(
        self, *, pipeline_id: str, role: str, task_id: str
    ) -> pipeline_runner.SpawnResult:
        """Choose from catalog/ledger reads only; never mutate or launch anything."""
        _safe_identifier(role, "stage role")
        # Reservation occurs while the stage is pending, so only runner authority
        # (not dispatched-task authority) is available at this point.
        ctl.require_operation_authority(self.root, pipeline_id=pipeline_id)
        run = pipeline_state.get_active_run(self.root)
        current = None if run is None else next(
            (
                stage
                for stage in (run.get("stages") or [])
                if stage.get("id") == run.get("currentStageId")
            ),
            None,
        )
        if (
            run is None
            or run.get("pipelineId") != pipeline_id
            or current is None
            or current.get("status") != "pending"
            or current.get("role") != role
            or current.get("taskId") != task_id
        ):
            raise PipelineRuntimeError("reservation does not match the current pending stage")
        agents = catalog.load_agents(self.root)
        configured_roles = agents.get("agents") or {}
        if role not in configured_roles or role == "pipeline-runner":
            raise PipelineRuntimeError(f"unknown or unavailable pipeline stage role: {role}")
        definition = catalog.role_defaults(agents, role)
        sessions = ledger.load_sessions(self.root)
        idle = ledger.find_idle_role(sessions, role)
        if idle is not None:
            name = _safe_identifier(idle.get("intercomName"), "idle instance name")
            return pipeline_runner.SpawnResult(name)

        lifecycle = definition.get("lifecycleDefault") or "temporary"
        if lifecycle not in {"temporary", "persistent"}:
            raise PipelineRuntimeError(f"invalid lifecycle for role {role!r}")
        if lifecycle == "persistent":
            name = role
            if ledger.find_instance(sessions, name) is not None:
                raise PipelineRuntimeError(f"persistent role instance {name!r} is not idle")
        else:
            # Random identifier generation has no roster, bus, or cmux effect.
            for _attempt in range(16):
                name = ledger.make_instance_name(role, lifecycle)
                if ledger.find_instance(sessions, name) is None:
                    break
            else:
                raise PipelineRuntimeError(f"could not reserve an unused instance for {role!r}")
        return pipeline_runner.SpawnResult(_safe_identifier(name, "reserved instance name"))

    def spawn_stage(
        self, *, pipeline_id: str, role: str, task_id: str, instance: str
    ) -> None:
        self._authorize_stage(pipeline_id, task_id, instance)
        result = agent_spawn.spawn_specialist(
            role,
            name=instance,
            reuse=True,
            pipeline_id=pipeline_id,
        )
        returned = result.get("instance")
        returned_name = returned.get("intercomName") if isinstance(returned, Mapping) else returned
        if returned_name != instance:
            raise PipelineRuntimeError(
                f"spawn returned {returned_name!r}, not reserved instance {instance!r}"
            )

    def delegate_stage(
        self,
        *,
        pipeline_id: str,
        instance: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._authorize_stage(pipeline_id, task_id, instance)
        args = Namespace(
            pipeline_id=pipeline_id,
            recovery=False,
            to=instance,
            task_id=task_id,
            workflow_id=None,
            payload_json=json.dumps(dict(payload)),
            goal=None,
            context_paths=None,
            success_criteria=None,
            constraints=None,
            charter_path=None,
            skill_path=None,
            output_shape=None,
            stop_rules=None,
            prepare_only=False,
            no_bus=False,
        )
        # cmd_delegate is the existing authenticated path; suppress only its CLI
        # presentation because the pure driver consumes no stdout.
        with redirect_stdout(StringIO()):
            ctl.cmd_delegate(args)

    def wait_stage(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        expected_sender: str,
        timeout: float,
    ) -> pipeline_runner.WaitResult:
        self._authorize_stage(pipeline_id, task_id, expected_sender)
        result = ctl.bus_run(
            self.root,
            [
                "wait",
                "--as",
                catalog.HUB,
                "--task-id",
                task_id,
                "--from",
                expected_sender,
                "--type",
                "report",
                "--timeout",
                str(timeout),
                "--interval",
                str(min(0.5, max(0.05, timeout or 0.05))),
            ],
            timeout=max(30.0, float(timeout) + 15.0),
        )
        if result.get("status") == "message" and result.get("type") == "report":
            envelope = result.get("envelope")
            receipt = result.get("path")
            if not isinstance(envelope, Mapping) or not isinstance(receipt, str):
                raise PipelineRuntimeError("file-bus wait returned an invalid processing receipt")
            self._validate_report_candidate(
                pipeline_id=pipeline_id,
                task_id=task_id,
                expected_sender=expected_sender,
                envelope=envelope,
            )
            return pipeline_runner.WaitResult("report", envelope, receipt=receipt)
        if result.get("status") == "timeout":
            sessions = ledger.load_sessions(self.root)
            assigned = ledger.find_instance(sessions, expected_sender)
            if assigned and ctl.surface_alive(assigned.get("cmuxSurface")) is False:
                return pipeline_runner.WaitResult("pane_dead", detail="assigned pane is not alive")
            return pipeline_runner.WaitResult("timeout", detail="stage report wait timed out")
        return pipeline_runner.WaitResult(
            "timeout",
            detail=f"stage wait returned non-report message {result.get('type')!r}",
        )

    @staticmethod
    def _matches_report(envelope: Mapping[str, Any], task_id: str, sender: str) -> bool:
        return (
            envelope.get("type") == "report"
            and envelope.get("taskId") == task_id
            and envelope.get("from") == sender
            and envelope.get("to") == catalog.HUB
        )

    def _validate_report_candidate(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        expected_sender: str,
        envelope: Mapping[str, Any],
    ) -> None:
        _verify_sender_auth(self.root, envelope, expected_sender)
        ownership = pipeline_state.find_task_ownership(self.root, task_id, active_only=True)
        if ownership is None or ownership.get("pipelineId") != pipeline_id:
            raise PipelineRuntimeError("report task has no active pipeline ownership")
        run = pipeline_state.get_run(self.root, pipeline_id)
        loaded = catalog.load_pipelines(self.root)
        definition = (loaded.get("pipelines") or {}).get(run["pipelineName"])
        stage_definition = None if not isinstance(definition, Mapping) else next(
            (
                stage
                for stage in (definition.get("stages") or [])
                if stage.get("id") == ownership.get("stageId")
            ),
            None,
        )
        if not isinstance(stage_definition, Mapping):
            raise PipelineRuntimeError("report stage definition is unavailable")
        pipeline_runner.validate_stage_report(
            envelope=envelope,
            expected_task_id=task_id,
            expected_sender=expected_sender,
            declared_outputs=stage_definition["outputs"],
            project_root=self.project,
        )

    def find_existing_report(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        expected_sender: str,
    ) -> pipeline_runner.WaitResult | None:
        self._authorize_report_lookup(pipeline_id, task_id, expected_sender)
        inbox = bus.ensure_inbox(self.root, catalog.HUB)
        processing_dir = inbox / "processing"
        for path in sorted(processing_dir.glob("*.json")):
            envelope = _read_envelope(path)
            if envelope is None or not self._matches_report(envelope, task_id, expected_sender):
                continue
            try:
                self._validate_report_candidate(
                    pipeline_id=pipeline_id,
                    task_id=task_id,
                    expected_sender=expected_sender,
                    envelope=envelope,
                )
            except Exception:
                _quarantine(self.root, catalog.HUB, path)
                continue
            return pipeline_runner.WaitResult("report", envelope, receipt=str(path))

        for source in bus.list_pending(self.root, catalog.HUB):
            envelope = _read_envelope(source)
            if envelope is None or not self._matches_report(envelope, task_id, expected_sender):
                continue
            try:
                self._validate_report_candidate(
                    pipeline_id=pipeline_id,
                    task_id=task_id,
                    expected_sender=expected_sender,
                    envelope=envelope,
                )
            except Exception:
                _quarantine(self.root, catalog.HUB, source)
                continue
            try:
                processing, claimed = bus.claim_pending(self.root, catalog.HUB, source)
            except FileNotFoundError:
                continue
            return pipeline_runner.WaitResult("report", claimed, receipt=str(processing))
        return None

    def surface_alive(self, instance: str) -> bool:
        """Reuse a prior stage instance only when its cmux surface is live.

        `instance` is an intercomName/intercomId, not a cmux surface ref, so it
        must be resolved through the ledger to the bound `cmuxSurface` before the
        cmux surface query is consulted. Returns False for unknown/!bound rows.
        """
        if not isinstance(instance, str) or not instance:
            return False
        sessions = ledger.load_sessions(self.root)
        row = ledger.find_instance(sessions, instance)
        if not row:
            return False
        surface = row.get("cmuxSurface")
        if not surface:
            return False
        return ctl.surface_alive(surface) is True

    def ack_stage_report(self, receipt: str) -> None:
        if not isinstance(receipt, str) or not receipt:
            raise PipelineRuntimeError("processing receipt must be a non-empty path")
        inbox = bus.ensure_inbox(self.root, catalog.HUB)
        processing_dir = (inbox / "processing").resolve()
        done_dir = (inbox / "done").resolve()
        candidate = Path(receipt)
        try:
            resolved_parent = candidate.parent.resolve()
        except OSError as exc:
            raise PipelineRuntimeError(f"invalid processing receipt: {exc}") from exc
        if resolved_parent != processing_dir or candidate.name in {"", ".", ".."}:
            raise PipelineRuntimeError("processing receipt is outside the hub processing inbox")
        if candidate.is_file():
            bus.move_to_done(self.root, catalog.HUB, candidate)
            return
        if (done_dir / candidate.name).is_file():
            return
        raise PipelineRuntimeError("processing receipt no longer exists")


def _runner_row(root: Path, instance: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    _safe_identifier(instance, "pipeline-runner instance")
    surface, _pane = ctl.caller_surface()
    if not isinstance(surface, str) or not surface:
        raise PipelineRuntimeError("pipeline runner must be called from a cmux surface")
    sessions = ledger.load_sessions(root)
    surface_rows = [
        row for row in (sessions.get("instances") or []) if row.get("cmuxSurface") == surface
    ]
    if len(surface_rows) != 1:
        raise PipelineRuntimeError(
            f"pipeline runner caller surface {surface!r} maps to {len(surface_rows)} rows"
        )
    row = surface_rows[0]
    name_rows = [
        candidate
        for candidate in (sessions.get("instances") or [])
        if candidate.get("intercomName") == instance
    ]
    if len(name_rows) != 1:
        raise PipelineRuntimeError(
            f"pipeline runner identity {instance!r} maps to {len(name_rows)} rows"
        )
    if row is not name_rows[0] or row.get("role") != "pipeline-runner":
        raise PipelineRuntimeError("pipeline runner surface does not match the requested runner identity")
    if ctl.surface_alive(surface) is not True:
        raise PipelineRuntimeError("pipeline runner surface is not confirmed alive")
    return sessions, row, surface


def _initial_header(envelope: Mapping[str, Any], instance: str) -> bool:
    return (
        envelope.get("from") == catalog.HUB
        and envelope.get("to") == instance
        and envelope.get("type") == "delegate"
    )


def _recover_claim_path(root: Path, instance: str, claim: Mapping[str, Any]) -> Path | None:
    name = claim.get("receiptName")
    if not isinstance(name, str) or Path(name).name != name:
        return None
    inbox = bus.ensure_inbox(root, instance)
    for directory in ("processing", "done"):
        candidate = inbox / directory / name
        if candidate.is_file():
            return candidate
    return None


def _claim_initial_delegate(
    root: Path,
    instance: str,
    row: Mapping[str, Any],
    timeout: float,
) -> tuple[Path, dict[str, Any]]:
    inbox = bus.ensure_inbox(root, instance)
    last_error: Exception | None = None
    prior = row.get("pipelineClaim")
    if isinstance(prior, Mapping):
        recovered = _recover_claim_path(root, instance, prior)
        if recovered is not None:
            envelope = _read_envelope(recovered)
            if envelope is not None and _initial_header(envelope, instance):
                try:
                    _validate_initial_delegate(root, instance, envelope)
                    return recovered, envelope
                except Exception as exc:
                    last_error = exc
                    if recovered.parent.name == "processing":
                        _quarantine(root, instance, recovered)

    # A process may have crashed after pending -> processing but before saving its
    # durable session claim. Validate and quarantine poison before choosing work.
    for path in sorted((inbox / "processing").glob("*.json")):
        envelope = _read_envelope(path)
        if envelope is None or not _initial_header(envelope, instance):
            continue
        try:
            _validate_initial_delegate(root, instance, envelope)
        except Exception as exc:
            last_error = exc
            _quarantine(root, instance, path)
            continue
        return path, envelope

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for source in bus.list_pending(root, instance):
            envelope = _read_envelope(source)
            if envelope is None or not _initial_header(envelope, instance):
                continue
            try:
                _validate_initial_delegate(root, instance, envelope)
            except Exception as exc:
                last_error = exc
                _quarantine(root, instance, source)
                continue
            try:
                processing, claimed = bus.claim_pending(root, instance, source)
            except FileNotFoundError:
                continue
            return processing, claimed
        if time.monotonic() >= deadline:
            detail = f"; last rejected delegate: {last_error}" if last_error else ""
            raise PipelineRuntimeError(
                "timed out waiting for a valid initial pipeline delegate" + detail
            )
        time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))


def _validate_initial_delegate(
    root: Path,
    instance: str,
    envelope: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _initial_header(envelope, instance):
        raise PipelineRuntimeError("initial pipeline delegate has invalid routing")
    _verify_sender_auth(root, envelope, catalog.HUB)
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise PipelineRuntimeError("initial pipeline delegate payload must be an object")
    required = ("pipelineId", "pipelineName", "topic")
    if any(not isinstance(payload.get(key), str) or not payload.get(key) for key in required):
        raise PipelineRuntimeError("initial pipeline delegate requires pipelineId, pipelineName, and topic")
    pipeline_id = _safe_identifier(payload["pipelineId"], "pipelineId")
    run = pipeline_state.get_active_run(root)
    if run is None or run.get("pipelineId") != pipeline_id:
        raise PipelineRuntimeError("initial delegate does not identify the precreated active pipeline")
    if run.get("status") != "running":
        raise PipelineRuntimeError("normal claim requires a running pipeline; use explicit resume for attention")
    if run.get("pipelineName") != payload["pipelineName"] or run.get("topic") != payload["topic"]:
        raise PipelineRuntimeError("initial delegate does not match durable pipeline name/topic")
    if envelope.get("taskId") != run.get("finalTaskId"):
        raise PipelineRuntimeError("initial delegate taskId does not match the pipeline final task")
    lock = pipeline_state.read_lock(root)
    if (
        lock is None
        or lock.get("pipelineId") != pipeline_id
        or lock.get("ownerId") != instance
    ):
        raise PipelineRuntimeError("pipeline lock is not owned by the requested runner")
    return dict(run), dict(payload)


def _save_claim(
    root: Path,
    sessions: dict[str, Any],
    row: dict[str, Any],
    envelope: Mapping[str, Any],
    receipt: Path,
    pipeline_id: str,
) -> None:
    now = ctl.utc_now()
    prior = row.get("pipelineClaim") if isinstance(row.get("pipelineClaim"), Mapping) else {}
    row["activePipelineId"] = pipeline_id
    row["status"] = "working"
    row["taskId"] = envelope.get("taskId")
    row["pipelineClaim"] = {
        "pipelineId": pipeline_id,
        "taskId": envelope.get("taskId"),
        "messageId": envelope.get("id"),
        "receiptName": receipt.name,
        "claimedAt": prior.get("claimedAt") or now,
    }
    row["updatedAt"] = now
    ledger.save_sessions(root, sessions)


def _ack_initial(root: Path, instance: str, receipt: Path) -> None:
    inbox = bus.ensure_inbox(root, instance)
    processing = inbox / "processing" / receipt.name
    done = inbox / "done" / receipt.name
    if processing.is_file():
        bus.move_to_done(root, instance, processing)
    elif not done.is_file():
        raise PipelineRuntimeError("initial pipeline delegate receipt disappeared before ack")


def _mark_initial_acknowledged(root: Path, instance: str, receipt: Path) -> None:
    """Persist completed initial receipt acknowledgement, idempotently."""
    inbox = bus.ensure_inbox(root, instance)
    if not (inbox / "done" / receipt.name).is_file():
        raise PipelineRuntimeError("initial pipeline delegate is not in the done inbox")
    sessions = ledger.load_sessions(root)
    row = ledger.find_instance(sessions, instance)
    if row is None:
        raise PipelineRuntimeError("pipeline runner session disappeared before claim acknowledgement")
    claim = row.get("pipelineClaim")
    if not isinstance(claim, Mapping) or claim.get("receiptName") != receipt.name:
        raise PipelineRuntimeError("pipeline claim does not match the acknowledged receipt")
    if claim.get("acknowledgedAt"):
        return
    row["pipelineClaim"] = {**claim, "acknowledgedAt": ctl.utc_now()}
    row["updatedAt"] = ctl.utc_now()
    ledger.save_sessions(root, sessions)


def _send_final(root: Path, instance: str, run: Mapping[str, Any], synthesis: Mapping[str, Any]) -> None:
    ctl.bus_run(
        root,
        [
            "send",
            "--from",
            instance,
            "--to",
            catalog.HUB,
            "--type",
            "report",
            "--task-id",
            run["finalTaskId"],
            "--message-id",
            run["finalDelivery"]["messageId"],
            "--require-caller",
            "--payload-json",
            json.dumps(dict(synthesis)),
        ],
    )


def _notify_attention(
    root: Path,
    instance: str,
    run: Mapping[str, Any],
    synthesis: Mapping[str, Any],
    *,
    question: str = "",
    options: Sequence[str] | None = None,
    context: Mapping[str, Any] | None = None,
) -> None:
    ctl.bus_run(
        root,
        [
            "send",
            "--from",
            instance,
            "--to",
            catalog.HUB,
            "--type",
            "ask",
            "--task-id",
            run["finalTaskId"],
            "--require-caller",
            "--payload-json",
            json.dumps(
                {
                    "message": question or "Pipeline requires operator attention; inspect state and resume explicitly.",
                    "question": question,
                    "options": list(options or []),
                    "context": dict(context or {}),
                    "synthesis": dict(synthesis),
                }
            ),
        ],
    )


def _finish_terminal(
    agency: Path,
    instance: str,
    surface: str,
    run: Mapping[str, Any],
    synthesis: Mapping[str, Any],
    *,
    final_sender: Callable[[Path, str, Mapping[str, Any], Mapping[str, Any]], None],
    close_surface_fn: Callable[[str], Any],
) -> None:
    pipeline_id = run["pipelineId"]
    delivery = run["finalDelivery"]
    lock = pipeline_state.read_lock(agency)
    if delivery["publishedAt"] is None:
        if lock is None or lock.get("pipelineId") != pipeline_id or lock.get("ownerId") != instance:
            raise PipelineRuntimeError("terminal publication requires the runner-owned lock")
        final_sender(agency, instance, run, synthesis)
        pipeline_state.mark_final_published(
            agency, pipeline_id, lock_owner=instance
        )
        run = pipeline_state.get_run(agency, pipeline_id)
        delivery = run["finalDelivery"]

    lock = pipeline_state.read_lock(agency)
    if lock is not None:
        if lock.get("pipelineId") != pipeline_id or lock.get("ownerId") != instance:
            raise PipelineRuntimeError("terminal cleanup lock is owned by another runner")
        pipeline_state.mark_final_cleanup_started(
            agency, pipeline_id, lock_owner=instance
        )
        pipeline_state.release_lock(
            agency, owner_id=instance, pipeline_id=pipeline_id
        )
    elif delivery.get("cleanupStartedAt") is None:
        raise PipelineRuntimeError("terminal cleanup lost its lock before durable cleanup intent")

    sessions = ledger.load_sessions(agency)
    current = ledger.find_instance(sessions, instance)
    if current is not None:
        ledger.clear_instance(sessions, current)
        ledger.save_sessions(agency, sessions)
    # A process death after row removal can leave only an orphan shell surface;
    # durable ownership and roster resources are already released.
    close_surface_fn(surface)


def _serve_pipeline_runner_locked(
    instance: str,
    *,
    resume: bool = False,
    wait_timeout: float = 120.0,
    root: Path | None = None,
    project: Path | None = None,
    pid: int | None = None,
    control_plane_factory: Callable[[Path, Path], pipeline_runner.ControlPlane] = AgencyControlPlane,
    run_pipeline_fn: Callable[..., dict[str, Any]] | None = None,
    final_sender: Callable[[Path, str, Mapping[str, Any], Mapping[str, Any]], None] = _send_final,
    attention_notifier: Callable[
        [Path, str, Mapping[str, Any], Mapping[str, Any]], None
    ] = _notify_attention,
    close_surface_fn: Callable[[str], Any] = ctl.close_surface,
) -> dict[str, Any]:
    """Claim or explicitly resume one precreated run and own its full lifecycle."""
    if not isinstance(wait_timeout, (int, float)) or isinstance(wait_timeout, bool):
        raise PipelineRuntimeError("wait timeout must be a non-negative finite number")
    wait_timeout = float(wait_timeout)
    if not math.isfinite(wait_timeout) or wait_timeout < 0:
        raise PipelineRuntimeError("wait timeout must be a non-negative finite number")
    agency = Path(root) if root is not None else configured_agency_root()
    project_dir = Path(project) if project is not None else configured_project_root()
    sessions, row, surface = _runner_row(agency, instance)
    process_pid = os.getpid() if pid is None else pid

    existing_claim = row.get("pipelineClaim")
    if isinstance(existing_claim, Mapping):
        claimed_id = _safe_identifier(
            existing_claim.get("pipelineId"), "claimed pipelineId"
        )
        try:
            historical = pipeline_state.get_run(agency, claimed_id)
        except pipeline_state.UnknownPipelineError:
            historical = None
        if historical is not None and historical.get("status") in {"succeeded", "failed"}:
            if resume:
                raise PipelineRuntimeError("pipeline resume is valid only for needs_attention")
            if existing_claim.get("taskId") != historical.get("finalTaskId"):
                raise PipelineRuntimeError("terminal recovery claim task mismatch")
            if historical.get("runnerInstance") != instance:
                raise PipelineRuntimeError("terminal recovery runner binding mismatch")
            recovery_lock = pipeline_state.read_lock(agency)
            if recovery_lock is not None:
                if (
                    recovery_lock.get("pipelineId") != claimed_id
                    or recovery_lock.get("ownerId") != instance
                ):
                    raise PipelineRuntimeError("terminal recovery lock ownership mismatch")
                pipeline_state.bind_lock_runtime(
                    agency,
                    pipeline_id=claimed_id,
                    owner_id=instance,
                    owner_pid=process_pid,
                    owner_surface=surface,
                )
            synthesis = pipeline_runner.synthesize_run(historical)
            _finish_terminal(
                agency,
                instance,
                surface,
                historical,
                synthesis,
                final_sender=final_sender,
                close_surface_fn=close_surface_fn,
            )
            return synthesis

    if resume:
        claim = row.get("pipelineClaim")
        if not isinstance(claim, Mapping):
            raise PipelineRuntimeError("pipeline resume requires an existing durable claim")
        pipeline_id = _safe_identifier(claim.get("pipelineId"), "claimed pipelineId")
        run = pipeline_state.get_active_run(agency)
        if run is None or run.get("pipelineId") != pipeline_id or run.get("status") != "needs_attention":
            raise PipelineRuntimeError("pipeline resume requires the claimed active needs_attention run")
        if claim.get("taskId") != run.get("finalTaskId"):
            raise PipelineRuntimeError("pipeline resume claim task does not match the final task")
        receipt_name = claim.get("receiptName")
        if not isinstance(receipt_name, str) or Path(receipt_name).name != receipt_name:
            raise PipelineRuntimeError("pipeline resume claim has an invalid receipt")
        lock = pipeline_state.read_lock(agency)
        if lock is None or lock.get("pipelineId") != pipeline_id or lock.get("ownerId") != instance:
            raise PipelineRuntimeError("pipeline resume lock ownership mismatch")
        pipeline_state.bind_lock_runtime(
            agency,
            pipeline_id=pipeline_id,
            owner_id=instance,
            owner_pid=process_pid,
            owner_surface=surface,
        )
        pipeline_state.bind_runner(
            agency,
            pipeline_id,
            lock_owner=instance,
            runner_instance=instance,
            runner_surface=surface,
        )
        row["status"] = "working"
        row["activePipelineId"] = pipeline_id
        row["updatedAt"] = ctl.utc_now()
        ledger.save_sessions(agency, sessions)
    else:
        if isinstance(row.get("pipelineClaim"), Mapping):
            active = pipeline_state.get_active_run(agency)
            if active is not None and active.get("status") == "needs_attention":
                raise PipelineRuntimeError("pipeline needs attention; use explicit --resume")
        receipt, envelope = _claim_initial_delegate(agency, instance, row, wait_timeout)
        run, _payload = _validate_initial_delegate(agency, instance, envelope)
        pipeline_id = run["pipelineId"]
        pipeline_state.bind_lock_runtime(
            agency,
            pipeline_id=pipeline_id,
            owner_id=instance,
            owner_pid=process_pid,
            owner_surface=surface,
        )
        pipeline_state.bind_runner(
            agency,
            pipeline_id,
            lock_owner=instance,
            runner_instance=instance,
            runner_surface=surface,
        )
        _save_claim(agency, sessions, row, envelope, receipt, pipeline_id)
        _ack_initial(agency, instance, receipt)
        _mark_initial_acknowledged(agency, instance, receipt)

    control_plane = control_plane_factory(agency, project_dir)
    driver = (
        pipeline_runner._run_pipeline_locked
        if run_pipeline_fn is None or run_pipeline_fn is pipeline_runner.run_pipeline
        else run_pipeline_fn
    )
    synthesis = driver(
        agency,
        project_dir,
        pipeline_id,
        instance,
        control_plane,
        resume=resume,
        wait_timeout=wait_timeout,
    )
    durable_run = pipeline_state.get_run(agency, pipeline_id)
    status = synthesis.get("status")
    sessions = ledger.load_sessions(agency)
    current = ledger.find_instance(sessions, instance)
    if status in {"succeeded", "failed"}:
        _finish_terminal(
            agency,
            instance,
            surface,
            durable_run,
            synthesis,
            final_sender=final_sender,
            close_surface_fn=close_surface_fn,
        )
    elif status == "needs_attention":
        if current is None:
            raise PipelineRuntimeError("runner session disappeared while retaining attention state")
        current["status"] = "needs_attention"
        current["activePipelineId"] = pipeline_id
        current["updatedAt"] = ctl.utc_now()
        ledger.save_sessions(agency, sessions)
        asking = next(
            (stage for stage in durable_run["stages"] if stage["id"] == durable_run["currentStageId"]),
            None,
        )
        attention_notifier(
            agency,
            instance,
            durable_run,
            synthesis,
            # Use the human-facing `question` only. Per review finding #7, `error`
            # is a separate machine-facing reason and must NOT be used as a
            # question fallback, so a real error is never misrendered to the operator.
            question=(asking or {}).get("question") or "",
            options=list((asking or {}).get("options") or []),
            context={
                "stageId": (asking or {}).get("id"),
                "summary": (asking or {}).get("summary") or "",
                "artifacts": dict((asking or {}).get("artifacts") or {}),
            },
        )
    else:
        raise PipelineRuntimeError(f"pipeline driver returned unsupported status {status!r}")
    return synthesis


def serve_pipeline_runner(
    instance: str,
    *,
    resume: bool = False,
    wait_timeout: float = 120.0,
    root: Path | None = None,
    project: Path | None = None,
    pid: int | None = None,
    control_plane_factory: Callable[[Path, Path], pipeline_runner.ControlPlane] = AgencyControlPlane,
    run_pipeline_fn: Callable[..., dict[str, Any]] | None = None,
    final_sender: Callable[[Path, str, Mapping[str, Any], Mapping[str, Any]], None] = _send_final,
    attention_notifier: Callable[
        [Path, str, Mapping[str, Any], Mapping[str, Any]], None
    ] = _notify_attention,
    close_surface_fn: Callable[[str], Any] = ctl.close_surface,
) -> dict[str, Any]:
    """Hold one execution guard across claim, drive, delivery, and cleanup."""
    agency = Path(root) if root is not None else configured_agency_root()
    with pipeline_state.pipeline_execution_guard(agency):
        return _serve_pipeline_runner_locked(
            instance,
            resume=resume,
            wait_timeout=wait_timeout,
            root=agency,
            project=project,
            pid=pid,
            control_plane_factory=control_plane_factory,
            run_pipeline_fn=run_pipeline_fn,
            final_sender=final_sender,
            attention_notifier=attention_notifier,
            close_surface_fn=close_surface_fn,
        )
