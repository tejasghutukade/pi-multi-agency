# Persistent agent memory (v0)

What survives across **persistent** Plan/Work delegations (and promoted temps).

## Layers

| Layer | Survives | Where | Who writes |
|-------|----------|-------|------------|
| **Pane context** | Until pane teardown / cmux kill | Live pi session | Automatic |
| **Instance NOTES** | Across idle→working cycles on same instance | `.pi/agency/memory/<intercomName>/NOTES.md` | Specialist after each report |
| **Workflow artifacts** | Forever (repo) | `.pi/agency/artifacts/<taskId>/`, `docs/plans/`, etc. | Specialist / Orchestrator |
| **Compound solutions** | Forever (repo) | `docs/solutions/` via ce-compound | **Work** after a durable fix/learning (Orchestrator may also request) |

Temps (`scout-t*`, …) may write NOTES but Orchestrator should treat them as disposable; teardown may leave files (ok).

## NOTES.md contract

Path: `.pi/agency/memory/<intercomName>/NOTES.md`

```markdown
# Memory — <intercomName> (<role>)

## Active
- Workflow / feature:
- Plan / key artifact paths:
- Decisions locked:
- Open blockers:

## Log
- YYYY-MM-DD taskId: one-line outcome + paths
```

Rules:

- Append a **Log** line on every successful `report` (and update **Active** when state changes).
- Keep Active ≤ ~40 lines; move stale detail to Log or artifact paths.
- On **reuse**, Orchestrator includes `memoryPath` in the delegate payload `contextPaths`.
- On **teardown**, leave NOTES on disk (audit); do not auto-delete.

Init helper:

```bash
python3 .pi/agency/scripts/memory.py init --as plan --role plan
python3 .pi/agency/scripts/memory.py path --as plan
```

## docs/solutions integration

When Work completes a non-trivial fix or discovers durable project vocabulary:

1. Follow `compound-engineering-plugin/skills/ce-compound/SKILL.md` (or Orchestrator delegates a follow-up).
2. Put the solutions path in the Work report + one NOTES Log line.
3. Do **not** dump full solutions bodies onto the bus — paths only.

Plan does **not** write `docs/solutions/` by default (requirements/plans stay under `docs/plans/`).

## What does *not* survive

- Unclaimed inbox envelopes (ttl / done hygiene)
- Temp pane transcript after teardown
- Orchestrator chat with the user (unless captured into an artifact)
