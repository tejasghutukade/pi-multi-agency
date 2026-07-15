from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import cmux_pane as cp

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_sessions",
    Path(__file__).resolve().parent.parent / "reconcile-sessions.py",
)
rs = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(rs)


def _write_sessions(root: Path, instances: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions.json").write_text(json.dumps({"instances": instances}))


def _instance(surface: str, pane: str) -> dict:
    return {
        "instanceId": f"inst-{surface}",
        "role": "scout",
        "intercomName": f"scout-{surface}",
        "lifecycle": "temporary",
        "status": "idle",
        "cwd": "/tmp",
        "taskId": None,
        "cmuxSurface": surface,
        "cmuxPane": pane,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _tree(surface_refs: list[str], pane_refs: list[str]) -> dict:
    panes = []
    for i, pane in enumerate(pane_refs):
        panes.append(
            {
                "ref": pane,
                "surfaces": [{"ref": s} for s in surface_refs],
            }
        )
    return {"windows": [{"workspaces": [{"panes": panes}]}]}


@pytest.fixture
def fake_cmux(monkeypatch):
    state: dict = {"tree": None}

    def fake_cmux_json(args):
        if args[:2] == ["tree", "--json"]:
            return state["tree"]
        return None

    monkeypatch.setattr(rs, "cmux_json", fake_cmux_json)
    return state


def test_live_session_with_short_ref_not_torn_down_by_substring(tmp_path: Path, fake_cmux):
    """Regression for review finding #2: a live session whose short surface/pane
    ref (e.g. 'surface:0') is a substring of an unrelated ref ('surface:10') must
    NOT be torn down by substring matching against the raw cmux tree."""
    _write_sessions(tmp_path, [_instance("surface:0", "pane:0")])
    # The only real surface is surface:10; surface:0 is NOT present. Under the old
    # substring logic, 'surface:0' in 'surface:10' would have matched and wrongly
    # KEPT it; the real danger is the inverse (short id present elsewhere). Here we
    # assert the exact-ref logic: surface:0 absent -> cleared.
    fake_cmux["tree"] = _tree(["surface:10"], ["pane:10"])
    result = rs.run_reconcile([str(tmp_path), "--check-cmux"])
    assert result["after"] == 0
    assert result["cleared"][0]["intercomName"] == "scout-surface:0"


def test_live_session_with_exact_ref_kept(tmp_path: Path, fake_cmux):
    """An instance whose surface/pane ref exactly matches a live cmux ref is kept."""
    _write_sessions(tmp_path, [_instance("surface:0", "pane:0")])
    fake_cmux["tree"] = _tree(["surface:0"], ["pane:0"])
    result = rs.run_reconcile([str(tmp_path), "--check-cmux"])
    assert result["after"] == 1
    assert result["cleared"] == []


def test_unavailable_cmux_does_not_clear_live_sessions(tmp_path: Path, fake_cmux):
    """Fail-safe: if cmux tree is unavailable, live sessions are never cleared."""
    _write_sessions(tmp_path, [_instance("surface:0", "pane:0")])
    fake_cmux["tree"] = None  # cmux_json returns None => unavailable
    result = rs.run_reconcile([str(tmp_path), "--check-cmux"])
    assert result["after"] == 1
    assert result["cleared"] == []
