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
```

Smokes:

```bash
python3 -m py_compile agency/scripts/*.py
export AGENCY_ROOT="$PWD/.pi/agency" AGENCY_PROJECT_ROOT="$PWD"
python3 agency/scripts/agency_ctl.py list
```

## Principles

- Keep the control plane thin: spawn / list / delegate / wait / release / init
- Project state stays under `.pi/agency`; package code stays in this repo
- Prefer documenting recovery (re-wait vs respawn) over one-shot run tools
- Do not commit inbox/outbox/artifacts or secrets

## PRs

- Small, focused diffs
- Update `docs/architecture.md` when locking a design decision
- Note any CE skill vendoring changes in `vendor/compound-engineering/NOTICE`
