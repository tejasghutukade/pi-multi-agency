from __future__ import annotations

import json
from pathlib import Path

import agency_events as ae
import ledger
import pytest


@pytest.fixture(autouse=True)
def _reset_emit():
    ae.set_emit(None)
    yield
    ae.set_emit(None)


def test_emit_noop_when_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENCY_EVENTS", raising=False)
    ae.emit("bus.sent", root=tmp_path, taskId="t1")
    assert not (tmp_path / "events.jsonl").exists()


def test_emit_appends_jsonl(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_EVENTS", "1")
    ae.emit("bus.sent", root=tmp_path, taskId="t1", instance="scout-t01")
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["type"] == "bus.sent"
    assert row["taskId"] == "t1"


def test_set_emit_none_restores_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_EVENTS", "1")
    captured: list[tuple] = []

    def capture(event_type, *, root=None, **fields):
        captured.append((event_type, fields))

    ae.set_emit(capture)
    ae.emit("x", root=tmp_path)
    assert captured
    ae.set_emit(None)
    ae.emit("bus.sent", root=tmp_path, taskId="t")
    assert (tmp_path / "events.jsonl").exists()


def test_emit_failure_swallowed(tmp_path: Path):
    def boom(*a, **k):
        raise RuntimeError("nope")

    ae.set_emit(boom)
    ae.emit("x", root=tmp_path)


def test_save_sessions_emits_delta(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_EVENTS", "1")
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-t01", "role": "scout", "status": "idle", "taskId": None}
        ],
    }
    ledger.save_sessions(tmp_path, data)
    data2 = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-t01", "role": "scout", "status": "working", "taskId": "t1"}
        ],
    }
    ledger.save_sessions(tmp_path, data2)
    rows = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().strip().splitlines()]
    assert any(r["type"] == "sessions.saved" for r in rows)
    last = rows[-1]
    assert last["changes"][0]["after"]["status"] == "working"


def test_save_sessions_skips_identical(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_EVENTS", "1")
    data = {
        "version": 1,
        "instances": [{"intercomName": "scout-t01", "role": "scout", "status": "idle"}],
    }
    ledger.save_sessions(tmp_path, data)
    n1 = len((tmp_path / "events.jsonl").read_text().strip().splitlines())
    ledger.save_sessions(tmp_path, data)
    n2 = len((tmp_path / "events.jsonl").read_text().strip().splitlines())
    assert n2 == n1
