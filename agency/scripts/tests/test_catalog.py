from __future__ import annotations

from pathlib import Path

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
    assert catalog.role_of("plan") == "plan"


def test_catalog_does_not_import_agency_ctl():
    src = Path(catalog.__file__).read_text()
    assert "import agency_ctl" not in src
    assert "from agency_ctl" not in src
