#!/usr/bin/env python3
"""Crash-safe persistence and reconciliation primitives for declarative pipelines.

This module deliberately has no dependency on the pipeline runner or agency CLI.
It owns only durable state, the cross-process project lock, state transitions, and
queries that later integration units can consume.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

STATE_VERSION = 3
LOCK_VERSION = 1
STATE_FILE = "pipelines.json"
PREVIOUS_STATE_FILE = "pipelines.json.prev"
LOCK_FILE = "pipelines.lock"
EXECUTION_LOCK_FILE = "pipelines.execution.lock"

STAGE_STATUSES = frozenset(
    {"pending", "dispatched", "succeeded", "failed", "dependency_failed", "needs_attention"}
)
RUN_STATUSES = frozenset({"running", "succeeded", "failed", "needs_attention"})
ACTIVE_RUN_STATUSES = frozenset({"running", "needs_attention"})
TERMINAL_STAGE_STATUSES = frozenset({"succeeded", "failed", "dependency_failed", "needs_attention"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PipelineStateError(RuntimeError):
    """Base error for pipeline state operations."""


class PipelineStateValidationError(PipelineStateError, ValueError):
    """A caller attempted to persist invalid state or a malformed definition."""


class PipelineStateCorruption(PipelineStateError):
    """No valid on-disk state generation could be loaded."""


class PipelineLockError(PipelineStateError):
    """Base error for project pipeline lock operations."""


class PipelineLockCorruption(PipelineLockError):
    """The durable lock exists but is not a valid ownership record."""


class PipelineLockConflict(PipelineLockError):
    """Another durable pipeline owner already holds this project lock."""

    def __init__(self, ownership: dict[str, Any]):
        self.ownership = copy.deepcopy(ownership)
        super().__init__(
            "active pipeline lock is owned by "
            f"pipeline {ownership['pipelineId']!r}, owner {ownership['ownerId']!r}"
        )


class PipelineLockOwnershipError(PipelineLockError):
    """A mutation or release was attempted by someone other than the lock owner."""


class PipelineExecutionConflict(PipelineStateError):
    """Another process or thread is currently driving this project's pipeline."""


class ActivePipelineError(PipelineStateError):
    """The project already has an active pipeline."""


class UnknownPipelineError(PipelineStateError):
    """The requested pipeline run does not exist."""


class UnknownStageError(PipelineStateError):
    """The requested stage does not exist in the run."""


class IllegalStageTransition(PipelineStateError):
    """A requested stage transition would violate the state machine."""


class ResumeAction(str, Enum):
    """A classification only; notably there is no retry/redelegate action."""

    SKIP = "skip"
    RECONCILE = "reconcile"
    NEEDS_ATTENTION = "needs_attention"
    PENDING = "pending"
    TERMINAL = "terminal"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_path(root: Path) -> Path:
    return root / STATE_FILE


def _previous_path(root: Path) -> Path:
    return root / PREVIOUS_STATE_FILE


def _lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def _execution_lock_path(root: Path) -> Path:
    return root / EXECUTION_LOCK_FILE


@contextmanager
def pipeline_execution_guard(root: Path):
    """Hold a non-blocking kernel advisory lock for one complete driver execution.

    The lock file is not an ownership record and may persist. The kernel releases
    the advisory lock if the process crashes; no timestamp or stale heuristic is
    involved.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = _execution_lock_path(root)
    stream = path.open("a+b")
    locked = False
    try:
        if os.name == "posix":
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, ImportError) as exc:
                raise PipelineExecutionConflict(
                    f"pipeline execution guard is already held or unavailable: {path}"
                ) from exc
        elif os.name == "nt":
            try:
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except (OSError, ImportError) as exc:
                raise PipelineExecutionConflict(
                    f"pipeline execution guard is already held or unavailable: {path}"
                ) from exc
        else:
            raise PipelineExecutionConflict(
                f"pipeline execution guard is unsupported on platform {os.name!r}"
            )
        yield
    finally:
        if locked:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                stream.close()
        else:
            stream.close()


def _fsync_directory(root: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(root), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, payload: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _require_string(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise PipelineStateValidationError(f"{context} must be {qualifier}")
    return value


def _require_optional_string(value: Any, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise PipelineStateValidationError(f"{context} must be a string or null")


def _validate_lock_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineStateValidationError("lock record must be an object")
    required = {"version", "pipelineId", "ownerId", "ownerPid", "ownerSurface", "createdAt"}
    if set(value) != required:
        raise PipelineStateValidationError("lock record has missing or unsupported fields")
    if value.get("version") != LOCK_VERSION:
        raise PipelineStateValidationError(f"lock record has unsupported version {value.get('version')!r}")
    _require_string(value.get("pipelineId"), "lock pipelineId")
    _require_string(value.get("ownerId"), "lock ownerId")
    owner_pid = value.get("ownerPid")
    if owner_pid is not None and (not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0):
        raise PipelineStateValidationError("lock ownerPid must be a positive integer or null")
    _require_optional_string(value.get("ownerSurface"), "lock ownerSurface")
    _require_string(value.get("createdAt"), "lock createdAt")
    return copy.deepcopy(value)


def acquire_lock(
    root: Path,
    *,
    pipeline_id: str,
    owner_id: str,
    owner_pid: int | None = None,
    owner_surface: str | None = None,
) -> dict[str, Any]:
    """Durably acquire the per-project lock using atomic ``O_EXCL`` creation.

    The lock is a record rather than a process-held file descriptor, so ownership
    survives a handoff into a separately launched runner process. This function
    intentionally makes no stale-owner/liveness decision.
    """
    root.mkdir(parents=True, exist_ok=True)
    _validate_identifier(pipeline_id, "pipeline_id")
    _require_string(owner_id, "owner_id")
    record = _validate_lock_record(
        {
            "version": LOCK_VERSION,
            "pipelineId": pipeline_id,
            "ownerId": owner_id,
            "ownerPid": os.getpid() if owner_pid is None else owner_pid,
            "ownerSurface": owner_surface,
            "createdAt": _now(),
        }
    )
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    try:
        _write_exclusive(_lock_path(root), payload)
    except FileExistsError:
        existing = read_lock(root)
        if existing is None:  # The owner released between O_EXCL and our query.
            return acquire_lock(
                root,
                pipeline_id=pipeline_id,
                owner_id=owner_id,
                owner_pid=owner_pid,
                owner_surface=owner_surface,
            )
        raise PipelineLockConflict(existing)
    _fsync_directory(root)
    return copy.deepcopy(record)


def read_lock(root: Path, *, attempts: int = 5) -> dict[str, Any] | None:
    """Return current ownership, tolerating an O_EXCL creator's brief write window."""
    path = _lock_path(root)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PipelineLockCorruption(f"cannot read pipeline lock {path}: {exc}") from exc
        try:
            parsed = json.loads(text)
            return _validate_lock_record(parsed)
        except (json.JSONDecodeError, PipelineStateValidationError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.01 * (attempt + 1))
    raise PipelineLockCorruption(f"invalid pipeline lock {path}: {last_error}") from last_error


def bind_lock_runtime(
    root: Path,
    *,
    pipeline_id: str,
    owner_id: str,
    owner_pid: int,
    owner_surface: str,
) -> dict[str, Any]:
    """Durably bind an existing lock owner to its live runtime identity.

    Ownership and the original creation timestamp are immutable.  The updated
    record is fsynced to a same-directory temporary file and atomically replaces
    the prior generation, so readers see either complete record.
    """
    _validate_identifier(pipeline_id, "pipeline_id")
    _require_string(owner_id, "owner_id")
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0:
        raise PipelineStateValidationError("owner_pid must be a positive integer")
    _require_string(owner_surface, "owner_surface")
    current = _assert_lock(root, pipeline_id, owner_id)
    record = _validate_lock_record(
        {
            **current,
            "ownerPid": owner_pid,
            "ownerSurface": owner_surface,
        }
    )
    if record == current:
        return copy.deepcopy(record)

    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    tmp = root / f".{LOCK_FILE}.{secrets.token_hex(6)}.tmp"
    _write_exclusive(tmp, payload)
    try:
        # Re-check immediately before replacement.  This prevents a caller from
        # overwriting a different owner discovered while preparing the record.
        latest = _assert_lock(root, pipeline_id, owner_id)
        if latest.get("createdAt") != current.get("createdAt"):
            raise PipelineLockOwnershipError("pipeline lock changed before runtime binding")
        os.replace(tmp, _lock_path(root))
        _fsync_directory(root)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return copy.deepcopy(record)


def release_lock(root: Path, *, owner_id: str, pipeline_id: str | None = None) -> None:
    """Release only when the durable record still belongs to the caller."""
    current = read_lock(root)
    if current is None:
        raise PipelineLockOwnershipError("pipeline lock is not held")
    if current["ownerId"] != owner_id or (pipeline_id is not None and current["pipelineId"] != pipeline_id):
        raise PipelineLockOwnershipError(
            "pipeline lock ownership mismatch: "
            f"held by pipeline {current['pipelineId']!r}, owner {current['ownerId']!r}"
        )
    try:
        _lock_path(root).unlink()
    except FileNotFoundError as exc:
        raise PipelineLockOwnershipError("pipeline lock changed before release") from exc
    _fsync_directory(root)


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "activePipelineId": None, "runs": []}


def _validate_identifier(value: Any, context: str) -> str:
    text = _require_string(value, context)
    if not _IDENTIFIER.fullmatch(text):
        raise PipelineStateValidationError(f"{context} is not a valid identifier")
    return text


def _validate_stage(stage: Any, context: str) -> dict[str, Any]:
    if not isinstance(stage, dict):
        raise PipelineStateValidationError(f"{context} must be an object")
    required = {
        "id",
        "role",
        "taskId",
        "assignedInstance",
        "status",
        "summary",
        "artifacts",
        "error",
        "createdAt",
        "updatedAt",
        "dispatchedAt",
        "completedAt",
    }
    if set(stage) != required:
        raise PipelineStateValidationError(f"{context} has missing or unsupported fields")
    _validate_identifier(stage.get("id"), f"{context}.id")
    _validate_identifier(stage.get("role"), f"{context}.role")
    _require_string(stage.get("taskId"), f"{context}.taskId")
    _require_optional_string(stage.get("assignedInstance"), f"{context}.assignedInstance")
    status = stage.get("status")
    if status not in STAGE_STATUSES:
        raise PipelineStateValidationError(f"{context}.status is unknown: {status!r}")
    _require_string(stage.get("summary"), f"{context}.summary", allow_empty=True)
    artifacts = stage.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PipelineStateValidationError(f"{context}.artifacts must be an object")
    for name, path in artifacts.items():
        _validate_identifier(name, f"{context}.artifacts key")
        _require_string(path, f"{context}.artifacts[{name!r}]")
    _require_optional_string(stage.get("error"), f"{context}.error")
    if status in {"failed", "dependency_failed", "needs_attention"} and (
        not stage["error"] or not stage["error"].strip()
    ):
        raise PipelineStateValidationError(f"{context}: {status} stage requires non-blank error")
    if status in {"pending", "dispatched", "succeeded"} and stage["error"] is not None:
        raise PipelineStateValidationError(f"{context}: {status} stage cannot have error")
    for field in ("createdAt", "updatedAt"):
        _require_string(stage.get(field), f"{context}.{field}")
    for field in ("dispatchedAt", "completedAt"):
        _require_optional_string(stage.get(field), f"{context}.{field}")
    if status == "pending":
        if stage["assignedInstance"] is not None:
            raise PipelineStateValidationError(f"{context}: pending stage cannot have assignedInstance")
        if stage["dispatchedAt"] is not None or stage["completedAt"] is not None:
            raise PipelineStateValidationError(f"{context}: pending stage cannot have dispatch/completion timestamps")
    elif status == "dependency_failed":
        if stage["assignedInstance"] is not None or stage["dispatchedAt"] is not None:
            raise PipelineStateValidationError(f"{context}: dependency-failed stage was not dispatched")
    elif status == "needs_attention" and stage["dispatchedAt"] is None:
        if stage["assignedInstance"] is not None:
            raise PipelineStateValidationError(f"{context}: undispatched attention cannot have assignedInstance")
    else:
        _require_string(stage["assignedInstance"], f"{context}.assignedInstance")
        if stage["dispatchedAt"] is None:
            raise PipelineStateValidationError(f"{context}: dispatched stage requires dispatchedAt")
    if status == "dispatched" and stage["dispatchedAt"] is None:
        raise PipelineStateValidationError(f"{context}: dispatched stage requires dispatchedAt")
    if status in TERMINAL_STAGE_STATUSES and stage["completedAt"] is None:
        raise PipelineStateValidationError(f"{context}: terminal stage requires completedAt")
    return copy.deepcopy(stage)


def _validate_run_coherence(run: Mapping[str, Any], stages: list[dict[str, Any]], context: str) -> None:
    status = run["status"]
    current = run["currentStageId"]
    statuses = [stage["status"] for stage in stages]
    terminal_prior = {"succeeded", "failed", "dependency_failed"}

    if status in {"succeeded", "failed"}:
        if current is not None:
            raise PipelineStateValidationError(f"{context}: terminal run currentStageId must be null")
        if "dispatched" in statuses or "needs_attention" in statuses:
            raise PipelineStateValidationError(f"{context}: terminal run contains uncertain stage work")
        if status == "succeeded":
            if any(item != "succeeded" for item in statuses):
                raise PipelineStateValidationError(f"{context}: succeeded run requires every stage succeeded")
            return
        if run["onFailure"] == "stop":
            failed = [index for index, item in enumerate(statuses) if item == "failed"]
            if len(failed) != 1:
                raise PipelineStateValidationError(f"{context}: stopped failed run requires one failed stage")
            index = failed[0]
            if any(item != "succeeded" for item in statuses[:index]) or any(
                item != "pending" for item in statuses[index + 1 :]
            ):
                raise PipelineStateValidationError(
                    f"{context}: stopped failed run must preserve succeeded prefix and pending suffix"
                )
        else:
            if not any(item in {"failed", "dependency_failed"} for item in statuses) or any(
                item not in terminal_prior for item in statuses
            ):
                raise PipelineStateValidationError(
                    f"{context}: continued failed run requires fully classified terminal stages"
                )
        return

    if current is None:
        raise PipelineStateValidationError(f"{context}: active run requires actionable currentStageId")
    index = next((i for i, stage in enumerate(stages) if stage["id"] == current), -1)
    if index < 0:
        raise PipelineStateValidationError(f"{context}.currentStageId is unknown")
    before = statuses[:index]
    after = statuses[index + 1 :]
    if any(item not in terminal_prior for item in before) or any(item != "pending" for item in after):
        raise PipelineStateValidationError(
            f"{context}: active run requires terminal prefix and pending suffix"
        )
    if run["onFailure"] == "stop" and any(item != "succeeded" for item in before):
        raise PipelineStateValidationError(f"{context}: stopped policy cannot continue after failure")
    if status == "running":
        if statuses[index] not in {"pending", "dispatched"}:
            raise PipelineStateValidationError(f"{context}: running current stage must be pending or dispatched")
        if "needs_attention" in statuses:
            raise PipelineStateValidationError(f"{context}: running run cannot contain needs_attention")
        if statuses.count("dispatched") > 1 or (
            "dispatched" in statuses and statuses[index] != "dispatched"
        ):
            raise PipelineStateValidationError(f"{context}: only the running current stage may be dispatched")
    else:
        if statuses[index] != "needs_attention":
            raise PipelineStateValidationError(f"{context}: attention current stage must need attention")
        if statuses.count("needs_attention") != 1 or "dispatched" in statuses:
            raise PipelineStateValidationError(
                f"{context}: attention run must contain one attention stage and no dispatched stage"
            )


def _validate_run(run: Any, context: str) -> dict[str, Any]:
    if not isinstance(run, dict):
        raise PipelineStateValidationError(f"{context} must be an object")
    required = {
        "pipelineId",
        "pipelineName",
        "topic",
        "status",
        "onFailure",
        "definitionDigest",
        "currentStageId",
        "runnerInstance",
        "runnerSurface",
        "finalTaskId",
        "finalDelivery",
        "createdAt",
        "updatedAt",
        "completedAt",
        "stages",
    }
    if set(run) != required:
        raise PipelineStateValidationError(f"{context} has missing or unsupported fields")
    _validate_identifier(run.get("pipelineId"), f"{context}.pipelineId")
    _validate_identifier(run.get("pipelineName"), f"{context}.pipelineName")
    _require_string(run.get("topic"), f"{context}.topic")
    if run.get("status") not in RUN_STATUSES:
        raise PipelineStateValidationError(f"{context}.status is unknown: {run.get('status')!r}")
    if run.get("onFailure") not in {"stop", "continue"}:
        raise PipelineStateValidationError(f"{context}.onFailure must be stop or continue")
    digest = _require_string(run.get("definitionDigest"), f"{context}.definitionDigest")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PipelineStateValidationError(f"{context}.definitionDigest must be a canonical SHA-256")
    _require_optional_string(run.get("currentStageId"), f"{context}.currentStageId")
    _require_optional_string(run.get("runnerInstance"), f"{context}.runnerInstance")
    _require_optional_string(run.get("runnerSurface"), f"{context}.runnerSurface")
    final_task_id = _require_string(run.get("finalTaskId"), f"{context}.finalTaskId")
    expected_final_task_id = f"pipe-done-{run['pipelineId']}"
    if final_task_id != expected_final_task_id:
        raise PipelineStateValidationError(
            f"{context}.finalTaskId must be stable value {expected_final_task_id!r}"
        )
    final_delivery = run.get("finalDelivery")
    if not isinstance(final_delivery, dict) or set(final_delivery) != {
        "messageId",
        "publishedAt",
        "cleanupStartedAt",
    }:
        raise PipelineStateValidationError(
            f"{context}.finalDelivery has missing or unsupported fields"
        )
    expected_message_id = f"pipe-final-{run['pipelineId']}"
    if final_delivery.get("messageId") != expected_message_id:
        raise PipelineStateValidationError(
            f"{context}.finalDelivery.messageId must be {expected_message_id!r}"
        )
    _require_optional_string(final_delivery.get("publishedAt"), f"{context}.finalDelivery.publishedAt")
    _require_optional_string(
        final_delivery.get("cleanupStartedAt"),
        f"{context}.finalDelivery.cleanupStartedAt",
    )
    if final_delivery.get("cleanupStartedAt") is not None and final_delivery.get("publishedAt") is None:
        raise PipelineStateValidationError(
            f"{context}.finalDelivery cleanup cannot start before publication"
        )
    if run.get("status") in ACTIVE_RUN_STATUSES and any(
        final_delivery.get(field) is not None for field in ("publishedAt", "cleanupStartedAt")
    ):
        raise PipelineStateValidationError(
            f"{context}: active run cannot have final delivery progress"
        )
    _require_string(run.get("createdAt"), f"{context}.createdAt")
    _require_string(run.get("updatedAt"), f"{context}.updatedAt")
    _require_optional_string(run.get("completedAt"), f"{context}.completedAt")
    stages = run.get("stages")
    if not isinstance(stages, list) or not stages:
        raise PipelineStateValidationError(f"{context}.stages must be a non-empty list")
    normalized_stages = [_validate_stage(stage, f"{context}.stages[{index}]") for index, stage in enumerate(stages)]
    ids = [stage["id"] for stage in normalized_stages]
    if len(ids) != len(set(ids)):
        raise PipelineStateValidationError(f"{context} has duplicate stage ids")
    for index, stage in enumerate(normalized_stages, 1):
        expected_task = f"pl-{run['pipelineId']}-s{index}"
        if stage["taskId"] != expected_task:
            raise PipelineStateValidationError(
                f"{context}.stages[{index - 1}].taskId must be stable value {expected_task!r}"
            )
    if run["status"] in {"succeeded", "failed"} and run["completedAt"] is None:
        raise PipelineStateValidationError(f"{context}: terminal run requires completedAt")
    if run["status"] in ACTIVE_RUN_STATUSES and run["completedAt"] is not None:
        raise PipelineStateValidationError(f"{context}: active run cannot have completedAt")
    _validate_run_coherence(run, normalized_stages, context)
    normalized = copy.deepcopy(run)
    normalized["stages"] = normalized_stages
    return normalized


def validate_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PipelineStateValidationError("pipeline state root must be an object")
    if set(data) != {"version", "activePipelineId", "runs"}:
        raise PipelineStateValidationError("pipeline state has missing or unsupported root fields")
    if data.get("version") != STATE_VERSION:
        raise PipelineStateValidationError(f"unsupported version {data.get('version')!r}")
    active_id = data.get("activePipelineId")
    _require_optional_string(active_id, "activePipelineId")
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise PipelineStateValidationError("runs must be a list")
    normalized_runs = [_validate_run(run, f"runs[{index}]") for index, run in enumerate(runs)]
    ids = [run["pipelineId"] for run in normalized_runs]
    if len(ids) != len(set(ids)):
        raise PipelineStateValidationError("pipeline state has duplicate pipeline ids")
    active_runs = [run for run in normalized_runs if run["status"] in ACTIVE_RUN_STATUSES]
    if len(active_runs) > 1:
        raise PipelineStateValidationError("pipeline state contains more than one active run")
    if active_id is None and active_runs:
        raise PipelineStateValidationError("activePipelineId is missing for active run")
    if active_id is not None:
        if len(active_runs) != 1 or active_runs[0]["pipelineId"] != active_id:
            raise PipelineStateValidationError("activePipelineId does not identify the active run")
    return {"version": STATE_VERSION, "activePipelineId": active_id, "runs": normalized_runs}


def _read_generation(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
        parsed = json.loads(text)
        return validate_state(parsed)
    except (OSError, json.JSONDecodeError, PipelineStateValidationError) as exc:
        raise PipelineStateCorruption(f"{path}: {exc}") from exc


def load_state(root: Path) -> dict[str, Any]:
    """Load primary state, falling back to the last valid prior generation."""
    primary = _state_path(root)
    previous = _previous_path(root)
    primary_exists = primary.exists()
    previous_exists = previous.exists()
    errors: list[str] = []
    if primary_exists:
        try:
            return _read_generation(primary)
        except PipelineStateCorruption as exc:
            errors.append(f"primary generation invalid ({exc})")
    else:
        errors.append("primary generation missing")
    if previous_exists:
        try:
            prior = _read_generation(previous)
            if prior["activePipelineId"] is not None:
                raise PipelineStateCorruption(
                    "refusing active previous-generation fallback; operator reconciliation is required"
                )
            return prior
        except PipelineStateCorruption as exc:
            errors.append(f"previous generation invalid ({exc})")
    else:
        errors.append("previous generation missing")
    if not primary_exists and not previous_exists:
        return empty_state()
    raise PipelineStateCorruption("unrecoverable pipeline state: " + "; ".join(errors))


def save_state(root: Path, data: Mapping[str, Any]) -> None:
    """Validate and durably save state while retaining the prior valid generation."""
    root.mkdir(parents=True, exist_ok=True)
    validated = validate_state(dict(data))
    payload = (json.dumps(validated, indent=2, sort_keys=True) + "\n").encode()
    # Validate the exact serialized bytes before they can replace any generation.
    validate_state(json.loads(payload.decode()))
    primary = _state_path(root)
    previous = _previous_path(root)
    tmp = root / f".{STATE_FILE}.{secrets.token_hex(6)}.tmp"
    _write_exclusive(tmp, payload)
    try:
        primary_valid = False
        if primary.exists():
            try:
                _read_generation(primary)
                primary_valid = True
            except PipelineStateCorruption:
                primary_valid = False
        if primary_valid:
            os.replace(primary, previous)
            _fsync_directory(root)
        os.replace(tmp, primary)
        _fsync_directory(root)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _assert_lock(root: Path, pipeline_id: str, owner_id: str) -> dict[str, Any]:
    lock = read_lock(root)
    if lock is None:
        raise PipelineLockOwnershipError("pipeline lock is not held")
    if lock["pipelineId"] != pipeline_id or lock["ownerId"] != owner_id:
        raise PipelineLockOwnershipError(
            "pipeline lock ownership mismatch: "
            f"held by pipeline {lock['pipelineId']!r}, owner {lock['ownerId']!r}"
        )
    return lock


def _canonical_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Return the operational definition in its canonical, digestible shape."""
    if not isinstance(definition, Mapping):
        raise PipelineStateValidationError("pipeline definition must be a mapping")
    on_failure = definition.get("onFailure", "stop")
    if on_failure not in {"stop", "continue"}:
        raise PipelineStateValidationError("pipeline definition has invalid onFailure")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages:
        raise PipelineStateValidationError("pipeline definition requires non-empty stages")
    normalized_stages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            raise PipelineStateValidationError(f"pipeline definition stage {index + 1} must be a mapping")
        stage_id = _validate_identifier(stage.get("id"), f"pipeline definition stage {index + 1} id")
        role = _validate_identifier(stage.get("role"), f"pipeline definition stage {stage_id!r} role")
        if stage_id in seen:
            raise PipelineStateValidationError(f"pipeline definition has duplicate stage {stage_id!r}")
        seen.add(stage_id)
        goal = _require_string(stage.get("goal"), f"pipeline definition stage {stage_id!r} goal")
        outputs = stage.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise PipelineStateValidationError(f"pipeline definition stage {stage_id!r} outputs must be non-empty")
        normalized_outputs = [
            _validate_identifier(output, f"pipeline definition stage {stage_id!r} output")
            for output in outputs
        ]
        if len(normalized_outputs) != len(set(normalized_outputs)):
            raise PipelineStateValidationError(f"pipeline definition stage {stage_id!r} has duplicate outputs")
        inputs = stage.get("inputs", [])
        if not isinstance(inputs, list):
            raise PipelineStateValidationError(f"pipeline definition stage {stage_id!r} inputs must be a list")
        normalized_inputs: list[dict[str, Any]] = []
        for input_index, selector in enumerate(inputs):
            if not isinstance(selector, Mapping):
                raise PipelineStateValidationError(
                    f"pipeline definition stage {stage_id!r} input {input_index + 1} must be a mapping"
                )
            source = _validate_identifier(
                selector.get("stage"), f"pipeline definition stage {stage_id!r} input source"
            )
            artifacts = selector.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                raise PipelineStateValidationError(
                    f"pipeline definition stage {stage_id!r} input artifacts must be non-empty"
                )
            normalized_inputs.append(
                {
                    "stage": source,
                    "artifacts": [
                        _validate_identifier(
                            artifact, f"pipeline definition stage {stage_id!r} input artifact"
                        )
                        for artifact in artifacts
                    ],
                }
            )
        normalized_stages.append(
            {
                "id": stage_id,
                "role": role,
                "goal": goal,
                "outputs": normalized_outputs,
                "inputs": normalized_inputs,
            }
        )
    return {"onFailure": on_failure, "stages": normalized_stages}


def pipeline_definition_digest(definition: Mapping[str, Any]) -> str:
    """Return a canonical SHA-256 over execution-affecting pipeline fields."""
    canonical = _canonical_definition(definition)
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_run(
    root: Path,
    *,
    pipeline_id: str,
    pipeline_name: str,
    topic: str,
    definition: Mapping[str, Any],
    lock_owner: str,
    runner_instance: str | None = None,
    runner_surface: str | None = None,
) -> dict[str, Any]:
    """Create one active run from a U1-validated pipeline definition."""
    _validate_identifier(pipeline_id, "pipeline_id")
    _validate_identifier(pipeline_name, "pipeline_name")
    _require_string(topic, "topic")
    _assert_lock(root, pipeline_id, lock_owner)
    canonical_definition = _canonical_definition(definition)
    definition_stages = canonical_definition["stages"]
    data = load_state(root)
    if data["activePipelineId"] is not None:
        raise ActivePipelineError(f"active pipeline {data['activePipelineId']!r} already exists")
    if any(run["pipelineId"] == pipeline_id for run in data["runs"]):
        raise ActivePipelineError(f"pipeline id {pipeline_id!r} already exists")
    timestamp = _now()
    stages = []
    for index, stage in enumerate(definition_stages, 1):
        stages.append(
            {
                "id": stage["id"],
                "role": stage["role"],
                "taskId": f"pl-{pipeline_id}-s{index}",
                "assignedInstance": None,
                "status": "pending",
                "summary": "",
                "artifacts": {},
                "error": None,
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "dispatchedAt": None,
                "completedAt": None,
            }
        )
    run = {
        "pipelineId": pipeline_id,
        "pipelineName": pipeline_name,
        "topic": topic,
        "status": "running",
        "onFailure": canonical_definition["onFailure"],
        "definitionDigest": pipeline_definition_digest(canonical_definition),
        "currentStageId": stages[0]["id"],
        "runnerInstance": runner_instance,
        "runnerSurface": runner_surface,
        "finalTaskId": f"pipe-done-{pipeline_id}",
        "finalDelivery": {
            "messageId": f"pipe-final-{pipeline_id}",
            "publishedAt": None,
            "cleanupStartedAt": None,
        },
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "completedAt": None,
        "stages": stages,
    }
    data["runs"].append(run)
    data["activePipelineId"] = pipeline_id
    save_state(root, data)
    return copy.deepcopy(run)


def _run_from_data(data: Mapping[str, Any], pipeline_id: str) -> dict[str, Any]:
    for run in data.get("runs", []):
        if run["pipelineId"] == pipeline_id:
            return run
    raise UnknownPipelineError(f"unknown pipeline {pipeline_id!r}")


def _stage_from_run(run: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in run.get("stages", []):
        if stage["id"] == stage_id:
            return stage
    raise UnknownStageError(f"unknown stage {stage_id!r} in pipeline {run['pipelineId']!r}")


def get_run(root: Path, pipeline_id: str) -> dict[str, Any]:
    return copy.deepcopy(_run_from_data(load_state(root), pipeline_id))


def get_active_run(root: Path) -> dict[str, Any] | None:
    data = load_state(root)
    pipeline_id = data["activePipelineId"]
    return None if pipeline_id is None else copy.deepcopy(_run_from_data(data, pipeline_id))


def mark_final_published(
    root: Path,
    pipeline_id: str,
    *,
    lock_owner: str,
) -> dict[str, Any]:
    """Durably record publication of the stable terminal final envelope."""
    _assert_lock(root, pipeline_id, lock_owner)
    data = load_state(root)
    run = _run_from_data(data, pipeline_id)
    if run.get("status") not in {"succeeded", "failed"}:
        raise PipelineStateValidationError("final publication requires a terminal run")
    delivery = run["finalDelivery"]
    if delivery["publishedAt"] is None:
        delivery["publishedAt"] = _now()
        run["updatedAt"] = delivery["publishedAt"]
        save_state(root, data)
    return copy.deepcopy(delivery)


def mark_final_cleanup_started(
    root: Path,
    pipeline_id: str,
    *,
    lock_owner: str,
) -> dict[str, Any]:
    """Durably record owner-authorized terminal cleanup intent."""
    _assert_lock(root, pipeline_id, lock_owner)
    data = load_state(root)
    run = _run_from_data(data, pipeline_id)
    if run.get("status") not in {"succeeded", "failed"}:
        raise PipelineStateValidationError("final cleanup requires a terminal run")
    delivery = run["finalDelivery"]
    if delivery["publishedAt"] is None:
        raise PipelineStateValidationError("final cleanup requires published delivery")
    if delivery["cleanupStartedAt"] is None:
        delivery["cleanupStartedAt"] = _now()
        run["updatedAt"] = delivery["cleanupStartedAt"]
        save_state(root, data)
    return copy.deepcopy(delivery)


def bind_runner(
    root: Path,
    pipeline_id: str,
    *,
    lock_owner: str,
    runner_instance: str,
    runner_surface: str,
) -> dict[str, Any]:
    _assert_lock(root, pipeline_id, lock_owner)
    _require_string(runner_instance, "runner_instance")
    _require_string(runner_surface, "runner_surface")
    data = load_state(root)
    run = _run_from_data(data, pipeline_id)
    if data["activePipelineId"] != pipeline_id:
        raise ActivePipelineError(f"pipeline {pipeline_id!r} is not active")
    run["runnerInstance"] = runner_instance
    run["runnerSurface"] = runner_surface
    run["updatedAt"] = _now()
    save_state(root, data)
    return copy.deepcopy(run)


def get_active_runner_binding(root: Path) -> dict[str, Any] | None:
    run = get_active_run(root)
    if run is None or run["runnerInstance"] is None or run["runnerSurface"] is None:
        return None
    return {
        "pipelineId": run["pipelineId"],
        "finalTaskId": run["finalTaskId"],
        "runnerInstance": run["runnerInstance"],
        "runnerSurface": run["runnerSurface"],
    }


def find_task_ownership(root: Path, task_id: str, *, active_only: bool = True) -> dict[str, Any] | None:
    """Query exact task identity; prefixes and partial matches have no authority."""
    data = load_state(root)
    runs = data["runs"]
    if active_only:
        active_id = data["activePipelineId"]
        runs = [] if active_id is None else [_run_from_data(data, active_id)]
    for run in runs:
        if run["finalTaskId"] == task_id:
            return {
                "pipelineId": run["pipelineId"],
                "pipelineName": run["pipelineName"],
                "stageId": None,
                "role": "pipeline-runner",
                "taskKind": "final",
                "taskId": run["finalTaskId"],
                "runStatus": run["status"],
                "stageStatus": None,
                "expectedSender": run["runnerInstance"],
                "runnerInstance": run["runnerInstance"],
                "runnerSurface": run["runnerSurface"],
            }
        for stage in run["stages"]:
            if stage["taskId"] == task_id:
                return {
                    "pipelineId": run["pipelineId"],
                    "pipelineName": run["pipelineName"],
                    "stageId": stage["id"],
                    "role": stage["role"],
                    "taskKind": "stage",
                    "taskId": stage["taskId"],
                    "runStatus": run["status"],
                    "stageStatus": stage["status"],
                    "expectedSender": stage["assignedInstance"],
                    "runnerInstance": run["runnerInstance"],
                    "runnerSurface": run["runnerSurface"],
                }
    return None


def record_dispatched(
    root: Path,
    pipeline_id: str,
    stage_id: str,
    *,
    lock_owner: str,
    assigned_instance: str,
) -> dict[str, Any]:
    """Atomically persist dispatch intent, task id, and exact delegate instance."""
    return transition_stage(
        root,
        pipeline_id,
        stage_id,
        "dispatched",
        lock_owner=lock_owner,
        assigned_instance=assigned_instance,
    )


def _update_run_after_transition(data: dict[str, Any], run: dict[str, Any], stage: dict[str, Any], now: str) -> None:
    if stage["status"] == "failed" and run["onFailure"] == "stop":
        run["status"] = "failed"
        run["currentStageId"] = None
        run["completedAt"] = now
        data["activePipelineId"] = None
        return
    attention = [item for item in run["stages"] if item["status"] == "needs_attention"]
    if attention:
        run["status"] = "needs_attention"
        run["currentStageId"] = attention[0]["id"]
        return
    dispatched = [item for item in run["stages"] if item["status"] == "dispatched"]
    if dispatched:
        run["status"] = "running"
        run["currentStageId"] = dispatched[0]["id"]
        return
    pending = [item for item in run["stages"] if item["status"] == "pending"]
    if pending:
        run["status"] = "running"
        run["currentStageId"] = pending[0]["id"]
        return
    run["status"] = "failed" if any(
        item["status"] in {"failed", "dependency_failed"} for item in run["stages"]
    ) else "succeeded"
    run["currentStageId"] = None
    run["completedAt"] = now
    data["activePipelineId"] = None


def transition_stage(
    root: Path,
    pipeline_id: str,
    stage_id: str,
    new_status: str,
    *,
    lock_owner: str,
    summary: str | None = None,
    artifacts: Mapping[str, str] | None = None,
    error: str | None = None,
    assigned_instance: str | None = None,
) -> dict[str, Any]:
    _assert_lock(root, pipeline_id, lock_owner)
    if new_status not in STAGE_STATUSES:
        raise IllegalStageTransition(f"unknown stage status {new_status!r}")
    data = load_state(root)
    run = _run_from_data(data, pipeline_id)
    if data["activePipelineId"] != pipeline_id:
        raise ActivePipelineError(f"pipeline {pipeline_id!r} is not active")
    stage = _stage_from_run(run, stage_id)
    if run["currentStageId"] != stage_id:
        raise IllegalStageTransition(
            f"only current stage {run['currentStageId']!r} may transition, got {stage_id!r}"
        )
    allowed = {
        "pending": {"dispatched", "dependency_failed", "needs_attention"},
        "dispatched": {"succeeded", "failed", "needs_attention"},
        "succeeded": set(),
        "failed": set(),
        "dependency_failed": set(),
        "needs_attention": set(),
    }
    if new_status not in allowed[stage["status"]]:
        raise IllegalStageTransition(f"illegal stage transition {stage['status']!r} -> {new_status!r}")
    if stage["status"] == "pending" and new_status == "dispatched":
        assigned_instance = _require_string(assigned_instance, "assigned_instance")
    elif assigned_instance is not None:
        raise PipelineStateValidationError("assigned_instance is valid only when dispatching a pending stage")
    if summary is not None:
        _require_string(summary, "summary", allow_empty=True)
    if error is not None:
        _require_string(error, "error")
    if new_status in {"failed", "dependency_failed", "needs_attention"} and (
        not error or not error.strip()
    ):
        raise PipelineStateValidationError(f"{new_status} transition requires error")
    normalized_artifacts: dict[str, str] | None = None
    if artifacts is not None:
        if not isinstance(artifacts, Mapping):
            raise PipelineStateValidationError("artifacts must be a mapping")
        normalized_artifacts = {}
        for name, path in artifacts.items():
            key = _validate_identifier(name, "artifact name")
            normalized_artifacts[key] = _require_string(path, f"artifact {key!r} path")
    now = _now()
    if assigned_instance is not None:
        stage["assignedInstance"] = assigned_instance
    stage["status"] = new_status
    stage["updatedAt"] = now
    if summary is not None:
        stage["summary"] = summary
    if normalized_artifacts is not None:
        stage["artifacts"] = normalized_artifacts
    if error is not None:
        stage["error"] = error
    if new_status == "dispatched":
        stage["dispatchedAt"] = now
    else:
        stage["completedAt"] = now
    run["updatedAt"] = now
    _update_run_after_transition(data, run, stage, now)
    save_state(root, data)
    return copy.deepcopy(stage)


def record_reconciled_result(
    root: Path,
    pipeline_id: str,
    stage_id: str,
    *,
    lock_owner: str,
    status: str,
    summary: str,
    artifacts: Mapping[str, str],
    error: str | None,
) -> dict[str, Any]:
    """Record an exact late result only for work known to have been dispatched."""
    if status not in {"succeeded", "failed"}:
        raise IllegalStageTransition("reconciled status must be succeeded or failed")
    _assert_lock(root, pipeline_id, lock_owner)
    data = load_state(root)
    run = _run_from_data(data, pipeline_id)
    if data["activePipelineId"] != pipeline_id:
        raise ActivePipelineError(f"pipeline {pipeline_id!r} is not active")
    stage = _stage_from_run(run, stage_id)
    if run["currentStageId"] != stage_id:
        raise IllegalStageTransition(
            f"only current stage {run['currentStageId']!r} may reconcile, got {stage_id!r}"
        )
    if stage["assignedInstance"] is None or stage["dispatchedAt"] is None:
        raise IllegalStageTransition("cannot reconcile a stage that was never dispatched")
    if stage["status"] not in {"dispatched", "needs_attention"}:
        raise IllegalStageTransition(
            f"cannot reconcile stage in status {stage['status']!r}"
        )
    if not isinstance(summary, str) or not summary.strip():
        raise PipelineStateValidationError("reconciled summary must be a non-blank string")
    if status == "failed" and (not error or not error.strip()):
        raise PipelineStateValidationError("failed reconciliation requires non-blank error")
    if status == "succeeded" and error is not None:
        raise PipelineStateValidationError("succeeded reconciliation cannot have error")
    normalized_artifacts: dict[str, str] = {}
    if not isinstance(artifacts, Mapping):
        raise PipelineStateValidationError("artifacts must be a mapping")
    for name, path in artifacts.items():
        key = _validate_identifier(name, "artifact name")
        normalized_artifacts[key] = _require_string(path, f"artifact {key!r} path")
    now = _now()
    stage["status"] = status
    stage["summary"] = summary
    stage["artifacts"] = normalized_artifacts
    stage["error"] = error
    stage["updatedAt"] = now
    stage["completedAt"] = now
    run["updatedAt"] = now
    _update_run_after_transition(data, run, stage, now)
    save_state(root, data)
    return copy.deepcopy(stage)


def classify_resume(stage: Mapping[str, Any], report_exists: Callable[[str], bool]) -> ResumeAction:
    """Classify a stage for resume without performing execution or retry."""
    status = stage.get("status")
    if status not in STAGE_STATUSES:
        raise PipelineStateValidationError(f"cannot resume stage with unknown status {status!r}")
    if status == "succeeded":
        return ResumeAction.SKIP
    dispatched = stage.get("assignedInstance") is not None and stage.get("dispatchedAt") is not None
    if status == "dispatched" or (status == "needs_attention" and dispatched):
        task_id = _require_string(stage.get("taskId"), "dispatched stage taskId")
        return ResumeAction.RECONCILE if report_exists(task_id) else ResumeAction.NEEDS_ATTENTION
    if status == "pending":
        return ResumeAction.PENDING
    return ResumeAction.TERMINAL


def reconcile_resume(
    root: Path,
    pipeline_id: str,
    stage_id: str,
    report_exists: Callable[[str], bool],
    *,
    lock_owner: str,
) -> ResumeAction:
    """Persist uncertainty as needs_attention; never turn it into a dispatch."""
    _assert_lock(root, pipeline_id, lock_owner)
    stage = get_run(root, pipeline_id)
    stage_record = _stage_from_run(stage, stage_id)
    action = classify_resume(stage_record, report_exists)
    if action is ResumeAction.NEEDS_ATTENTION and stage_record["status"] == "dispatched":
        transition_stage(
            root,
            pipeline_id,
            stage_id,
            "needs_attention",
            lock_owner=lock_owner,
            error=f"No report exists for dispatched task {stage_record['taskId']}; automatic retry is prohibited",
        )
    return action
