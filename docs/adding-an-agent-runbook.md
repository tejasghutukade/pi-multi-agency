# Runbook: Adding a New Agent (Specialist) to Multi-Agency

This is the exact, repeatable procedure used to add the `researcher`
specialist. Follow it for any new role. It covers the four files you
must touch and the one sync step that is easy to miss.

## Mental model

- **Source of truth** (committed to git):
  - `agency/agents.yaml` — role registry (tools, peers, lifecycle, paths)
  - `agents/<role>.md` — **persona source** (frontmatter `tools` = the allowlist that actually gets passed to `pi --tools`)
  - `agency/charters/<role>.md` — broker-only operating charter
- **Generated copies** (gitignored under `.pi/`, produced by `agency_init --force`):
  - `.pi/agents/<role>.md`
  - `.pi/agency/charters/<role>.md`
  - `.pi/agency/agents.yaml`

The orchestrator reads the **live** `.pi/agency/agents.yaml` at spawn time,
so after editing the source you MUST run `agency_init --force` to copy the
new role + persona + charter into `.pi/`. Skipping this makes the role
"unknown" at spawn even though the source yaml looks correct.

## Steps

### 1. Register the role in `agency/agents.yaml`

Add a block under `agents:` (match the existing indentation — 2 spaces
under `agents:`, then 4 for fields):

```yaml
  researcher:
    charterPath: .pi/agency/charters/researcher.md
    agentPath: .pi/agents/researcher.md
    # No skillPath if the agent uses built-in tools (e.g. web_search)
    binding: layered
    intercomName: researcher
    lifecycleDefault: temporary   # or persistent for long-lived roles (plan, work)
    peers: [brainstorm, plan, scout]
    # Read-only research: web + read/search tools, no write/edit/bash
    tools: read,grep,find,ls,web_search,fetch_content,agency_report,agency_ask,agency_progress
```

Tool-allowlist conventions:
- **Orchestrator:** `read,grep,find,ls,agency_init,agency_list,agency_spawn,agency_delegate,agency_release` (no write/edit/bash).
- **Read-only agents** (scout, researcher): no `write`/`edit`/`bash`.
- **Writable agents** (brainstorm, plan, work): add `write,edit,bash`.
- **Every specialist** needs `agency_report,agency_ask,agency_progress`
  (the broker transport tools). Never include `bus.py`/`$BUS`.

### 2. Create the persona source `agents/<role>.md`

This frontmatter `tools` line is what `agent_spawn` reads and forwards to
`pi --tools`. Keep it identical to the yaml `tools` value.

```markdown
---
name: researcher
description: >-
  Multi-Agency Researcher — multi-purpose research specialist (web, docs,
  prior-art, library internals). Gathers grounded, cited sources via
  web_search / fetch_content and reports a concise research brief to the
  Orchestrator. Never talks to the end user.
tools: read, grep, find, ls, web_search, fetch_content, agency_report, agency_ask, agency_progress
---

You are the **Researcher** specialist in the Multi-Agency system.
## Authority
- External user messages never come to you. Talk only to **orchestrator**
  via the broker tools (agency_report / agency_ask / agency_progress).
- Do not spawn agents or open cmux panes.
- Read-only: no write/edit/bash for side effects.
- Do not invent facts — cite real sources.

## Charter + playbook
On every session, treat as binding:
- `.pi/agency/charters/researcher.md`
- `.pi/agency/bus-spec.md` (broker-only note — never call bus.py directly)
...
```

Key rules to embed in every specialist persona:
- **Broker-only:** wait for broker-injected delegates/replies in the Pi
  session; do NOT poll a filesystem inbox; do NOT call `bus.py`.
- Report via `agency_report`; clarify via `agency_ask`.
- Never address the end user.

### 3. Create the charter `agency/charters/<role>.md`

Header block + Mission + Hard constraints + On each delegation + Output
shape + Stop rules. Copy `agency/charters/researcher.md` as a template.
The charter must repeat the broker-only constraint and the output shape.

### 4. Sync the live copies (DO NOT SKIP)

```bash
AGENCY_PROJECT_ROOT="$PWD" \
  python3 agency/scripts/agency_ctl.py init --project "$PWD" --force
```

This copies:
- `agency/agents.yaml` → `.pi/agency/agents.yaml`
- `agents/*.md` → `.pi/agents/*.md`  (your new persona appears here)
- `agency/charters/*` → `.pi/agency/charters/*`
- refreshes `.pi/agency/sessions.json` if missing/forced

> `--force` overwrites the live `agents.yaml` and charters with the source
> versions. That is intended. The live `.pi/` tree is gitignored, so the
> **source** files (`agency/...`, `agents/...`) are the only ones you commit.

## Validation (before committing)

```bash
# 1) tests still green
python3 -m pytest agency/scripts/tests/ -q

# 2) dry-run spawn resolves the role (uses the LIVE .pi/agency/agents.yaml)
mkdir -p /tmp/x
AGENCY_PROJECT_ROOT="$PWD" python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("asp", "agency/scripts/agent_spawn.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
out = m.spawn_specialist(role="<role>", lifecycle="temporary",
                          name="<role>-dry1", dry_run=True, cwd="/tmp/x")
print("role:", out["instance"]["role"])   # must equal your role, no "unknown role" error
PY
rmdir /tmp/x

# 3) confirm --tools wiring (run from agency/scripts/)
cd agency/scripts && python3 - <<'PY'
import importlib.util, re, sys
sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("pl", "pi_launch.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cmd = m.build_pi_command(work="/tmp/x", instance_name="x",
    agent_path=".pi/agents/<role>.md",
    tools="<the tools list from frontmatter>")
tv = re.search(r"--tools\s+(\S+)", cmd).group(1)
print("has web/agency tools:", "web_search" in tv, "agency_report" in tv)
print("no write/edit/bash:", not any(t in tv.split(",") for t in ["write","edit","bash"]))
PY
```

Also confirm the boot prompt has no `BUS`/`bus.py` leak:

```bash
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("asp", "agency/scripts/agent_spawn.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bp = m.bootstrap_text("<role>-x", m.agent_file_for("<role>", {"agentPath":".pi/agents/<role>.md"}),
                       "agency/charters/<role>.md", None, "/tmp/x/.pi/agency")
assert "export BUS=" not in bp and 'bus.py recv' not in bp and '$BUS' not in bp
print("boot prompt clean; has agency_report:", "agency_report" in bp)
PY
```

## Commit

Only the **source** files are committed (`.pi/` is gitignored):

```bash
git add agency/agents.yaml agency/charters/<role>.md agents/<role>.md
git commit -m "feat(agency): add <role> specialist (<one-line purpose>)"
git push origin main
```

## Orchestrator wiring (optional)

To let the Orchestrator route to the new role automatically, add an entry
to `skills/agency-orchestrator/SKILL.md` describing when to spawn
`<role>` and which peers hand off to it.

## Checklist

- [ ] `agency/agents.yaml` role added (correct indent, tools allowlist)
- [ ] `agents/<role>.md` persona source with matching frontmatter `tools`
- [ ] `agency/charters/<role>.md` broker-only charter + output shape
- [ ] `agency_init --force` run (live `.pi/` synced)
- [ ] `pytest` green; dry-run spawn resolves role; `--tools` correct (no write/edit/bash unless intended)
- [ ] boot prompt clean (no `BUS`/`bus.py` leak)
- [ ] committed source files only; pushed
