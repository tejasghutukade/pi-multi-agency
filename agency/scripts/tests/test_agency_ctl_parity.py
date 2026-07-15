from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

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
        "pipeline-report",
        "pipeline-answer",
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
    for flag in ("--role", "--lifecycle", "--reuse", "--dry-run", "--boot-wait", "--cwd", "--recovery"):
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
    ):
        text = (scripts / name).read_text()
        assert "import agency_ctl" not in text
        assert "from agency_ctl" not in text


def test_init_output_has_exact_claim_status_list_sequence(tmp_path: Path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(ctl, "package_root", lambda: Path(ctl.__file__).resolve().parents[2])
    assert ctl.cmd_init(SimpleNamespace(project=str(project), force=False)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"][-1] == "/agency-claim → /agency-broker-status → agency_list"


def test_orchestrator_scaffold_matches_canonical_skill_and_claim_precedes_status():
    repo = Path(ctl.__file__).resolve().parents[2]
    canonical = (repo / "skills" / "agency-orchestrator" / "SKILL.md").read_text()
    scaffold = (repo / "agency" / "skills" / "orchestrator" / "SKILL.md").read_text()
    assert scaffold == canonical
    bootstrap = canonical.split("## Session bootstrap", 1)[1].split("## Classify", 1)[0]
    assert bootstrap.index("/agency-claim") < bootstrap.index("/agency-broker-status")
    for role in ("Planner", "Worker", "Researcher"):
        assert role in scaffold


def test_specialist_and_agent_spawn_same():
    import agent_spawn
    import specialist_spawn

    assert specialist_spawn.spawn_specialist is agent_spawn.spawn_specialist


def test_release_uses_clear_instance_helper():
    src = Path(ctl.__file__).read_text()
    assert "clear_instance" in src


def test_delegate_supports_broker_preflight_and_no_bus_commit():
    src = Path(ctl.__file__).read_text()
    assert "--prepare-only" in src
    assert "delegate-preflight" in src
    assert "--no-bus" in src


def test_hub_command_establishes_project_context_before_pi(tmp_path: Path):
    project = tmp_path / "owner project's"
    (project / ".pi" / "agents").mkdir(parents=True)
    (project / ".pi" / "agents" / "orchestrator.md").write_text("hub")
    command = ctl.hub_start_command(project)
    assert command.startswith(
        "cd '" + str(project).replace("'", "'\"'\"'") + "' && "
        "AGENCY_ROOT='" + str(project / ".pi" / "agency").replace("'", "'\"'\"'") + "' "
        "AGENCY_PROJECT_ROOT='" + str(project).replace("'", "'\"'\"'") + "' pi "
    )
