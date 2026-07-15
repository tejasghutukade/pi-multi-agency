from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import cmux_pane as cp
import pi_launch as pl
import pytest


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float = 15) -> CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:1] == ["new-split"]:
            return CompletedProcess(args=args, returncode=0, stdout="surface:11\n", stderr="")
        if args[:2] == ["tree", "--json"]:
            tree = {
                "windows": [
                    {
                        "workspaces": [
                            {"panes": [{"ref": "pane:2", "surfaces": [{"ref": "surface:11"}]}]}
                        ]
                    }
                ]
            }
            import json

            return CompletedProcess(args=args, returncode=0, stdout=json.dumps(tree), stderr="")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_cmux():
    runner = FakeRunner()
    cp.set_runner(runner)
    yield runner
    cp.set_runner(None)


def test_shell_quote():
    assert pl.shell_quote("a'b") == "'a'\"'\"'b'"


def test_build_pi_command_variants(tmp_path: Path):
    boot = tmp_path / "boot.txt"
    boot.write_text("hi")
    persona = tmp_path / "persona.md"
    persona.write_text("x")

    cmd = pl.build_pi_command(
        work="/proj",
        instance_name="scout-t01",
        agent_path=persona,
        tools=" read, grep , ",
        boot_path=boot,
        agency_root="/owner/.pi/agency",
        agency_project_root="/owner",
    )
    assert "cd '/proj' && AGENCY_ROOT='/owner/.pi/agency' AGENCY_PROJECT_ROOT='/owner' pi --approve --name 'scout-t01'" in cmd
    assert f"--append-system-prompt '{persona}'" in cmd
    assert "--tools 'read,grep'" in cmd
    assert f'"$(cat \'{boot}\')"' in cmd

    bare = pl.build_pi_command(work="/p", instance_name="x", message="hello")
    assert bare.endswith("'hello'")

    no_extra = pl.build_pi_command(work="/p", instance_name="x")
    assert no_extra == "cd '/p' && pi --approve --name 'x'"


def test_build_pi_command_quotes_project_context_before_pi():
    cmd = pl.build_pi_command(
        work="/reference repo/it's here",
        instance_name="scout",
        agency_root="/owner project's/.pi/agency",
        agency_project_root="/owner project's",
    )
    assert cmd.startswith(
        "cd '/reference repo/it'\"'\"'s here' && "
        "AGENCY_ROOT='/owner project'\"'\"'s/.pi/agency' "
        "AGENCY_PROJECT_ROOT='/owner project'\"'\"'s' pi "
    )


def test_launch_pi_opens_and_sends(fake_cmux: FakeRunner):
    result = pl.launch_pi("/work", "scout-t01", tools="read", message="boot now", direction="right")
    assert result["surface"] == "surface:11"
    assert result["pane"] == "pane:2"
    assert "pi --approve --name 'scout-t01'" in result["command"]
    assert fake_cmux.calls[0][:2] == ["new-split", "right"]
    assert fake_cmux.calls[-1][:3] == ["send", "--surface", "surface:11"]


def test_write_boot_prompt(tmp_path: Path):
    path = pl.write_boot_prompt(tmp_path, "scout-t01", "hello")
    assert path.read_text() == "hello"
    assert path.parent.name == "_boot"
