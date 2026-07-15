# U8 — Human-in-the-loop for pipeline stages

**Status:** planned · **Branch:** `feat/declarative-pipelines` (on top of U1–U7) · **Date:** 2026-07-14

## Context

U1–U7 shipped deterministic, crash-safe declarative pipelines. A stage ends by sending
exactly one `report` (`succeeded` / `failed` / `dependency_failed` / `needs_attention`).
When a stage cannot proceed without a human decision it reports `needs_attention`; the
runner records it, marks the run `needs_attention`, and sends a pipeline `ask` to the
hub (`catalog.HUB == "orchestrator"`, `pipeline_runtime._notify_attention`). Today that
`ask` just tells the orchestrator to "inspect state and resume explicitly" — there is **no
path that feeds an answer back into the stage**, and the orchestrator is never prompted
interactively.

Two requests, now locked as **one feature**:

1. The runner's `ask` should be surfaced to the human via the `ask_user` extension on the
   hub (a presentation step on the orchestrator side), instead of a bare chat message.
2. Stages may need several questions in sequence. They should be able to "ask the hub" and
   receive the human's answer back **into that same stage**, so the stage *continues* rather
   than ending. `report` stays reserved for *finished or crashed*.

## Locked design decisions

- **Design B (stateless continuation).** A `needs_attention` report carries a structured
  **summary of what the stage did + the specific question**. On resume we re-dispatch the
  stage with: original goal + that summary + the human's answer. The stage *re-derives*
  context from persisted state; it does **not** rely on the pane remembering.
  - Rationale: crash-safe (summary lives in `pipelines.json`), no long-lived idle panes,
    matches the durability ethos of U5. (Design A — keep the pane alive and reuse the
    instance — is explicitly *not* the primary path; it is fragile if the pane dies and
    falls back to B anyway.)
- **Brokered, never direct.** A stage asking the human goes
  `stage → report(needs_attention, question) → runner → pipeline ask → hub → ask_user →
  human → answer stored → resume re-dispatches stage`. A stage calling `ask_user` *directly*
  (bypassing the runner) is **forbidden** — it would break runner authority, the
  authenticated gating (U3), and the chat-filtering (U6).
- **`ask_user` toggle.** **ON** (auto-invoke `ask_user` on a pipeline `ask`; the
  orchestrator blocks and waits for the human). Hands-off is intentionally
  sacrificed for interactive convenience — documented so the tradeoff is explicit.
- **Per-stage answer storage.** The human's answer is stored on the **stage record**
  that asked (`stage.operatorResponse`), not a run-level map. Matches the
  one-question-at-a-time model (one uncertain stage per run).
- **Re-dispatch instance reuse.** Reuse the stage's `assignedInstance` **if its
  surface is still alive** (cheaper — no new spawn); otherwise reserve a fresh
  instance. Behavior is identical either way (Design B injects the same
  context), so this is purely a resource-cost choice.
- **One question at a time.** The runner already `break`s at the first `needs_attention`
  (one uncertain stage per run). Multi-question flows are just repeated
  `needs_attention` → answer → re-dispatch cycles reusing the stage id.

## Goal / Definition of Done

- A stage can report `needs_attention` with a structured `question` (+ optional `options`,
  + `summary` of work so far + any `artifacts`).
- The runner forwards this as a pipeline `ask` whose payload lets the orchestrator render it
  via `ask_user` (`message`/`options`/`context`).
- On a human answer, the answer is persisted in `pipelines.json` bound to the stage, and the
  runner **re-dispatches the same stage** with goal + summary + answer injected. The stage
  then sends a terminal `report`.
- `report` is still only emitted on finish/crash; mid-stage questions use `needs_attention`.
- The orchestrator can auto-`ask_user` (toggle) or surface manually (default).
- Crash mid-cycle reconciles: stored answer + persisted summary let a re-dispatch reconstruct
  context; no blind repeat of non-idempotent work.
- Tests cover: answer persistence + validation, single-question re-dispatch, two-question
  cycle, crash-and-resume reconciliation, `ask` payload shape, and the orchestrator
  `ask_user` wiring (payload correctness + toggle behavior).

## Components & changes

### 1. State model — `agency/scripts/pipeline_state.py`
- Add stage field `operatorResponse: str | None` (optional; validated as optional string in
  `_validate_stage`).
- Add `record_operator_response(root, pipeline_id, stage_id, response, *, lock_owner)`
  → sets `operatorResponse`, updates `updatedAt`. No status transition (answer is pending
  consumption by resume).
- Extend `classify_resume` / add `ResumeAction.RE_DISPATCH`: a `needs_attention` stage
  that has an `operatorResponse` *and* was `dispatched` (has `assignedInstance` +
  `dispatchedAt`) yields `RE_DISPATCH` instead of staying parked.
- Extend `reconcile_resume` to persist `RE_DISPATCH` intent (and never auto-transition to a
  terminal status).
- Optionally add `run.autoAsk: bool` (default `True`) recorded at run creation
  (`create_run`) for the toggle.

### 2. Report contract for `needs_attention` — `agency/scripts/pipeline_runner.py`
- `validate_stage_report`: when `status == "needs_attention"`, require/allow
  `question: str` (non-blank) and optional `options: list[str]`; keep `summary` and
  `artifacts` as the persisted work record. The runner stores these on the stage record
  (today only `error` is stored for `needs_attention`; broaden to hold `question`/
  `options`/`summary` so re-dispatch can reconstruct).
- Keep `error` as the machine-facing reason; `question` is the human-facing prompt.

### 3. Runner resume re-dispatch — `agency/scripts/pipeline_runner.py` (`_run_pipeline_locked`)
- In the `needs_attention` branch: if `resume` and the stage has `operatorResponse`
  (and was dispatched), **do not `break`** — instead build an augmented delegate payload
  (`build_delegate_payload` gains an `operator_response` + `prior_summary` arg) and
  re-dispatch the *same* stage id (reuse `assignedInstance` if still alive, else
  `reserve_stage_instance` again). Mark `operatorResponse` consumed so a re-dispatched
  stage that *again* reports `needs_attention` starts a new cycle (guards infinite loop).
- The re-dispatched task gets a *new* `taskId` (so auth/ownership stay exact) but the same
  stage id; `record_dispatched` updates the stage's `assignedInstance`/`dispatchedAt`/
  `taskId`.
- Normal `dispatched`-with-existing-report reconciliation is unchanged.

### 4. Ask payload enrichment — `agency/scripts/pipeline_runtime.py` (`_notify_attention`)
- Send `payload-json` = `{ message: question, options: [...], context: { summary, artifacts },
  synthesis: {...} }` so the orchestrator can map it to `ask_user` (question + options +
  context), not just a free-text note.

### 5. New CLI — `agency/scripts/agency_ctl.py`
- Add subparser **`pipeline-answer`** (orchestrator-only, `--require-caller`):
  `--pipeline-id`, `--stage` (stage id) or `--task-id`, `--answer` (json/text), and an
  optional `--resume` flag that, after persisting the answer, invokes
  `serve_pipeline_runner(..., resume=True)` (the existing re-dispatch path).
- Authority: same caller-surface binding as `run-pipeline` (orchestrator hub only).

### 6. Orchestrator-side `ask_user` wiring — `skills/agency-orchestrator/SKILL.md` (+ helper)
- Document: "On a pipeline `ask` (type `ask`, pipeline-owned), **automatically** call
  `ask_user` with `message`+`options`+`context` from the payload (autoAsk is ON), then on
  the reply run `agency_ctl pipeline-answer --pipeline-id … --stage … --answer '<reply>'
  [--resume]`."
- The actual `ask_user` call is the orchestrator *session's* action (it is a pi tool, not a
  subprocess) — so this is behavioral instruction + the mechanical store/resume command above.
- Note explicitly: with `autoAsk` ON the orchestrator intentionally blocks on the human
  (hands-off is sacrificed for interactivity); this is the locked tradeoff for U8.

### 7. Extension (only if needed) — `extensions/multi-agency/`
- `agency_delegate` already routes to `pipeline-runner` with `payloadJson`. No change needed
  for the stage re-dispatch (that is internal to the runner). Possibly expose a typed
  `agency_pipeline_answer` tool mirroring the `pipeline-answer` CLI for symmetry. **Optional.**

### 8. Tests
- `test_pipeline_state.py`: `record_operator_response`, `operatorResponse` validation,
  `RE_DISPATCH` classification.
- `test_pipeline_runner.py`: stage reports `needs_attention` w/ question+summary → resume
  with answer **re-dispatches same stage id** with augmented payload; two-question cycle
  (reports `needs_attention` twice, answered twice, then `succeeded`); infinite-loop guard
  (consumed answer not re-applied); crash mid-cycle reconciles via stored answer+summary.
- `test_pipeline_reporting.py`: `needs_attention` report requires `question`; `options`/
  `summary` carried.
- `test_pipeline_runtime.py`: `_notify_attention` payload shape (`message`/`options`/
  `context`).
- Orchestrator `ask_user` wiring: assert `ask` payload is renderable (structured fields
  present) + auto-ask behavior (autoAsk ON: orchestrator auto-invokes `ask_user`; covered
  via SKILL doc + a unit on payload shape).

## Open decisions (confirm before implementing)

~~(a) `operatorResponse` storage granularity: per-stage (chosen) vs a `run.operatorResponses`
  map. Per-stage is simpler and matches the one-question-at-a-time model.~~ **LOCKED: per-stage.**

~~(b) `autoAsk` default: off (chosen) to preserve hands-off. Confirm.~~ **LOCKED: ON**
  (auto-invoke `ask_user`; orchestrator blocks on the human; hands-off intentionally
  sacrificed for interactivity).

~~(c) Re-dispatch instance reuse: reuse `assignedInstance` if its surface is still alive
  (cheaper), else reserve fresh. Both feed the same injected context (Design B), so behavior
  is identical to the stage; only resource cost differs.~~ **LOCKED: reuse-if-alive-else-fresh.**

## Sequenced steps (suggested commit order)

1. `pipeline_state`: `operatorResponse` field + `record_operator_response` + `RE_DISPATCH`.
2. `pipeline_runner`: `needs_attention` report contract (`question`/`options`/`summary`) +
   resume re-dispatch with augmented payload + loop guard.
3. `pipeline_runtime`: `_notify_attention` payload enrichment.
4. `agency_ctl`: `pipeline-answer` command (store + optional resume), orchestrator-gated.
5. `skills/agency-orchestrator/SKILL.md`: `ask_user` wiring + `autoAsk` note.
6. Tests for all of the above; run `pytest agency/scripts/tests/` + `npx tsx --test
   extensions/multi-agency/test/*.test.ts`; `git diff --check`.

## Status (implemented 2026-07-14)

- [x] **U8.1** `db59e44` — state model: per-stage `operatorResponse`, `autoAsk` run flag,
  `needs_attention -> dispatched` re-dispatch transition, `RE_DISPATCH` resume action,
  `record_operator_response()` (lock-owner optional; operator-gated at CLI), version 3 -> 4.
- [x] **U8.2** `0bfc166` — `needs_attention` report contract (`question`/`options`/
  `summary`/`artifacts`) + enriched `ask` payload + loop breaks on fresh needs_attention.
- [x] **U8.3** `808c105` — resume re-dispatches the same stage with the operator answer
  injected (Design B); reuse-if-alive-else-fresh via `ControlPlane.surface_alive`.
- [x] **U8.4** `cbb9441` — `pipeline-answer` CLI (orchestrator-gated: store answer +
  optional `--resume`).
- [x] **U8.5** `a8b573e` — orchestrator SKILL human-in-the-loop wiring (`ask_user`
  auto-invoke on pipeline `ask`, then `pipeline-answer --resume`); test asserts enriched
  notify payload (question/options/context).
- [x] **U8.6** — full suite green: 247 pytest pass; 12/13 TS tests pass. The one
  failing TS test (`spawn.test.ts` tsx-resolution assertion) is a **pre-existing environment
  drift** (a global `tsx` now resolves where the test expected `null`), unrelated to U8
  (U8 never touched spawn or tsx resolution).

Locked decisions: (a) per-stage `operatorResponse`; (b) `autoAsk` ON (orchestrator
auto-invokes `ask_user`); (c) re-dispatch reuses the assigned instance if its surface is
alive, else reserves fresh. Behavior is identical either way (Design B injects the same
context).
