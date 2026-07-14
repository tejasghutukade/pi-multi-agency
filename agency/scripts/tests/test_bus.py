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
    assert notes and notes[0][0] == "orchestrator"


def test_acl_deny_non_hub_peer(agency_tmp: Path):
    rc = bus.cmd_send(
        Namespace(
            type="delegate",
            from_name="scout-t01",
            to="plan",
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
