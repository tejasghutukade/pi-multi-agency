from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from fixtures.observe_golden_root import build_observe_golden_root
from observe_state import snapshot


def test_ae1_snapshot(tmp_path: Path):
    root = build_observe_golden_root(tmp_path / "agency")
    snap = snapshot(root)
    assert snap["ok"]
    names = {i["intercomName"] for i in snap["instances"]}
    assert "scout-t01" in names
    assert snap["claim"]["bound"] is False
    hub = snap["inbox"]["orchestrator"]["pending"]
    assert hub["count"] == 1
    assert hub["messages"][0]["type"] == "report"
    assert snap["timeline"]["enabledFile"] is False
    assert "AGENCY_EVENTS" in (snap["timeline"]["emptyCopy"] or "")


def test_missing_root_dirs(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "sessions.json").write_text('{"version":1,"instances":[]}\n')
    snap = snapshot(root)
    assert snap["instances"] == []
    assert snap["inbox"] == {}
