# Contributing

## Develop

```bash
git clone https://github.com/tejasghutukade/multi-agency.git
cd multi-agency
pi install .
# or: pi -e ./extensions/multi-agency
```

Scaffold local state:

```bash
python3 agency/scripts/agency_ctl.py init --force
python3 agency/scripts/agency_ctl.py hub-start
```

Smokes:

```bash
python3 -m py_compile agency/scripts/*.py
export AGENCY_ROOT="$PWD/.pi/agency" AGENCY_PROJECT_ROOT="$PWD"
python3 agency/scripts/agency_ctl.py list
python3 agency/scripts/agency_ctl.py hub-start
npm run test:broker
python3 -m pytest agency/scripts/tests
```

## Project broker invariant

An initialized project owns exactly one broker runtime beneath its canonical `.pi/agency` directory. Endpoint state derives from that agency root; transport IDs are qualified by the canonical owning project root. Logical names, ACL inputs, ledger rows, and message envelopes remain unqualified.

All managed Pi launch commands must set both `AGENCY_PROJECT_ROOT` and `AGENCY_ROOT` before the `pi` executable. Execution cwd is independent: a reference-repository Scout keeps the originating project's broker context. Do not document prompt-time `export` as a way to configure an already-running Pi process.

When testing an upgrade, pause/drain the cohort, restart every hub and specialist Pi process, then run `/agency-broker-status` in every pane before resuming. `/reload` is not a process-environment migration. Do not add automatic discovery, termination, or cleanup of the legacy user-global broker. Windows named-pipe ACL behavior remains a manual platform check when Windows CI is unavailable; retain owner-only Unix runtime modes and the Windows TCP state credential.

## Principles

- Keep the control plane thin: spawn / list / delegate / wait / release / init / hub-start
- Project state stays under `.pi/agency`; package code stays in this repo
- Orchestrator hub lock (persona + no edit/write/bash) is required — soft Prefer is not enough
- Prefer documenting recovery (re-wait vs respawn) over one-shot run tools
- Do not commit inbox/outbox/artifacts or secrets

## PRs

- Small, focused diffs
- Update `docs/architecture.md` when locking a design decision
- Note any CE skill vendoring changes in `vendor/compound-engineering/NOTICE`
