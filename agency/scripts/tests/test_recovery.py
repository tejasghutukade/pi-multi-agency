from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cmux_pane as cp
import recovery
from subprocess import CompletedProcess


class FakeRunner:
    def __call__(self, args: list[str], timeout: float = 15) -> CompletedProcess[str]:
        return CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")


def _iso_ago(seconds: float) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - seconds, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def test_tick_skips_hub_and_no_task(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {"intercomName": "orchestrator", "role": "orchestrator", "status": "idle"},
                    {"intercomName": "scout-t01", "role": "scout", "status": "idle", "taskId": None},
                ],
            }
        )
        + "\n"
    )

    class FakeCtl:
        def agency_root(self):
            return tmp_path

        def load_sessions(self, root):
            return json.loads((root / "sessions.json").read_text())

        def find_instance(self, data, name):
            for i in data["instances"]:
                if i["intercomName"] == name:
                    return i
            return None

        def save_sessions(self, root, data):
            (root / "sessions.json").write_text(json.dumps(data) + "\n")

        def caller_surface(self):
            raise RuntimeError("unused")

    monkeypatch.setattr(recovery, "import_ctl", lambda: FakeCtl())

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert recovery.cmd_tick(Namespace(name="orchestrator", grace_sec=60, nudge_wait_sec=25)) == 0
    assert "hub" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert recovery.cmd_tick(Namespace(name="scout-t01", grace_sec=60, nudge_wait_sec=25)) == 0
    assert "no-task" in buf.getvalue()


def test_tick_grace_then_nudge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    cp.set_runner(FakeRunner())
    try:
        (tmp_path / "sessions.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "instances": [
                        {
                            "intercomName": "scout-t01",
                            "role": "scout",
                            "status": "idle",
                            "taskId": "t1",
                            "cmuxSurface": "surface:1",
                            "silentSettleAt": _iso_ago(120),
                            "nudgeCount": 0,
                        }
                    ],
                }
            )
            + "\n"
        )

        class FakeCtl:
            def agency_root(self):
                return tmp_path

            def load_sessions(self, root):
                return json.loads((root / "sessions.json").read_text())

            def find_instance(self, data, name):
                return data["instances"][0]

            def save_sessions(self, root, data):
                (root / "sessions.json").write_text(json.dumps(data) + "\n")

            def caller_surface(self):
                return "surface:1", "pane:1"

        monkeypatch.setattr(recovery, "import_ctl", lambda: FakeCtl())
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = recovery.cmd_tick(Namespace(name="scout-t01", grace_sec=60, nudge_wait_sec=25))
        assert rc == 0
        assert '"nudged"' in buf.getvalue() or "nudged" in buf.getvalue()
        data = json.loads((tmp_path / "sessions.json").read_text())
        assert data["instances"][0]["nudgeCount"] == 1
        assert data["instances"][0]["awaitingStartAfterNudge"] is True
    finally:
        cp.set_runner(None)


def test_idle_teardown_skips_non_temporary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "intercomName": "plan",
                        "role": "plan",
                        "lifecycle": "persistent",
                        "status": "idle",
                    }
                ],
            }
        )
        + "\n"
    )

    class FakeCtl:
        def agency_root(self):
            return tmp_path

        def load_sessions(self, root):
            return json.loads((root / "sessions.json").read_text())

        def find_instance(self, data, name):
            return data["instances"][0]

        def caller_surface(self):
            return "s", "p"

        def project_root(self):
            return tmp_path

    monkeypatch.setattr(recovery, "import_ctl", lambda: FakeCtl())
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert recovery.cmd_idle_teardown(Namespace(name="plan", reason=None, idle_sec=300)) == 0
    assert "not-temporary" in buf.getvalue()
