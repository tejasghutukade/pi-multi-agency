from __future__ import annotations

import json
import os
from pathlib import Path

import hub_delivery as hd
import pipeline_state


def _active_run(root: Path) -> None:
    pipeline_state.acquire_lock(
        root,
        pipeline_id="pipe-X",
        owner_id="runner",
        owner_pid=os.getpid(),
        owner_surface="surface:runner",
    )
    pipeline_state.create_run(
        root,
        pipeline_id="pipe-X",
        pipeline_name="flow",
        topic="t",
        definition={
            "description": "d",
            "onFailure": "stop",
            "stages": [
                {"id": "work", "role": "worker", "goal": "g", "outputs": ["primary"], "inputs": []}
            ],
        },
        lock_owner="runner",
        runner_instance="runner-t1",
        runner_surface="surface:runner",
    )
    pipeline_state.record_dispatched(
        root, "pipe-X", "work", lock_owner="runner", assigned_instance="worker-t1"
    )


def test_owned_stage_report_filtered_from_claim(tmp_path: Path):
    _active_run(tmp_path)
    pending = tmp_path / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True)
    (pending / "owned.json").write_text(
        json.dumps({"type": "report", "from": "worker-t1", "taskId": "pl-pipe-X-s1", "payload": {"status": "succeeded"}}) + "\n"
    )
    (pending / "normal.json").write_text(
        json.dumps({"type": "report", "from": "scout-t01", "taskId": "t2", "payload": {"summary": "x"}}) + "\n"
    )
    claimed = hd.claim_for_delivery(tmp_path)
    assert claimed["empty"] is False
    assert claimed["envelope"]["taskId"] == "t2"


def test_owned_stage_report_excluded_from_pending_hub(tmp_path: Path):
    import lifecycle_bridge as lb

    _active_run(tmp_path)
    pending = tmp_path / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True)
    (pending / "owned.json").write_text(
        json.dumps({"type": "report", "from": "worker-t1", "taskId": "pl-pipe-X-s1"}) + "\n"
    )
    (pending / "ask.json").write_text(
        json.dumps({"type": "ask", "from": "worker-t1", "taskId": "pl-pipe-X-s1", "payload": {"q": 1}}) + "\n"
    )
    (pending / "normal.json").write_text(
        json.dumps({"type": "report", "from": "scout-t01", "taskId": "t2"}) + "\n"
    )
    items = lb.hub_inbox_envelopes(tmp_path)
    tuples = {(i["envelope"]["type"], i["envelope"].get("taskId")) for i in items}
    assert ("report", "pl-pipe-X-s1") not in tuples
    assert ("ask", "pl-pipe-X-s1") in tuples
    assert ("report", "t2") in tuples


def test_unrelated_and_final_reports_remain_visible(tmp_path: Path):
    _active_run(tmp_path)
    pending = tmp_path / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True)
    (pending / "stray.json").write_text(
        json.dumps({"type": "report", "from": "x", "taskId": "pl-other-s1"}) + "\n"
    )
    (pending / "final.json").write_text(
        json.dumps({"type": "report", "from": "runner-t1", "taskId": "pipe-done-pipe-X"}) + "\n"
    )
    claimed = hd.claim_for_delivery(tmp_path)
    assert claimed["empty"] is False
    assert claimed["envelope"]["taskId"] in ("pl-other-s1", "pipe-done-pipe-X")


def test_format_delivery_text_json_payload():
    text = hd.format_delivery_text(
        {"type": "report", "from": "scout-t01", "taskId": "t1", "payload": {"ok": True}}
    )
    assert "[agency:report]" in text
    assert "scout-t01" in text
    assert '"ok": true' in text or '"ok": true' in text.replace(" ", "")


def test_claim_empty(tmp_path: Path):
    result = hd.claim_for_delivery(tmp_path)
    assert result["ok"] is True and result["empty"] is True


def test_claim_and_ack_report_clears_task(tmp_path: Path):
    pending = tmp_path / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True)
    env = {
        "type": "report",
        "from": "scout-t01",
        "taskId": "t1",
        "payload": {"summary": "done"},
    }
    (pending / "20260101T000000Z-abcd-report.json").write_text(json.dumps(env) + "\n")

    sessions = {
        "version": 1,
        "instances": [
            {
                "intercomName": "scout-t01",
                "instanceId": "a",
                "role": "scout",
                "taskId": "t1",
                "nudgeCount": 1,
                "lastDelegate": {"taskId": "t1"},
                "silentSettleAt": "x",
                "awaitingStartAfterNudge": True,
            }
        ],
    }
    (tmp_path / "sessions.json").write_text(json.dumps(sessions) + "\n")

    claimed = hd.claim_for_delivery(tmp_path)
    assert claimed["empty"] is False
    assert "text" in claimed
    path = Path(claimed["path"])
    assert path.parent.name == "processing"

    ack = hd.ack_delivery(tmp_path, path)
    assert ack["ok"] is True
    data = json.loads((tmp_path / "sessions.json").read_text())
    inst = data["instances"][0]
    assert inst["taskId"] is None
    assert inst["lastDelegate"] is None
    assert inst["nudgeCount"] == 0


def test_ack_ask_keeps_task_id(tmp_path: Path):
    pending = tmp_path / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True)
    env = {"type": "ask", "from": "scout-t01", "taskId": "t2", "payload": {"q": 1}}
    (pending / "ask.json").write_text(json.dumps(env) + "\n")
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {"intercomName": "scout-t01", "instanceId": "a", "role": "scout", "taskId": "t2"}
                ],
            }
        )
        + "\n"
    )
    claimed = hd.claim_for_delivery(tmp_path)
    hd.ack_delivery(tmp_path, Path(claimed["path"]))
    data = json.loads((tmp_path / "sessions.json").read_text())
    assert data["instances"][0]["taskId"] == "t2"


def test_hub_delivery_does_not_import_recovery():
    src = Path(hd.__file__).read_text()
    assert "recovery" not in src
    assert "import agency_ctl" not in src
