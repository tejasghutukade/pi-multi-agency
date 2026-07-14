from __future__ import annotations

import json
from pathlib import Path

import hub_delivery as hd


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
