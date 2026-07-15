from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import agency_ctl
import catalog


def test_catalog_load_agents_fixture(tmp_path: Path):
    yaml_text = """agents:
  scout:
    lifecycleDefault: temporary
    peers: [orchestrator]
    agentPath: agents/scout.md
spawn:
  maxSpecialistPanes: 4
"""
    (tmp_path / "agents.yaml").write_text(yaml_text)
    data = catalog.load_agents(tmp_path)
    assert "scout" in (data.get("agents") or {})
    assert catalog.max_specialist_panes(data) == 4
    assert catalog.role_defaults(data, "scout").get("lifecycleDefault") == "temporary"


def test_catalog_frontmatter(tmp_path: Path):
    path = tmp_path / "agent.md"
    path.write_text("---\ntools: read,grep\nname: Scout\n---\n\n# body\n")
    fm = catalog.parse_agent_frontmatter(path)
    assert fm.get("tools") == "read,grep"
    empty = tmp_path / "no-fm.md"
    empty.write_text("# no frontmatter\n")
    assert catalog.parse_agent_frontmatter(empty) == {}


def test_catalog_role_of():
    assert catalog.role_of("orchestrator") == "orchestrator"
    assert catalog.role_of("scout-tabc") == "scout"
    assert catalog.role_of("planner") == "planner"


def test_catalog_does_not_import_agency_ctl():
    src = Path(catalog.__file__).read_text()
    assert "import agency_ctl" not in src
    assert "from agency_ctl" not in src


PIPELINE = """pipelines:
  implementation:
    description: Plan then implement
    onFailure: stop
    stages:
      - id: scout
        role: scout
        goal: "Scout: {topic}"
        outputs: [primary, notes]
        inputs: []
      - id: plan
        role: planner
        goal: "Plan: {topic}"
        outputs: [primary]
        inputs:
          - stage: scout
            artifacts: [primary]
"""


def _write_pipeline(root: Path, text: str = PIPELINE) -> Path:
    path = root / "pipelines.yaml"
    path.write_text(text)
    return path


def test_pipeline_catalog_happy_path(tmp_path: Path):
    _write_pipeline(tmp_path)

    data = catalog.load_pipelines(tmp_path)

    pipeline = data["pipelines"]["implementation"]
    assert [stage["id"] for stage in pipeline["stages"]] == ["scout", "plan"]
    assert pipeline["stages"][1]["inputs"] == [
        {"stage": "scout", "artifacts": ["primary"]}
    ]


def test_pipeline_catalog_missing_file_returns_empty(tmp_path: Path):
    assert catalog.load_pipelines(tmp_path) == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (PIPELINE.replace("implementation:", "bad.name:"), "invalid pipeline name 'bad.name'"),
        (PIPELINE.replace("      - id: scout", "      - id: bad.name"), "invalid stage id 'bad.name'"),
    ],
)
def test_pipeline_catalog_rejects_invalid_identifiers(tmp_path: Path, text: str, message: str):
    _write_pipeline(tmp_path, text)
    with pytest.raises(ValueError, match=message):
        catalog.load_pipelines(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("pipelines: [", "malformed YAML"),
        ("- pipelines", "root must be a mapping"),
        ("pipelines: {}\nextra: true\n", "unsupported key 'extra'"),
        ("pipelines:\n  implementation:\n    stages: []\n    surprise: true\n", "unsupported key 'surprise'"),
    ],
)
def test_pipeline_catalog_rejects_malformed_roots_and_unsupported_keys(
    tmp_path: Path, text: str, message: str
):
    _write_pipeline(tmp_path, text)
    with pytest.raises(ValueError, match=message):
        catalog.load_pipelines(tmp_path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("      - id: plan", "duplicate stage id 'plan'"),
        ("        outputs: []", "outputs must be a non-empty list"),
        ("        outputs: [primary, primary]", "duplicate output 'primary'"),
        ("        outputs: [primary, bad.name]", "invalid output 'bad.name'"),
        ("            artifacts: [primary, primary]", "duplicate selector 'scout.primary'"),
        ("          - stage: missing", "unknown or forward stage reference 'missing'"),
        ("          - stage: plan", "unknown or forward stage reference 'plan'"),
        ("            artifacts: [missing]", "undeclared output 'scout.missing'"),
        ("        role: missing-role", "unknown role 'missing-role'"),
        ("    onFailure: retry", "onFailure must be stop or continue"),
        ('        goal: "Plan: {subject}"', "unsupported placeholder 'subject'"),
    ],
)
def test_pipeline_catalog_rejects_invalid_stage_contracts(
    tmp_path: Path, replacement: str, message: str
):
    text = PIPELINE
    if replacement == "      - id: plan":
        text = text.replace("      - id: scout", replacement)
    elif replacement.startswith("    onFailure"):
        text = text.replace("    onFailure: stop", replacement)
    elif replacement.startswith('        goal: "Plan'):
        text = text.replace('        goal: "Plan: {topic}"', replacement)
    elif replacement.startswith("          - stage"):
        text = text.replace("          - stage: scout", replacement)
    elif replacement.startswith("            artifacts"):
        text = text.replace("            artifacts: [primary]", replacement)
    elif replacement.startswith("        role"):
        text = text.replace("        role: planner", replacement)
    else:
        text = text.replace("        outputs: [primary]\n        inputs:", replacement + "\n        inputs:")
    _write_pipeline(tmp_path, text)

    with pytest.raises(ValueError, match=message) as exc:
        catalog.load_pipelines(tmp_path)

    assert "pipeline 'implementation'" in str(exc.value)
    if "onFailure" not in message:
        assert "stage" in str(exc.value)


def test_pipeline_catalog_rejects_duplicate_selector_across_input_entries(tmp_path: Path):
    text = PIPELINE.replace(
        "          - stage: scout\n            artifacts: [primary]",
        "          - stage: scout\n            artifacts: [primary]\n"
        "          - stage: scout\n            artifacts: [primary]",
    )
    _write_pipeline(tmp_path, text)
    with pytest.raises(ValueError, match="duplicate selector 'scout.primary'"):
        catalog.load_pipelines(tmp_path)


def test_pipeline_catalogs_do_not_leak_between_roots(tmp_path: Path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _write_pipeline(one, PIPELINE.replace("implementation:", "first:"))
    _write_pipeline(two, PIPELINE.replace("implementation:", "second:"))

    assert set(catalog.load_pipelines(one)["pipelines"]) == {"first"}
    assert set(catalog.load_pipelines(two)["pipelines"]) == {"second"}


def test_init_force_seeds_pipeline_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert agency_ctl.cmd_init(argparse.Namespace(project=str(tmp_path), force=True)) == 0
    capsys.readouterr()

    seeded = tmp_path / ".pi" / "agency" / "pipelines.yaml"
    assert seeded.read_text() == (agency_ctl.kit_root() / "pipelines.yaml").read_text()
    assert "implementation" in catalog.load_pipelines(seeded.parent)["pipelines"]
