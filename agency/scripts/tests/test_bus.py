from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import bus
import pytest


@pytest.fixture(autouse=True)
def _reset_notify():
    yield
    bus.set_notify(None)


@pytest.fixture
def agency_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    (tmp_path / "agents.yaml").write_text(
        """agents:
  scout:
    peers: [orchestrator]
  orchestrator:
    peers: [scout]
"""
    )
    return tmp_path


def test_send_creates_envelope(agency_tmp: Path):
    notes: list[tuple[str, str]] = []
    bus.set_notify(lambda t, b: notes.append((t, b)) or True)

    rc = bus.cmd_send(
        Namespace(
            type="report",
            from_name="scout-t01",
            to="orchestrator",
            task_id="task-1",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json='{"ok": true}',
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=False,
            allow_peers=False,
        )
    )
    assert rc == 0
    pending = list((agency_tmp / "inbox" / "orchestrator" / "pending").glob("*-report.json"))
    assert len(pending) == 1
    env = json.loads(pending[0].read_text())
    assert env["from"] == "scout-t01"
    assert env["to"] == "orchestrator"
    assert env["type"] == "report"
    assert env["taskId"] == "task-1"
    assert "senderAuth" not in env
    assert notes and notes[0][0] == "orchestrator"


def test_acl_deny_non_hub_peer(agency_tmp: Path):
    rc = bus.cmd_send(
        Namespace(
            type="delegate",
            from_name="scout-t01",
            to="planner",
            task_id="t",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
        )
    )
    assert rc == 3


def test_recv_claim_done(agency_tmp: Path):
    bus.set_notify(lambda *_: True)
    bus.cmd_send(
        Namespace(
            type="report",
            from_name="scout-t01",
            to="orchestrator",
            task_id="task-2",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
        )
    )
    pending = bus.list_pending(agency_tmp, "orchestrator")
    assert len(pending) == 1
    processing, data = bus.claim_pending(agency_tmp, "orchestrator", pending[0])
    assert data["taskId"] == "task-2"
    assert processing.parent.name == "processing"
    done = bus.move_to_done(agency_tmp, "orchestrator", processing)
    assert done.parent.name == "done"


def _send_report(root: Path, sender: str, task_id: str) -> None:
    rc = bus.cmd_send(
        Namespace(
            type="report",
            from_name=sender,
            to="orchestrator",
            task_id=task_id,
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
        )
    )
    assert rc == 0


def test_wait_sender_filter_leaves_wrong_sender_pending(agency_tmp: Path, capsys):
    _send_report(agency_tmp, "scout-t02", "pipeline-task")
    _send_report(agency_tmp, "scout-t01", "pipeline-task")
    capsys.readouterr()

    assert bus.cmd_wait(
        Namespace(
            as_name="orchestrator",
            task_id="pipeline-task",
            from_name="scout-t01",
            timeout=0,
            interval=0,
            auto_done_progress=True,
        )
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["envelope"]["from"] == "scout-t01"
    pending = [json.loads(path.read_text()) for path in bus.list_pending(agency_tmp, "orchestrator")]
    assert [envelope["from"] for envelope in pending] == ["scout-t02"]


def test_wait_type_filter_leaves_same_task_ask_pending(agency_tmp: Path, capsys):
    def send(typ: str) -> None:
        args = Namespace(
            type=typ,
            from_name="scout-t01",
            to="orchestrator",
            task_id="same-task",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
        )
        assert bus.cmd_send(args) == 0

    send("ask")
    send("report")
    capsys.readouterr()
    assert bus.cmd_wait(
        Namespace(
            as_name="orchestrator",
            task_id="same-task",
            from_name="scout-t01",
            type="report",
            timeout=0,
            interval=0,
            auto_done_progress=True,
        )
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["envelope"]["type"] == "report"
    pending = [json.loads(path.read_text()) for path in bus.list_pending(agency_tmp, "orchestrator")]
    assert [envelope["type"] for envelope in pending] == ["ask"]


def test_wait_without_sender_filter_remains_backward_compatible(agency_tmp: Path, capsys):
    _send_report(agency_tmp, "scout-t02", "ordinary-task")
    capsys.readouterr()
    assert bus.cmd_wait(
        Namespace(
            as_name="orchestrator",
            task_id="ordinary-task",
            timeout=0,
            interval=0,
            auto_done_progress=True,
        )
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "message"
    assert output["envelope"]["from"] == "scout-t02"


def test_require_caller_authenticates_sender_and_rejects_forgery(
    agency_tmp: Path, monkeypatch
):
    (agency_tmp / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "instanceId": "orch-1",
                        "intercomName": "orchestrator",
                        "role": "orchestrator",
                        "cmuxSurface": "surface:orch",
                    },
                    {
                        "instanceId": "scout-1",
                        "intercomName": "scout-t01",
                        "role": "scout",
                        "cmuxSurface": "surface:scout",
                    },
                ],
            }
        )
        + "\n"
    )
    monkeypatch.setattr(bus, "surface_alive", lambda surface: True)

    def args(from_name: str):
        return Namespace(
            type="report",
            from_name=from_name,
            to="orchestrator",
            task_id="auth-task",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
            require_caller=True,
            message_id=None,
        )

    monkeypatch.setattr(bus, "caller_surface", lambda: ("surface:scout", "pane"))
    assert bus.cmd_send(args("orchestrator")) == 4
    assert bus.list_pending(agency_tmp, "orchestrator") == []

    assert bus.cmd_send(args("scout-t01")) == 0
    envelope = json.loads(bus.list_pending(agency_tmp, "orchestrator")[0].read_text())
    assert envelope["senderAuth"] == {
        "instanceId": "scout-1",
        "intercomName": "scout-t01",
        "surface": "surface:scout",
    }


def test_authenticated_pipeline_runner_may_send_stage_delegate(
    agency_tmp: Path, monkeypatch
):
    (agency_tmp / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "instanceId": "runner-1",
                        "intercomName": "pipeline-runner-t1",
                        "role": "pipeline-runner",
                        "cmuxSurface": "surface:runner",
                    }
                ],
            }
        )
        + "\n"
    )
    monkeypatch.setattr(bus, "caller_surface", lambda: ("surface:runner", "pane"))
    monkeypatch.setattr(bus, "surface_alive", lambda surface: True)
    args = Namespace(
        type="delegate",
        from_name="pipeline-runner-t1",
        to="scout-t1",
        task_id="pl-p-123-s1",
        workflow_id=None,
        correlation_id=None,
        reply_to=None,
        ttl=3600,
        priority="normal",
        payload_json="{}",
        payload_file=None,
        payload_path=None,
        notify_title=None,
        notify_body=None,
        no_notify=True,
        allow_peers=False,
        require_caller=True,
        message_id=None,
    )
    assert bus.cmd_send(args) == 0
    envelope = json.loads(bus.list_pending(agency_tmp, "scout-t1")[0].read_text())
    assert envelope["from"] == "pipeline-runner-t1"
    assert envelope["senderAuth"]["instanceId"] == "runner-1"


def test_stable_message_id_is_idempotent_and_conflicts_fail(
    agency_tmp: Path, monkeypatch, capsys
):
    (agency_tmp / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "instanceId": "scout-1",
                        "intercomName": "scout-t01",
                        "role": "scout",
                        "cmuxSurface": "surface:scout",
                    }
                ],
            }
        )
        + "\n"
    )
    monkeypatch.setattr(bus, "caller_surface", lambda: ("surface:scout", "pane"))
    monkeypatch.setattr(bus, "surface_alive", lambda surface: True)

    def args(payload: str):
        return Namespace(
            type="report",
            from_name="scout-t01",
            to="orchestrator",
            task_id="final-task",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json=payload,
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
            require_caller=True,
            message_id="pipe-final-p-123",
        )

    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    capsys.readouterr()
    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["replay"] is True
    assert len(bus.list_pending(agency_tmp, "orchestrator")) == 1

    outbox = agency_tmp / "outbox" / "pipe-final-p-123.json"
    outbox.unlink()
    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    capsys.readouterr()
    assert outbox.is_file()
    pending = bus.list_pending(agency_tmp, "orchestrator")
    assert len(pending) == 1
    pending[0].unlink()
    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    capsys.readouterr()
    assert len(bus.list_pending(agency_tmp, "orchestrator")) == 1
    processing, _ = bus.claim_pending(
        agency_tmp,
        "orchestrator",
        bus.list_pending(agency_tmp, "orchestrator")[0],
    )
    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    capsys.readouterr()
    assert processing.is_file()
    assert bus.list_pending(agency_tmp, "orchestrator") == []
    done = bus.move_to_done(agency_tmp, "orchestrator", processing)
    assert bus.cmd_send(args('{"status":"succeeded"}')) == 0
    capsys.readouterr()
    assert done.is_file()
    assert bus.list_pending(agency_tmp, "orchestrator") == []

    assert bus.cmd_send(args('{"status":"failed"}')) == 5
    assert done.is_file()
    assert bus.list_pending(agency_tmp, "orchestrator") == []


def test_notify_noop_when_disabled(agency_tmp: Path):
    notes: list[str] = []
    bus.set_notify(lambda t, b: notes.append(t) or True)
    bus.cmd_send(
        Namespace(
            type="ask",
            from_name="scout-t01",
            to="orchestrator",
            task_id="t3",
            workflow_id=None,
            correlation_id=None,
            reply_to=None,
            ttl=3600,
            priority="normal",
            payload_json="{}",
            payload_file=None,
            payload_path=None,
            notify_title=None,
            notify_body=None,
            no_notify=True,
            allow_peers=False,
        )
    )
    assert notes == []
