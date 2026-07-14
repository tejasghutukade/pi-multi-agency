from __future__ import annotations

import json
from subprocess import CompletedProcess
from typing import Any

import pytest

import cmux_pane as cp


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.responses: list[CompletedProcess[str]] = []

    def queue(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.responses.append(
            CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
        )

    def __call__(self, args: list[str], timeout: float = 15) -> CompletedProcess[str]:
        self.calls.append((list(args), timeout))
        if not self.responses:
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return self.responses.pop(0)


@pytest.fixture
def fake_runner():
    runner = FakeRunner()
    cp.set_runner(runner)
    yield runner
    cp.set_runner(None)


def test_parse_new_split_surface():
    assert cp.parse_new_split_surface("created surface:42 pane:7") == "surface:42"
    with pytest.raises(RuntimeError, match="could not parse surface"):
        cp.parse_new_split_surface("no surface here")


def test_find_cmux_missing(monkeypatch):
    monkeypatch.setattr(cp.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cp.Path, "exists", lambda self: False)
    with pytest.raises(RuntimeError, match="cmux not found"):
        cp.find_cmux()


def test_open_pane_records_argv_and_returns_surface(fake_runner: FakeRunner):
    tree = {
        "windows": [
            {
                "workspaces": [
                    {
                        "panes": [
                            {
                                "ref": "pane:9",
                                "surfaces": [{"ref": "surface:3"}],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    fake_runner.queue(stdout="ok surface:3\n")
    fake_runner.queue(stdout=json.dumps(tree))
    fake_runner.queue(returncode=0)

    result = cp.open_pane("right", command='echo hi', focus=True)

    assert result["surface"] == "surface:3"
    assert result["pane"] == "pane:9"
    assert result["sent"] is True
    assert fake_runner.calls[0][0] == ["new-split", "right", "--focus", "true"]
    assert fake_runner.calls[1][0] == ["tree", "--json"]
    assert fake_runner.calls[2][0][:3] == ["send", "--surface", "surface:3"]


def test_send_to_surface_enter_variants(fake_runner: FakeRunner):
    fake_runner.queue()
    cp.send_to_surface("surface:1", "hello", enter=True)
    assert fake_runner.calls[-1][0] == ["send", "--surface", "surface:1", "hello\\n"]

    fake_runner.queue()
    cp.send_to_surface("surface:1", "hello", enter=False)
    assert fake_runner.calls[-1][0] == ["send", "--surface", "surface:1", "hello"]


def test_surface_alive_true_false_unknown(fake_runner: FakeRunner):
    fake_runner.queue(stdout="pane surface:5 alive")
    assert cp.surface_alive("surface:5") is True

    fake_runner.queue(stdout="pane surface:1")
    assert cp.surface_alive("surface:5") is False

    fake_runner.queue(stdout="", stderr="", returncode=1)
    assert cp.surface_alive("surface:5") is None

    assert cp.surface_alive(None) is None


def test_close_surface_argv(fake_runner: FakeRunner):
    fake_runner.queue(returncode=0, stdout="closed")
    r = cp.close_surface("surface:8")
    assert r.returncode == 0
    assert fake_runner.calls[-1][0] == ["close-surface", "--surface", "surface:8"]


def test_identify_and_caller_surface(fake_runner: FakeRunner):
    payload: dict[str, Any] = {
        "caller": {"surface_ref": "surface:1", "pane_ref": "pane:2"},
    }
    fake_runner.queue(stdout=json.dumps(payload))
    assert cp.identify() == payload

    fake_runner.queue(stdout=json.dumps(payload))
    assert cp.caller_surface() == ("surface:1", "pane:2")
