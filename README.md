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

# from a local clone
pi install /absolute/path/to/multi-agency
```

## Use in any project

Run **inside cmux** (cmux rejects control clients started outside).

```bash
cd /path/to/your-project
```

In pi (after install):

1. `/reload`
2. `/agency-init` — scaffolds `.pi/agency` + `.pi/agents`
3. `pi --append-system-prompt .pi/agents/orchestrator.md` (or start that way)
4. `/agency-claim` then give the Orchestrator a real task

CLI equivalent for init:

```bash
export AGENCY_PROJECT_ROOT="$PWD"
python3 ~/.pi/agent/git/github.com/tejasghutukade/multi-agency/agency/scripts/agency_ctl.py init
# path varies by install location — use `pi list` or your clone path
```

### Control plane (spawn → delegate → wait)

| Tool | Purpose |
|------|---------|
| `agency_init` | Scaffold project state from the package |
| `agency_list` | Roster + stale reconcile |
| `agency_spawn` | Open/reuse specialist pane |
| `agency_delegate` | Send task (`taskId`) |
| `agency_wait` | Poll hub inbox for that `taskId` |
| `agency_release` | Idle persistent / teardown temp |

CLI (same behavior):

```bash
export AGENCY_ROOT="$PWD/.pi/agency"
export AGENCY_PROJECT_ROOT="$PWD"
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py init
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py claim-orchestrator
python3 /path/to/multi-agency/agency/scripts/agency_ctl.py spawn --role scout --lifecycle temporary
```

## Layout

```
extensions/multi-agency/   # agency_* tools + /agency-init, /agency-claim
skills/                    # agency-orchestrator, scout (pi-discovered)
agency/                    # kit: scripts, charters, agents.yaml, specs
agents/                    # persona templates (--append-system-prompt)
vendor/compound-engineering/  # vendored CE skills (MIT, Every)
docs/                      # architecture board
```

**Per project (created by init):** `.pi/agency/` (sessions, inbox, charters copy) and `.pi/agents/`.

## Policy highlights (v0)

- **Sole-writer:** only one Work instance on the project checkout
- Max **6** specialist panes; Plan may get one temp twin; Work never twins
- Async handoff: **spawn → delegate → wait** (retry wait on timeout; respawn only if pane dead)

## Security

- Do not commit `.pi/agency/inbox`, `outbox`, `artifacts`, or live `sessions.json`
- Review [SECURITY.md](./SECURITY.md)
- Vendored CE skills: see [vendor/compound-engineering/NOTICE](./vendor/compound-engineering/NOTICE)

## License

MIT — see [LICENSE](./LICENSE). Vendored Compound Engineering skills remain MIT © Every (see `vendor/compound-engineering/LICENSE`).
