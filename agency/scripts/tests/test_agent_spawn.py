from __future__ import annotations

import inspect
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import agent_spawn as asp
from pi_launch import shell_quote
import pytest


class FakeCtl:
    def __init__(self, root: Path):
        self.root = root
        self.authority_calls = []
        self.stage_spawn_calls = []

    def require_operation_authority(self, root, *, pipeline_id=None, recovery=False):
        self.authority_calls.append({"pipeline_id": pipeline_id, "recovery": recovery})
        return {"intercomName": "orchestrator"}

    def require_active_dispatched_stage_spawn(self, root, pipeline_id, role, instance):
        self.stage_spawn_calls.append(
            {"pipeline_id": pipeline_id, "role": role, "instance": instance}
        )
        return {
            "id": "stage",
            "role": role,
            "status": "dispatched",
            "assignedInstance": instance,
        }

    def reconcile_cmux(self, root):
        return {"ok": True}

    def bus_run(self, root, args):
        return {"ok": True}


@pytest.fixture
def spawn_env(tmp_path: Path, monkeypatch):
    project = tmp_path / "owner project"
    root = project / ".pi" / "agency"
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENCY_ROOT", str(root))
    monkeypatch.setenv("AGENCY_PROJECT_ROOT", str(project))
    (root / "agents.yaml").write_text(
        """agents:
  pipeline-runner:
    lifecycleDefault: temporary
  scout:
    lifecycleDefault: temporary
    peers: [orchestrator]
  planner:
    lifecycleDefault: persistent
    peers: [orchestrator]
  worker:
    lifecycleDefault: persistent
    peers: [orchestrator]
spawn:
  maxSpecialistPanes: 2
  allowPlanTempTwin: true
  allowWorkTwin: false
"""
    )
    (root / "sessions.json").write_text(json.dumps({"version": 1, "instances": []}) + "\n")
    monkeypatch.setattr(asp, "_ctl", lambda: FakeCtl(root))
    return root


def test_pipeline_runner_command_has_fixed_serve_shape(tmp_path: Path):
    work = tmp_path / "work"
    root = tmp_path / "agency"
    project = tmp_path / "project"

    command = asp.build_pipeline_runner_command(
        work=work,
        root=root,
        project=project,
        instance="runner-01",
    )

    expected_process = shlex.join(
        [
            "env",
            f"AGENCY_ROOT={root}",
            f"AGENCY_PROJECT_ROOT={project}",
            sys.executable,
            str((asp.scripts_dir() / "agency_ctl.py").resolve()),
            "pipeline-runner",
            "serve",
            "--instance",
            "runner-01",
        ]
    )
    assert command == f"cd {shlex.quote(str(work))} && {expected_process}"
    assert "exec" not in shlex.split(command)
    assert "AGENCY_ROOT=" in command
    assert "AGENCY_PROJECT_ROOT=" in command
    assert "pi --approve" not in command
    assert "boot" not in command.lower()
    assert "--pipeline" not in command
    assert "--topic" not in command


def test_pipeline_runner_command_quotes_hostile_paths_as_single_arguments(tmp_path: Path):
    work = tmp_path / "work dir; printf exploited\n'quoted'"
    root = tmp_path / "agency $(printf exploited)"
    project = tmp_path / 'project "quoted" && printf exploited'

    command = asp.build_pipeline_runner_command(
        work=work,
        root=root,
        project=project,
        instance="runner_safe-2",
    )

    assert shlex.split(command) == [
        "cd",
        str(work),
        "&&",
        "env",
        f"AGENCY_ROOT={root}",
        f"AGENCY_PROJECT_ROOT={project}",
        sys.executable,
        str((asp.scripts_dir() / "agency_ctl.py").resolve()),
        "pipeline-runner",
        "serve",
        "--instance",
        "runner_safe-2",
    ]


@pytest.mark.parametrize(
    "instance",
    ["", "-runner", "_runner", "runner name", "runner\nname", "runner;touch", "runner$id", "runner/id"],
)
def test_pipeline_runner_command_rejects_malformed_instance(instance: str, tmp_path: Path):
    with pytest.raises(ValueError, match="instance identifier"):
        asp.build_pipeline_runner_command(
            work=tmp_path,
            root=tmp_path,
            project=tmp_path,
            instance=instance,
        )


def test_pipeline_runner_command_accepts_safe_identifier_family(tmp_path: Path):
    command = asp.build_pipeline_runner_command(
        work=tmp_path,
        root=tmp_path,
        project=tmp_path,
        instance="7Runner_name-2",
    )
    assert shlex.split(command)[-1] == "7Runner_name-2"


def test_pipeline_runner_command_signature_has_no_generic_process_inputs():
    signature = inspect.signature(asp.build_pipeline_runner_command)
    assert list(signature.parameters) == ["work", "root", "project", "instance"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    assert not {
        "command",
        "pane",
        "argv",
        "pipeline_id",
        "pipeline_name",
        "topic",
        "agent",
        "config",
    } & signature.parameters.keys()


def test_reuse_idle(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-told", "role": "scout", "status": "idle", "lifecycle": "temporary"}
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    result = asp.spawn_specialist("scout", reuse=True, dry_run=True)
    assert result["action"] == "reuse"


def test_max_panes(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "scout-t1", "role": "scout", "status": "idle"},
            {"intercomName": "scout-t2", "role": "scout", "status": "idle"},
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="max specialist panes"):
        asp.spawn_specialist("scout", dry_run=True)
    with pytest.raises(RuntimeError, match="max specialist panes"):
        asp.spawn_specialist("scout", name="scout-t3", dry_run=True, pipeline_id="p-123")


def test_pipeline_authority_still_enforces_worker_sole_writer(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {"intercomName": "worker", "role": "worker", "status": "idle"},
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="sole writer"):
        asp.spawn_specialist("worker", name="worker-new", dry_run=True, pipeline_id="p-123")


def test_spawn_threads_pipeline_authority_before_policy(spawn_env: Path, monkeypatch):
    fake = FakeCtl(spawn_env)
    monkeypatch.setattr(asp, "_ctl", lambda: fake)
    asp.spawn_specialist("scout", name="scout-t1", dry_run=True, pipeline_id="p-123")
    assert fake.authority_calls == [{"pipeline_id": "p-123", "recovery": False}]
    assert fake.stage_spawn_calls == [
        {"pipeline_id": "p-123", "role": "scout", "instance": "scout-t1"}
    ]


def test_dry_run_creates_idle_row(spawn_env: Path):
    result = asp.spawn_specialist("scout", dry_run=True, boot_wait=0, nudge=False)
    assert result["action"] == "spawn-dry-run"
    assert result["instance"]["status"] == "idle"
    data = json.loads((spawn_env / "sessions.json").read_text())
    assert len(data["instances"]) == 1


def test_persistent_specialist_uses_canonical_role_name_and_lifecycle(spawn_env: Path):
    result = asp.spawn_specialist("planner", dry_run=True, boot_wait=0)
    assert result["instance"]["intercomName"] == "planner"
    assert result["instance"]["role"] == "planner"
    assert result["instance"]["lifecycle"] == "persistent"


def test_recovery_launch_passes_explicit_surface_gate_override(spawn_env: Path, monkeypatch):
    recoveries: list[bool] = []

    class RecoveryCtl(FakeCtl):
        def require_orchestrator(self, root, *, recovery=False):
            recoveries.append(recovery)
            return {"intercomName": "orchestrator"}

    monkeypatch.setattr(asp, "_ctl", lambda: RecoveryCtl(spawn_env))
    result = asp.spawn_specialist("scout", recovery=True, dry_run=True, boot_wait=0)
    assert result["action"] == "spawn-dry-run"
    assert recoveries == [True]


def test_spawn_nudge_is_noop_compatibility_arg():
    assert asp.spawn_specialist.__kwdefaults__["nudge"] is False
    text = asp.bootstrap_text("worker", None, ".pi/agency/charters/worker.md", None, "/tmp/agency")
    assert "nudge" not in text.lower()
    assert "$BUS" not in text


def test_bootstrap_uses_broker_tools_only(spawn_env: Path):
    text = asp.bootstrap_text(
        "worker",
        None,
        ".pi/agency/charters/worker.md",
        None,
        str(spawn_env),
    )
    assert f"Agency ownership was established before Pi started at {spawn_env}." in text
    assert 'export AGENCY_ROOT=' not in text
    assert 'export BUS=' not in text
    assert f'export MEMORY="{asp.scripts_dir() / "memory.py"}"' in text
    assert 'python3 "$BUS" recv --as work --wait 60 --interval 2' not in text
    assert "agency_report / agency_ask / agency_progress" in text
    assert "$BUS" not in text
    assert "fallback" not in text.lower()


def test_packaged_prompts_do_not_recommend_project_script_paths():
    repo = Path(__file__).resolve().parents[3]
    offenders = []
    needles = (
        "python3 .pi/agency/scripts/",
        'BUS="python3 .pi/agency/scripts/',
        'CTL="python3 .pi/agency/scripts/',
    )
    for base in (repo / "agency", repo / "agents"):
        for path in base.rglob("*"):
            if "scripts/tests" in path.as_posix():
                continue
            if path.is_file() and path.suffix in {".md", ".py", ".sh"}:
                text = path.read_text(errors="ignore")
                if any(needle in text for needle in needles):
                    offenders.append(str(path.relative_to(repo)))
    assert offenders == []


def test_reference_repo_spawn_keeps_owner_context_before_pi(spawn_env: Path, tmp_path: Path, monkeypatch):
    reference = tmp_path / "reference repo"
    reference.mkdir()
    sent: list[str] = []
    monkeypatch.setattr(asp, "open_pane", lambda *args, **kwargs: {"surface": "surface:1", "pane": "pane:1"})
    monkeypatch.setattr(asp, "send_to_surface", lambda _surface, command: sent.append(command))

    result = asp.spawn_specialist("scout", cwd=str(reference), boot_wait=0)
    owner = spawn_env.parent.parent
    assert result["instance"]["cwd"] == str(reference.resolve())
    assert sent[0].startswith(
        f"cd {shell_quote(str(reference.resolve()))} && "
        f"AGENCY_ROOT={shell_quote(str(spawn_env.resolve()))} "
        f"AGENCY_PROJECT_ROOT={shell_quote(str(owner.resolve()))} pi "
    )


def test_managed_guidance_uses_broker_status_not_prompt_time_exports():
    repo = Path(__file__).resolve().parents[3]
    guidance = [
        repo / "skills" / "agency-orchestrator" / "SKILL.md",
        repo / "agency" / "skills" / "orchestrator" / "SKILL.md",
        *[
            repo / "agency" / "charters" / f"{name}.md"
            for name in ("orchestrator", "scout", "brainstorm", "planner", "worker", "debug", "coderev", "docrev")
        ],
    ]
    for path in guidance:
        text = path.read_text()
        assert "/agency-broker-status" in text, path
    for path in guidance[2:]:
        on_each = path.read_text().split("## On each delegation", 1)[-1]
        assert '`export AGENCY_ROOT="<project>/.pi/agency"`' not in on_each, path


def test_agent_spawn_cli_exposes_pipeline_id_for_spawn():
    result = subprocess.run(
        [sys.executable, str(Path(asp.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--pipeline-id" in result.stdout
    assert "--pipeline " not in result.stdout


def test_pipeline_spawn_requires_name_and_never_substitutes_idle_instance(spawn_env: Path):
    data = {
        "version": 1,
        "instances": [
            {
                "intercomName": "scout-t-other",
                "role": "scout",
                "status": "idle",
                "lifecycle": "temporary",
            }
        ],
    }
    (spawn_env / "sessions.json").write_text(json.dumps(data) + "\n")
    with pytest.raises(RuntimeError, match="explicit --name"):
        asp.spawn_specialist("scout", pipeline_id="p-123", reuse=True, dry_run=True)
    result = asp.spawn_specialist(
        "scout",
        name="scout-t-reserved",
        pipeline_id="p-123",
        reuse=True,
        dry_run=True,
    )
    assert result["action"] == "spawn-dry-run"
    assert result["instance"]["intercomName"] == "scout-t-reserved"


def test_fixed_pipeline_runner_ignores_malicious_config_and_never_builds_pi(
    spawn_env: Path, monkeypatch
):
    (spawn_env / "agents.yaml").write_text(
        """agents:
  pipeline-runner:
    lifecycleDefault: temporary
    command: "printf exploited"
    pane: "evil"
    runnerCommand: "pi --approve"
spawn:
  maxSpecialistPanes: 2
"""
    )
    sent = []
    monkeypatch.setattr(asp, "open_pane", lambda direction, focus: {"surface": "s1", "pane": "p1"})
    monkeypatch.setattr(
        asp,
        "write_boot_prompt",
        lambda *args, **kwargs: pytest.fail("fixed runner must not write a boot prompt"),
    )
    monkeypatch.setattr(
        asp,
        "build_pi_command",
        lambda **kwargs: pytest.fail("fixed runner must not build a Pi command"),
    )
    monkeypatch.setattr(asp, "send_to_surface", lambda surface, command: sent.append((surface, command)))

    result = asp.spawn_specialist("pipeline-runner", name="pipeline-runner-t1", boot_wait=0)

    assert result["action"] == "spawn"
    assert "processCommand" in result
    assert "piCommand" not in result and "bootPromptPath" not in result
    assert sent == [("s1", result["processCommand"])]
    assert "pipeline-runner serve --instance pipeline-runner-t1" in result["processCommand"]
    assert "printf exploited" not in result["processCommand"]
    assert "pi --approve" not in result["processCommand"]


def test_ordinary_specialist_spawn_still_uses_pi(spawn_env: Path, monkeypatch):
    sent = []
    boot_path = spawn_env / "boot.txt"
    monkeypatch.setattr(asp, "open_pane", lambda direction, focus: {"surface": "s1", "pane": "p1"})
    monkeypatch.setattr(asp, "write_boot_prompt", lambda root, instance, boot: boot_path)
    monkeypatch.setattr(asp, "build_pi_command", lambda **kwargs: "ordinary-pi-command")
    monkeypatch.setattr(asp, "send_to_surface", lambda surface, command: sent.append((surface, command)))

    result = asp.spawn_specialist("scout", boot_wait=0)

    assert result["action"] == "spawn"
    assert result["piCommand"] == "ordinary-pi-command"
    assert sent == [("s1", "ordinary-pi-command")]


def test_dual_entry_same_function():
    import specialist_spawn as ss

    assert ss.spawn_specialist is asp.spawn_specialist


def test_spawn_pipeline_init_creates_run_and_lock(spawn_env, monkeypatch):
    root = spawn_env
    (root / "pipelines.yaml").write_text(
        "pipelines:\n"
        "  smoke:\n"
        "    description: smoke\n"
        "    onFailure: stop\n"
        "    stages:\n"
        "      - id: w\n"
        "        role: worker\n"
        "        goal: work\n"
        "        outputs: [primary]\n"
        "        inputs: []\n"
    )
    monkeypatch.setattr(asp, "_ctl", lambda: FakeCtl(root))
    monkeypatch.setattr(
        asp, "open_pane", lambda direction, focus=False: {"surface": "surface:runner", "pane": "pane:runner"}
    )
    sent = []
    monkeypatch.setattr(
        asp, "send_to_surface", lambda surface, command: sent.append((surface, command))
    )

    result = asp.spawn_specialist("pipeline-runner", pipeline_name="smoke", topic="my topic")

    assert result["action"] == "spawn"
    assert result["pipelineId"]
    assert result["finalTaskId"] == f"pipe-done-{result['pipelineId']}"
    from pipeline_state import get_active_run, read_lock

    run = get_active_run(root)
    assert run["pipelineName"] == "smoke"
    assert run["topic"] == "my topic"
    assert run["status"] == "running"
    lock = read_lock(root)
    assert lock["pipelineId"] == result["pipelineId"]
    assert lock["ownerId"] == result["instance"]["intercomName"]
    assert sent and sent[0][0] == "surface:runner"
