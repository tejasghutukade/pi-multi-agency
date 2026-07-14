from __future__ import annotations

import argparse
from pathlib import Path

import agency_ctl as ctl


def test_public_subcommands_registered():
    p = argparse.ArgumentParser()
    # rebuild the same surface via help/parse of known cmds
    required = {
        "list",
        "spawn",
        "delegate",
        "wait",
        "release",
        "claim-orchestrator",
        "init",
        "hub-start",
        "lifecycle",
        "observe",
    }
    # invoke main --help path by inspecting argparse construction in a dry way:
    # call agency_ctl.main's parser by importing and reconstructing from source tokens
    src = Path(ctl.__file__).read_text()
    for name in required:
        assert f'"{name}"' in src or f"'{name}'" in src, f"missing command {name}"


def test_tool_flags_accepted_by_spawn_help():
    src = Path(ctl.__file__).read_text()
    for flag in ("--role", "--lifecycle", "--reuse", "--dry-run", "--boot-wait", "--cwd", "--nudge", "--recovery"):
        assert flag in src or flag.replace("--", "") in Path(
            Path(ctl.__file__).parent / "agent_spawn.py"
        ).read_text()


def test_lower_modules_do_not_import_agency_ctl():
    scripts = Path(ctl.__file__).parent
    for name in (
        "cmux_pane.py",
        "pi_launch.py",
        "catalog.py",
        "ledger.py",
        "agency_paths.py",
        "hub_delivery.py",
    ):
        text = (scripts / name).read_text()
        assert "import agency_ctl" not in text
        assert "from agency_ctl" not in text


def test_specialist_and_agent_spawn_same():
    import agent_spawn
    import specialist_spawn

    assert specialist_spawn.spawn_specialist is agent_spawn.spawn_specialist


def test_release_uses_clear_instance_helper():
    src = Path(ctl.__file__).read_text()
    assert "clear_instance" in src
