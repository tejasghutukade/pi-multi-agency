# Multi-Agency

Orchestrator hub for **multi-specialist [pi](https://pi.dev) sessions in [cmux](https://cmux.com)** — hybrid filesystem bus, lean `agency_*` tools, and compound-engineering playbooks.

Each specialist is a normal `pi` process in its own cmux pane. The Orchestrator is the only user-facing agent. Messaging is durable JSON under `.pi/agency/inbox/` plus `cmux notify` (not pi-intercom as the primary bus).

## Requirements

- [cmux](https://cmux.com) app + CLI on `PATH`
- [pi](https://pi.dev) on `PATH`
- Python 3.9+

## Install

```bash
# user-global
pi install git:github.com/tejasghutukade/multi-agency

# or project-local (writes .pi/settings.json)
pi install -l git:github.com/tejasghutukade/multi-agency

# from a local clone (edits show up after /reload)
pi install -l /absolute/path/to/multi-agency
```

## Use in any project

Run **inside cmux** (cmux rejects control clients started outside).

```bash
cd /path/to/your-project
```

In pi (after install):

1. `/reload`
2. `/agency-init` — scaffolds `.pi/agency` + `.pi/agents`
3. Quit and start the **locked** Orchestrator hub (required — plain `pi` will freestyle):

```bash
# print the exact command for this project:
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py hub-start
# or in any pi session after install: /agency-hub
```

Example shape (the generated command shell-quotes the canonical paths):

```bash
cd "$PWD" && \
  AGENCY_ROOT="$PWD/.pi/agency" AGENCY_PROJECT_ROOT="$PWD" \
  pi --approve --name orchestrator \
  --tools read,grep,find,ls,agency_init,agency_list,agency_spawn,agency_delegate,agency_release \
  --append-system-prompt .pi/agents/orchestrator.md
```

4. `/agency-claim`, run `/agency-broker-status`, then give a real task (spawn → delegate; stay free for pushed reports)

The hub **must not** have `edit` / `write` / `bash`. Specialists implement; the Orchestrator only classifies, delegates, and synthesizes.

**Do not** keep a project-local `.pi/extensions/multi-agency/` when the package is also installed — that duplicates tools and fails hub start.

CLI equivalent for init:

```bash
export AGENCY_PROJECT_ROOT="$PWD"
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py init
# path varies by install location — use `pi list` or your clone path
```

### Control plane (spawn → delegate → free hub)

| Tool | Purpose |
|------|---------|
| `agency_init` | Scaffold project state from the package |
| `agency_list` | Roster + stale reconcile |
| `agency_spawn` | Open/reuse specialist pane |
| `agency_delegate` | Send task (`taskId`); hub stays free |
| `agency_release` | Idle persistent / teardown temp |

Commands: `/agency-init`, `/agency-claim`, `/agency-hub`, `/agency-broker-status`, `/agency-ops start|stop|status [--port N]`, `/agency_list`, `/agency_release <name> [--mode auto|idle|teardown]`

### Project-owned broker and upgrades

Each initialized project owns one broker runtime under `<project>/.pi/agency/runtime/broker`. The owning project root qualifies transport session IDs, while logical agent names and message `from`/`to` fields remain unchanged. A Scout may execute in a reference checkout, but its broker still belongs to the originating project; pane cwd never selects broker ownership.

Managed hub and specialist commands establish both `AGENCY_PROJECT_ROOT` and `AGENCY_ROOT` **before Pi starts**. Typing an `export` into a Pi prompt cannot repair the environment of that already-running Pi process. If a pane reports an unavailable or mismatched context, do not resume delegation.

For an upgrade from the legacy user-global broker: pause new delegation, drain or explicitly abandon active work, quit **every** Pi process in the project's agency cohort, restart the hub and all specialists with the generated commands, and run `/agency-broker-status` in every pane. Resume only when every pane reports the same project key and project-local endpoint family with `connected` state. An extension `/reload` alone is insufficient. Existing global broker processes are left untouched; manual cleanup is optional. This isolation protects trusted same-user local sessions from accidental collision, not from hostile local processes.

After delegate, the **lifecycle bridge** pushes specialist `report`/`ask` into the hub chat when idle, or shows a queue banner while the hub is working. Silent settle without a report triggers abandon + respawn if the specialist does not start again.

CLI (same behavior):

```bash
export AGENCY_ROOT="$PWD/.pi/agency"
export AGENCY_PROJECT_ROOT="$PWD"
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py init
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py hub-start
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py claim-orchestrator
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py spawn --role scout --lifecycle temporary
```

### Ops observer

Live roster / broker / timeline UI (localhost). Files under `.pi/agency` are truth; optional emit timeline needs `AGENCY_EVENTS=1`. Claim is a hub badge, not a gate to open the UI.

```bash
export AGENCY_ROOT="$PWD/.pi/agency"
export AGENCY_EVENTS=1   # optional — timeline emit
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py observe
# → http://127.0.0.1:8765/
```

## Layout

```
extensions/multi-agency/   # agency_* tools, lifecycle bridge, /agency-init|/agency-claim|/agency-hub
skills/                    # agency-orchestrator, scout (pi-discovered)
agency/scripts/            # layered control plane (ledger, broker helpers, cmux_pane, recovery, observe, …)
agency/observe/static/     # localhost ops UI served by agency_ctl observe
agents/                    # persona templates (--append-system-prompt)
vendor/compound-engineering/  # vendored CE skills (MIT, Every)
docs/                      # architecture board + plans
```

**Per project (created by init):** `.pi/agency/` (sessions, optional `events.jsonl`, charters copy) and `.pi/agents/`. Runtime state stays under `.pi/` (gitignored). Package scripts stay in the installed multi-agency package — not copied into `.pi/agency/scripts/`.

## Policy highlights (v0.3)

- **Hub lock:** Orchestrator starts with read/search + `agency_*` only (no edit/write/bash); persona forbids solo implementation
- **Lifecycle bridge:** `agent_start`/`agent_settled` update status; silent settle → abandon/respawn; hub idle → push report, hub busy → queue banner
- **Temp auto-close:** temporary specialists arm a **5-minute** idle timer on `agent_settled` (cancel on `agent_start`); Orchestrator need not release them
- **Ops observer:** `agency_ctl observe` — roster / broker / timeline; claim is a badge, not a gate
- **Sole-writer:** only one Work instance on the project checkout
- Max **6** specialist panes; Plan may get one temp twin; Work never twins
- Async handoff: **spawn → delegate → free hub**

## Security

- Do not commit `.pi/agency/artifacts`, live `sessions.json`, or `events.jsonl`
- Review [SECURITY.md](./SECURITY.md)
- Vendored CE skills: see [vendor/compound-engineering/NOTICE](./vendor/compound-engineering/NOTICE)

## License

MIT — see [LICENSE](./LICENSE). Vendored Compound Engineering skills remain MIT © Every (see `vendor/compound-engineering/LICENSE`).
