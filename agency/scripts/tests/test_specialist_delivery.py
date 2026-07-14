from __future__ import annotations

import json
from pathlib import Path

import specialist_delivery as sd


def test_claim_empty(tmp_path: Path):
    out = sd.claim_for_specialist_delivery(tmp_path, name="worker")
    assert out["ok"] is True
    assert out["empty"] is True


def test_claim_pending_delegate(tmp_path: Path):
    pending = tmp_path / "inbox" / "worker" / "pending"
    pending.mkdir(parents=True)
    env = {
        "type": "delegate",
        "from": "orchestrator",
        "taskId": "t1",
        "payload": {"goal": "ship it"},
    }
    (pending / "20260101T000000Z-a.json").write_text(json.dumps(env) + "\n")

    out = sd.claim_for_specialist_delivery(tmp_path, name="worker")
    assert out["ok"] is True
    assert out["empty"] is False
    assert out["path"].endswith("/inbox/worker/processing/20260101T000000Z-a.json")
    assert "[agency:delegate]" in out["text"]
    assert "agency_report / agency_ask" in out["text"]
    assert "$BUS" not in out["text"]


def test_claim_replays_existing_processing(tmp_path: Path):
    processing = tmp_path / "inbox" / "worker" / "processing"
    processing.mkdir(parents=True)
    env = {
        "type": "delegate",
        "from": "orchestrator",
        "taskId": "t2",
        "payload": {"goal": "resume"},
    }
    p = processing / "20260101T000001Z-b.json"
    p.write_text(json.dumps(env) + "\n")

    out = sd.claim_for_specialist_delivery(tmp_path, name="worker")
    assert out["ok"] is True
    assert out["empty"] is False
    assert out.get("replay") is True
    assert out["path"] == str(p)
    assert "resume" in out["text"]
