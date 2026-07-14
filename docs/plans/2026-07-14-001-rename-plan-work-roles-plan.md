---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
created: 2026-07-14
title: Rename agency roles `plan`→`planner` and `work`→`worker`
---

# Rename agency roles `plan`→`planner` and `work`→`worker`

## Problem frame

The Multi-Agency system registers specialist roles in `agency/agents.yaml`
and mirrors them as persona files (`agents/<role>.md`) and charters
(`agency/charters/<role>.md`). Two roles use terse names (`plan`, `work`)
while the rest of the roster uses role-ish nouns. The user wants:

- `plan` → **`planner`**
- `work` → **`worker`**

This is a **mechanical, method-obvious rename** (no architectural or
behavioral change). The only correctness risk is leaving a stale reference
to the old name in any of the synchronized files or in the two project trees
(`multi-agency` repo + `easy-apply` project), which would cause
`unknown role: plan` / `unknown role: work` at spawn time.

## Scope boundary

**In scope**
- Rename the `plan` and `work` role keys + all derived identifiers
  (`intercomName`, `charterPath`, `agentPath`, `peers` lists, memory-init
  `--role`, twin-policy comments, orchestrator playbook, runbook, tests).
- Update prose references to "Plan"/"Work" inside charters/personas that
  name those roles (e.g. "that is Plan/Work", "recommend to Planner").
- Apply identically to **both** the `multi-agency` repo (source of truth)
  and the `easy-apply` project's `.pi/` tree (separate, gitignored copy).

**Out of scope**
- Renaming any other role (scout, researcher, brainstorm, debug, coderev,
  docrev, orchestrator) — explicitly not requested.
- Changing skill paths (`ce-plan`/`ce-work` skills keep their names; only
  the *role* is renamed).
- Changing the broker transport, spawn logic beyond the two role
  special-cases, or session manifest schema.

## Requirements traceability

| Req | Source |
|-----|--------|
| R1 | `plan` role key becomes `planner` everywhere | user message |
| R2 | `work` role key becomes `worker` everywhere | user message |
| R3 | No stale `plan`/`work` role references remain in either project | "cover all bases… docs also updated" |
| R4 | `agency_spawn({role:"planner"})` and `{role:"worker"}` resolve | implied by correctness |
| R5 | easy-apply project usable with new names | prior session (spawn failed there) |

## Decisions (with rationale)

- **D1 — Rename role keys + files, keep skill paths.** The skills
  `ce-plan`/`ce-work` are compound-engineering assets; renaming them is a
  larger, unrelated change. The role→skill mapping in `agents.yaml`
  (`skillPath: .../ce-plan/SKILL.md`) stays; only the *role* name changes.
- **D2 — Use `git mv` for persona/charter files** so history is preserved
  and the rename is obvious in the diff.
- **D3 — Update `agent_spawn.py` role special-cases** (lines ~138, 148,
  196) that hard-code `role == "plan"` / `role == "work"` for twin/sole-
  writer policy. These must use `planner`/`worker` or the policy breaks.
- **D4 — easy-apply gets the same edits manually** (its `.pi/` tree is
  gitignored and was initialized from an older `agents.yaml`). We do NOT
  run `agency_init --force` blindly there because it would overwrite
  easy-apply's custom `agents.yaml`; instead we apply the same targeted
  edits to its `.pi/agency/agents.yaml` + rename its `.pi/agents/*.md` and
  `.pi/agency/charters/*.md` files.

## Implementation units

### IU1 — `agency/agents.yaml` (multi-agency repo)
- `plan:` → `planner:` (line ~47); update `charterPath`, `agentPath`,
  `intercomName: plan` → `planner`, `peers: [brainstorm, work, scout]` →
  `[brainstorm, worker, scout]`.
- `work:` → `worker:` (line ~57); update `charterPath`, `agentPath`,
  `intercomName: work` → `worker`, `peers: [plan, debug, coderev]` →
  `[planner, debug, coderev]`.
- Update other roles' `peers` that list `plan`/`work`:
  - `scout.peers: [brainstorm, plan]` → `[brainstorm, planner]`
  - `brainstorm.peers: [plan, docrev]` → `[planner, docrev]`
  - `coderev.peers: [work, codrev]` → `[worker, codrev]`
  - `docrev.peers: [brainstorm, plan, codrev]` → `[brainstorm, planner, codrev]`
  - `researcher.peers: [brainstorm, plan, scout]` → `[brainstorm, planner, scout]`
- Twin-policy comment (line ~101-102): `plan-t*` → `planner-t*`,
  "Work never twins" → "Worker never twins".

### IU2 — Persona files (multi-agency repo)
- `git mv agents/plan.md agents/planner.md`; update frontmatter
  `name: plan` → `name: planner`; prose "You are the **Plan** specialist"
  → "**Planner**"; references to "Work" → "Worker"; "Plan" as role noun
  → "Planner".
- `git mv agents/work.md agents/worker.md`; frontmatter `name: work` →
  `name: worker`; "**Work** specialist" → "**Worker**"; "Plan" → "Planner".

### IU3 — Charter files (multi-agency repo)
- `git mv agency/charters/plan.md agency/charters/planner.md`;
  `**Role id:** \`plan\`` → `\`planner\``, `**Broker instance name:** \`plan\``
  → `\`planner\``, prose "**Plan** specialist" → "**Planner**",
  "Plan"→"Planner", "Work"→"Worker".
- `git mv agency/charters/work.md agency/charters/worker.md`;
  `**Role id:** \`work\`` → `\`worker\``, broker name `work`→`worker`,
  "**Work** specialist" → "**Worker**", "Plan"→"Planner".

### IU4 — Other charters/personas referencing the old role nouns
Update prose only (no file rename) in files that mention Plan/Work as role
names:
- `agency/charters/brainstorm.md`: "implementation plans (Plan)" →
  "(Planner)", "code (Work)" → "(Worker)".
- `agency/charters/scout.md`: "write implementation plans — that is
  Brainstorm/Plan" → "Brainstorm/Planner"; "suggested next specialist:
  … plan" → "planner".
- `agency/charters/docrev.md`: peers already updated via IU1; check prose
  "plan"→"planner".
- `agency/charters/researcher.md`: "Suggested next specialist: plan" →
  "planner".
- `agents/brainstorm.md`, `agents/scout.md`, `agents/docrev.md`,
  `agents/researcher.md`: same prose fixes ("Plan"/"Work" →
  "Planner"/"Worker" where they name the role).

### IU5 — `agency/scripts/agent_spawn.py`
- Line ~138: `if role == "work":` → `if role == "worker":`
- Line ~148: `if role == "plan":` → `if role == "planner":`
- Line ~196: `role in ("plan", "work")` → `role in ("planner", "worker")`
- Confirm no other `"plan"`/`"work"` role literals exist.

### IU6 — `agency/memory-spec.md`
- Memory-init example `--role plan` → `--role planner`, `--role work` →
  `--role worker` (and any instance-name examples).

### IU7 — `skills/agency-orchestrator/SKILL.md`
- Classify table: "Plan (reuse + memory NOTES) → Work" → "Planner (reuse +
  memory NOTES) → Worker".
- Lifecycle table: `| persistent | plan, work |` → `| persistent | planner,
  worker |`.
- Prose: "Never a second Work while one is `working`" → "Worker"; "Plan
  `working` … `plan-t*` twin" → "Planner … `planner-t*`"; "Names: persistent
  = role id (`plan`)" → "(`planner`)"; "Spawn a second Work while one is
  working" → "Worker".

### IU8 — `docs/adding-an-agent-runbook.md`
- Example tool-allowlist comment "(brainstorm, plan, work)" → "(brainstorm,
  planner, worker)".
- Any `plan`/`work` role mentions in examples → updated.

### IU9 — Tests (`agency/scripts/tests/`)
- `test_agent_spawn.py`: charter path `.pi/agency/charters/work.md` →
  `worker.md`; any `role: "work"` → `"worker"`.
- `test_recovery.py`, `test_catalog.py`, others: any `role: "plan"`/
  `"work"` literals → renamed (verify by grep after IU5).

### IU10 — easy-apply project (separate tree, gitignored)
Apply IU1–IU9 equivalents to `/Users/tejasghutukade/Projects/easy-apply/.pi/`:
- `.pi/agency/agents.yaml`: same role-block + peers + comment edits.
- `git mv` (or `mv`) `.pi/agents/plan.md`→`planner.md`, `work.md`→`worker.md`
  with frontmatter + prose fixes.
- `mv` `.pi/agency/charters/plan.md`→`planner.md`, `work.md`→`worker.md`
  with Role-id + prose fixes.
- Same prose fixes in easy-apply's brainstorm/scout/docrev/researcher
  charters + personas.
- **Do NOT** run `agency_init --force` (would overwrite easy-apply's custom
  `agents.yaml`); apply targeted edits by hand.

## Verification

- **V1** `python3 -m pytest agency/scripts/tests/ -q` → all green.
- **V2** Dry-run spawn resolves both new roles in the repo:
  ```bash
  mkdir -p /tmp/v && AGENCY_PROJECT_ROOT="$PWD" python3 - <<'PY'
  import importlib.util
  spec = importlib.util.spec_from_file_location("asp","agency/scripts/agent_spawn.py")
  m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
  for r in ("planner","worker"):
      o = m.spawn_specialist(role=r, lifecycle="persistent", name=f"{r}-dry", dry_run=True, cwd="/tmp/v")
      print(r, "->", o["instance"]["role"])
  PY
  rmdir /tmp/v
  ```
  (orchestrator-surface gate may raise in bare bash; that is expected and
  not a failure of role resolution — wrap or run inside the orchestrator.)
- **V3** `grep -rn '\bplan\b\|\bwork\b' agency/agents.yaml agents/planner.md
  agents/worker.md agency/charters/planner.md agency/charters/worker.md
  skills/agency-orchestrator/SKILL.md` shows **no** role-name `plan`/`work`
  tokens outside prose where "plan"/"work" is a real English word
  (e.g. "implementation plan" is fine; `role: plan` is not).
- **V4** Repeat V3 against easy-apply's `.pi/` tree; confirm
  `agency_spawn({role:"planner"})` / `{role:"worker"}` work there.
- **V5** `agency_init --force` in the **multi-agency repo** re-syncs its
  own `.pi/` (already gitignored) so the source and live copy agree.

## Dependencies / sequencing

1. IU1 (yaml) → IU2/IU3 (files) → IU4 (prose) → IU5 (spawn py) → IU6
   (memory-spec) → IU7 (SKILL) → IU8 (runbook) → IU9 (tests) → verify
   (V1–V3).
2. IU10 (easy-apply) after repo is green; verify V4/V5.
3. Commit repo changes on the `rename-plan-work` worktree branch; push;
   open PR. easy-apply `.pi/` edits are local-only (gitignored) — no commit
   needed there, but confirm with user before touching easy-apply.

## Risks

- **RK1** Forgetting a `peers` list or prose reference → `unknown role` at
  spawn. Mitigated by V3/V4 grep + dry-run.
- **RK2** `agent_spawn.py` special-cases missed → twin/sole-writer policy
  breaks silently. Mitigated by IU5 + tests.
- **RK3** easy-apply overwrite via `--force` → loss of its custom
  `agents.yaml`. Mitigated by manual edits (IU10), not `--force`.

## Open questions (none blocking)
- None. Scope is confirmed: only `plan`→`planner`, `work`→`worker`.
