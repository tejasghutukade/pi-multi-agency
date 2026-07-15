---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
created: 2026-07-14
title: Declarative Multi-Agent Pipelines
---

# Declarative Multi-Agent Pipelines - Plan

## Goal Capsule

Let the orchestrator (or a user) run a **named, declarative pipeline** — e.g. `implementation: scout → planner → worker → coderev` — where stages advance **deterministically** via the existing spawn→delegate→wait control plane, with no LLM in the advancement loop. Pipeline definitions live in a per-project `pipelines.yaml` (multitenant). Each stage automatically receives artifact paths from the prior stages named in its `inputs` list, progress is durable and resumable, and the orchestrator stays hands-off during the run (it only sees the final synthesis).

**Product authority:** brainstorm session 2026-07-14 (settled decisions below).
**Open blockers:** none — the four review blockers were resolved by the user on 2026-07-14 and are recorded in KTD11–KTD14.

## Problem Frame

Today, multi-stage work (plan → implement → review) is chained only by the **orchestrator LLM's good grace**: it reads a pushed `report` and decides the next spawn (see `skills/agency-orchestrator/SKILL.md` "Typical sequence" table). That is soft, non-deterministic, and untestable. There is no declarative pipeline spec and no driver that advances stages on `report` without a model.

What exists and is reused: `agency_ctl.py` already has `spawn` / `delegate` / `wait`; `delegate` already carries `contextPaths` (artifact inheritance is free); `wait` already blocks on a `taskId`'s hub report; `recovery.py` already knows how to detect and respawn dead panes. The gap is purely a **deterministic driver + a declarative spec**.

## Requirements

- **R1** — Pipelines are defined declaratively in a per-project `pipelines.yaml` (multitenant: each project's `.pi/agency/` carries its own), not hardcoded in code or prose.
- **R2** — A pipeline run advances stages in order using the existing `spawn → delegate → wait` primitives; advancement is **deterministic and model-independent** (no LLM decides the next stage).
- **R3** — Every stage has a unique pipeline-local `id` and declares named `outputs`; `inputs` references earlier stage IDs and their declared outputs. The runner validates those references and passes resolved, project-contained paths via `contextPaths`.
- **R4** — Pipeline authority is bound to a live registered `pipeline-runner` surface and its active pipeline ID. A caller-supplied flag alone never bypasses the orchestrator surface gate.
- **R5** — Pipeline state is written atomically, one active run is enforced per project, and resume reconciles a previously dispatched task before considering any retry.
- **R6** — The lifecycle bridge suppresses only exact, state-owned intermediate `report` envelopes. Pipeline `ask` envelopes remain visible, and only the bound runner can emit the final completion report.
- **R7** — The orchestrator launches the runner through the existing `agency_spawn` / `agency_delegate` tools; users can start the same flow through a documented CLI command. No new native tool is required.
- **R8** — v1 supports **linear** stage lists and `onFailure: stop | continue`, with no automatic retries. Valid stage failures become `failed`; `stop` terminates, while `continue` marks required dependents `dependency_failed` and permits independent later stages. Timeout, dead pane, malformed report, or missing artifacts become `needs_attention`; per-stage branching / DAGs remain out of scope.

## Key Technical Decisions

- **KTD1 — Driver location: a `run-pipeline` loop inside `agency_ctl.py` (option A).** (session-settled: user-directed — chosen over B "hook inside lifecycle_bridge" and C "standalone daemon": A is least new code, reuses spawn/delegate/wait, and is portable to C later. B was rejected because it couples orchestration into the deliberately-thin delivery layer.)
- **KTD2 — Pipeline spec lives in its own file `pipelines.yaml`, not `agents.yaml`.** (session-settled: user-directed — chosen over embedding in agents.yaml: keeps role config and pipeline config separate and readable; satisfies multitenancy via per-project file.)
- **KTD3 — Runner is a spawnable `pipeline-runner` role launched via existing `agency_spawn` / `agency_delegate`; spawn authority via `--pipeline` bypass.** (session-settled: user-directed — chosen over a new native `agency_run_pipeline` tool (ii) and human-launched CLI (iii): reuses native tooling, no new tool to register. The runner pane is a **process pane** (not a pi/LLM pane) that execs the deterministic CLI loop, so no model is in the advancement path.)
- **KTD4 — Durable `pipelines.json` + `--resume`, reusing `recovery.py` stale-detection patterns.** (session-settled: user-directed — chosen over a `sessions.json` block: avoids mixing pipeline state into the pane roster; cleaner to test and to later hand to a daemon.)
- **KTD5 — Pipeline-owned taskId prefix (`pl-<pid>-s<n>`) + orchestrator ignore filter.** (session-settled: user-directed — chosen to kill the double-action hazard where the orchestrator LLM sees a stage report and re-spawns the next stage. The final completion report uses a non-prefixed taskId so the orchestrator still sees the outcome.)
- **KTD6 — Loop logic lives in a new `pipeline_runner.py` module; `agency_ctl.py` keeps a thin `cmd_run_pipeline` CLI entry.** Keeps `agency_ctl.py` from bloating and isolates the pure-Python driver for unit testing with a fake bus.
- **KTD7 — `pipeline-runner` uses a runner-specific process launch path with a code-owned fixed argv, not project-supplied generic `pane` / `command` configuration.** This keeps the loop model-independent without adding a general shell-command extension point.
- **KTD8 — Stage reports use a required result contract:** `status`, `summary`, and a named `artifacts` map (plus `error` on failure). Inputs select artifact names; no "first path wins" behavior exists.
- **KTD9 — Pipeline task IDs are identifiers, not credentials.** Authorization and bridge filtering both validate the registered runner surface, active pipeline ID, exact stage task ID, and expected sender from durable pipeline state.
- **KTD10 — v1 enforces a single active run per project and crash-safe state persistence.** A project lock prevents concurrent drivers; same-directory atomic replacement preserves the last valid state generation.
- **KTD11 — The fixed runner process waits for, atomically claims, and acknowledges its initial delegate before starting stages.** (session-settled: user-approved — chosen over direct spawn arguments and foreground CLI ownership: it preserves the existing spawn→delegate architecture without an LLM.)
- **KTD12 — v1 performs no automatic stage retries.** (session-settled: user-approved — chosen over opt-in idempotent retry and unconditional retry: uncertain side effects enter `needs_attention`; operators explicitly retry after assessment.)
- **KTD13 — Each pipeline uses a temporary runner.** (session-settled: user-approved — chosen over a persistent idle runner: terminal `done`/`failed` releases the runner and lock; `needs_attention` keeps it registered for inspect/resume/abandon.)
- **KTD14 — Pipeline state is implemented before and imported by the driver, never the reverse.** (session-settled: user-approved — chosen over driver-first stubs and a combined module: `pipeline_state.py` owns persistence/locking/transitions/queries; runner and bridge consume that API.)
- **KTD15 — Every stage declares its named outputs in `pipelines.yaml`.** (session-settled: user-approved — chosen over runtime-only selector validation and a single implicit artifact: U1 can reject unknown selectors before opening panes.)
- **KTD16 — The existing `agency_report` tool is extended backward-compatibly with optional structured result fields.** (session-settled: user-approved — chosen over JSON-in-prose and a second pipeline-report tool: pipeline tasks require `status`, `artifacts`, and optional `error`; ordinary reports retain `summary`/`output`.)
- **KTD17 — The existing `agency_spawn` tool accepts optional pipeline initialization for `pipeline-runner`.** (session-settled: user-approved — chosen over state creation during delegate and CLI-only initiation: spawn validates name/topic, allocates ID, locks and writes initial state, then opens the waiting process.)

**KTD4 clarification:** for v1, "reusing `recovery.py` stale-detection patterns" means pane/task liveness reconciliation. Time-based stale classification is deferred with the standalone supervisor daemon.

## Scope Boundaries

### Deferred to Follow-Up Work
- Standalone supervision **daemon** (option C) for concurrent/multi-pipeline orchestration. The `pipelines.json` state shape is designed to be daemon-portable.
- Per-stage **branching / DAG / conditional skips** beyond `onFailure: stop|continue`.
- Pipeline **cancellation** UI mid-run (a `SIGTERM`/teardown handler may land later).
- Web UI for pipeline authoring/monitoring.
- Auto-converting the orchestrator SKILL "Typical sequence" prose table into generated pipelines.

## High-Level Technical Design

**Start request (once per run):**
```
orchestrator tools OR user CLI
        │ validate pipeline + allocate pipeline_id + acquire project lock
        │ bind pipeline-runner session row to pipeline_id
        ▼
runner-specific cmux process pane ──▶ deterministic agency_ctl driver
```
The start command validates the pipeline, allocates the pipeline ID, acquires the project lock, and writes initial state before spawning a fixed `pipeline-runner serve --instance <name>` process. The process waits on its broker inbox. `agency_delegate` sends pipeline ID/name/topic; the process atomically claims the envelope, validates initial state, binds its session row to the pipeline ID, writes an acknowledgement, and only then starts stages.

**Stage loop (pure Python, deterministic):**
```
for stage in pipeline.stages:
    task_id = stable_task_id(pipeline_id, stage.id)
    persist(stage.status = "dispatched", task_id)  # atomic, before delegate
    spawn/reuse stage role with authenticated pipeline authority
    delegate(task_id, resolved_named_artifacts_as_contextPaths)
    result = wait_and_reconcile(task_id)
    validate sender + task_id + result contract + contained artifact paths
    persist(stage status, summary, named artifacts)
emit bound-runner final report with final outcome + all stage results/artifacts
```

**Stage result contract:**
```json
{
  "status": "succeeded|failed",
  "summary": "human-readable stage outcome",
  "artifacts": {"primary": "project/relative/path"},
  "error": "required when status=failed"
}
```
Artifact names are unique; paths must normalize inside the project/artifact root and exist before inheritance.

**Authority boundary:** privileged spawn/delegate/wait calls include the pipeline ID and pass only when `caller_surface()` resolves to a live `sessions.json` row with role `pipeline-runner`, the same active pipeline ID, and the expected cmux surface. Twin/max-pane/sole-writer policies remain enforced.

**Delivery boundary:** the `pl-` prefix remains a readable naming convention only. `lifecycle_bridge` suppresses an intermediate report only when durable state confirms the exact task ID, sender, active pipeline, and bound runner. `ask` envelopes are always delivered. Final completion is accepted only from the bound runner.

## Implementation Units

### U1. `pipelines.yaml` schema + validated loader

- **Goal:** Define the declarative pipeline and named-artifact input contract.
- **Requirements:** R1, R3, R8.
- **Dependencies:** none.
- **Files:** `agency/scripts/catalog.py` (add `load_pipelines` + validation), new per-project `.pi/agency/pipelines.yaml`, `agency/kit/pipelines.yaml` (template seeded by `agency_init --force`).
- **Approach:** Add `load_pipelines(root)` to `catalog.py` using the same YAML loading pattern as `load_agents`, then validate unique stage IDs, non-empty unique output names, known roles, prior-stage-only input references, selectors against the referenced stage's declared outputs, `onFailure`, and supported `{topic}` substitution. Schema:
  ```yaml
  pipelines:
    implementation:
      description: Plan then implement then review
      onFailure: stop
      stages:
        - id: scout
          role: scout
          goal: "Scout: {topic}"
          outputs: [primary]
          inputs: []
        - id: plan
          role: planner
          goal: "Plan: {topic}"
          outputs: [primary]
          inputs:
            - stage: scout
              artifacts: [primary]
        - id: implement
          role: worker
          goal: "Implement: {topic}"
          outputs: [primary]
          inputs:
            - stage: plan
              artifacts: [primary]
        - id: review
          role: coderev
          goal: "Review: {topic}"
          outputs: [primary]
          inputs:
            - stage: implement
              artifacts: [primary]
  ```
  Add the template to the `agency_init` copy list.
- **Patterns to follow:** `load_agents` (catalog.py:62) for loading and error framing; `role_of` (catalog.py:78) for role resolution.
- **Test scenarios:**
  - Happy path: the four-stage sample parses with stable IDs and named input selectors.
  - Missing file returns `{}`; malformed YAML and unsupported keys fail clearly.
  - Duplicate stage IDs, empty/duplicate/invalid output names, duplicate selectors, unknown/forward stage references, selectors for undeclared outputs, unknown roles, and invalid `onFailure` fail before any pane is opened.
  - Two project fixtures load only their own pipelines.
- **Verification:** `python3 -m pytest agency/scripts/tests/test_catalog.py -k pipeline` passes.

### U2. Runner-specific process launch

- **Goal:** Add the spawnable `pipeline-runner` role without creating a generic shell-command extension point.
- **Requirements:** R4, R7, KTD3, KTD7.
- **Dependencies:** U1, U5, U3.
- **Files:** `agency/agents.yaml` (register `pipeline-runner` identity only), `agency/scripts/agent_spawn.py` (runner-specific process branch), `agency/scripts/agency_ctl.py` (fixed runner entrypoint).
- **Approach:** Register the role as temporary. Recognize `role == "pipeline-runner"` in code and launch fixed argv `pipeline-runner serve --instance <name>`; do not read process commands or arbitrary placeholders from project YAML. The process waits for a broker delegate, atomically claims it, validates pipeline ID/name/topic against initial state, binds `activePipelineId`, acknowledges the claim, and starts U4. Terminal `done`/`failed` releases the lock and tears down the runner; `needs_attention` retains it until resume or explicit abandon.
- **Patterns to follow:** current `open_pane` usage in `agent_spawn.py`; existing role registration in `agency/agents.yaml`.
- **Test scenarios:**
  - Runner role selects the process path and never boots pi.
  - Every existing specialist role still boots pi unchanged.
  - Project config containing `pane`, `command`, shell metacharacters, or runner command overrides is rejected/ignored and never reaches a shell.
  - The generated launch uses argv-safe quoting and rejects malformed pipeline IDs.
- **Verification:** existing spawn tests remain green; new tests prove no project-controlled shell command is executed.

### U3. Authenticated pipeline authority

- **Goal:** Allow only the bound runner process to perform pipeline-owned spawn/delegate/wait operations.
- **Requirements:** R4, KTD3, KTD9.
- **Dependencies:** none (enables U2/U4/U6).
- **Files:** `agency/scripts/agency_ctl.py` (add `require_pipeline_runner_authority` and `--pipeline-id` plumbing), `agency/scripts/agent_spawn.py` (thread pipeline ID), session-ledger helpers used to resolve caller surface.
- **Approach:** Replace the proposed boolean bypass with a pipeline-ID-bearing authorization check. `require_pipeline_runner_authority(root, pipeline_id)` resolves `caller_surface()` and requires a live session row whose role is `pipeline-runner`, `activePipelineId` equals the argument, and cmux surface matches the caller. Only then may the normal orchestrator surface check be relaxed; max panes, role twins, sole-writer, and routing rules still apply. Missing, stale, unregistered, wrong-role, or mismatched-pipeline callers fail closed.
- **Patterns to follow:** `ensure_orchestrator` surface matching and `recovery=True` early return, while adding the runner-specific identity checks that recovery lacks.
- **Test scenarios:**
  - Bound live runner with matching pipeline ID succeeds.
  - Specialist, unregistered surface, stale runner, dead pane, wrong pipeline ID, and caller-supplied flag without a bound row all fail.
  - Authorized calls still enforce worker sole-writer and max-pane rules.
- **Verification:** unit tests cover every allow/deny branch; manual runner call succeeds without weakening unrelated surfaces.

### U4. Deterministic driver + stage-result protocol

- **Goal:** Advance validated stages through spawn→delegate→wait using stable task IDs and a required result contract.
- **Requirements:** R2, R3, R5, R6, R8, KTD1, KTD6, KTD8, KTD12, KTD14.
- **Dependencies:** U1, U5, U3, U2.
- **Files:** `agency/scripts/pipeline_runner.py` (new), `agency/scripts/agency_ctl.py` (thin command entry), `extensions/multi-agency/index.ts` + extension tests (backward-compatible `agency_report` fields), stage-report validation helpers/tests.
- **Approach:** Extend `agency_report` with optional `status`, `artifacts`, and `error`; require them only when the task ID belongs to an active pipeline, while ordinary reports retain `summary`/`output`. For each stage, resolve named inputs, persist `dispatched` with a stable task ID before delegation, spawn/reuse under authenticated authority, delegate, and wait/reconcile. Accept only a report from the expected stage instance with matching pipeline/stage/task identity and payload `{status, summary, artifacts, error?}`. Validate returned names against that stage's declared outputs plus path existence/containment before saving or forwarding. The final bound-runner report carries overall status, final-stage summary, every stage outcome, and accumulated named artifact paths—not merely a completion string.
- **Failure policy:** No automatic retry. A valid failed report records `failed`; `onFailure: stop` terminates the run, while `continue` marks stages with failed required inputs `dependency_failed` and allows later independent stages. Timeout, dead pane, malformed report, or missing required artifacts record `needs_attention`. Resume reconciles existing state/report only; an explicit operator retry is a new attempt after side-effect assessment.
- **Patterns to follow:** `cmd_delegate` for envelopes; `cmd_wait` for broker blocking and pane-dead detection.
- **Test scenarios:**
  - Four-stage happy path advances in order and passes only selected named artifacts.
  - Missing/duplicate artifact names, absolute paths, `..` escapes, nonexistent paths, wrong sender/task ID, and malformed payload are rejected.
  - Final report contains overall status, final summary, all stage statuses, and all accumulated artifacts.
  - `stop`, `continue`, dependency failure, timeout, pane death, malformed report, missing artifact, resume reconciliation, and explicit retry cover every state transition; no path automatically redelegates.
- **Verification:** `test_pipeline_runner.py` uses a fake bus and asserts ordering, envelope identity, artifact containment, final synthesis, and the resolved failure matrix.

### U5. Crash-safe state, exclusive lock, and resume reconciliation

- **Goal:** Make pipeline progress durable without blind duplicate execution.
- **Requirements:** R5, KTD4, KTD10, KTD12, KTD14.
- **Dependencies:** U1. This unit lands before U3/U2/U4; it imports neither the runner nor CLI.
- **Files:** `agency/scripts/pipeline_state.py` (new: lock, validated load, atomic save, reconciliation helpers), `agency/scripts/agency_ctl.py` (`--resume` / pipeline-ID plumbing).
- **Approach:** Acquire a per-project exclusive lock before creating or resuming a run; a second launch fails with the active pipeline ID. Persist stage records with stable IDs and statuses `pending|dispatched|succeeded|failed|needs_attention`. Save `dispatched` + task ID atomically before delegate. On resume, skip succeeded stages; for dispatched stages first search for an existing report with the same task ID and reconcile pane/session state. Never blindly redelegate an uncertain non-idempotent stage: mark `needs_attention` unless D2 explicitly authorizes a safe retry.

  Save by writing and validating a same-directory temporary file, flushing/fsyncing it, atomically replacing `pipelines.json`, and fsyncing the directory; retain the previous valid generation for recovery from corruption. Lock recovery is based on verified owner process/surface liveness, not elapsed time. No automatic stale-time heuristic ships in v1.
- **State fields:** pipeline name/topic/status, active runner instance/surface, current stage ID, and per-stage role/task ID/status/summary/named artifacts/error/timestamps.
- **Patterns to follow:** session JSON shape and token generation, while adding atomic persistence rather than copying in-place writes.
- **Test scenarios:**
  - Second concurrent start fails and identifies the active pipeline.
  - Crash before delegate leaves `dispatched`; resume consumes an existing report without redelegating.
  - Crash with uncertain side effects and no report produces `needs_attention`, not an automatic retry.
  - Simulated interrupted write preserves a readable prior generation; unknown/corrupt state fails safely.
  - Unknown resume ID and mismatched lock ownership fail clearly.
- **Verification:** `test_pipeline_state.py` exercises locking, atomic replacement, corruption recovery, and every resume branch.

### U6. State-owned report filtering

- **Goal:** Keep the orchestrator hands-off without trusting a spoofable task-ID prefix or hiding asks.
- **Requirements:** R6, KTD5, KTD9.
- **Dependencies:** U4 and U5.
- **Files:** `agency/scripts/lifecycle_bridge.py`, `agency/scripts/hub_delivery.py` (shared ownership predicate before claim/push), pipeline-state lookup helpers, `skills/agency-orchestrator/SKILL.md`.
- **Approach:** For `report` envelopes only, suppress delivery when active pipeline state matches the exact pipeline ID, stage task ID, expected sender instance, and bound runner ownership. Treat `pl-` as naming only. Always deliver `ask` envelopes. Accept a final completion report only from the bound runner instance for that pipeline; unrelated prefixed reports and forged final IDs remain visible/rejected rather than silently trusted.
- **Patterns to follow:** `hub_inbox_envelopes` type filtering plus durable ownership lookup.
- **Test scenarios:**
  - Expected intermediate report is consumed by the runner and not delivered to orchestrator chat.
  - Pipeline asks are delivered.
  - Wrong sender, unrelated `pl-` task, forged final ID, stale pipeline, and wrong runner do not pass ownership checks.
  - Exactly one valid final report reaches the orchestrator.
- **Verification:** bridge tests cover every identity tuple and a live integration shows intermediate reports hidden, asks visible, and final synthesis delivered.

### U7. Orchestrator and user invocation, examples, and integration tests

- **Goal:** Make the same pipeline flow discoverable from both the orchestrator and the CLI.
- **Requirements:** R1, R6, R7, R8.
- **Dependencies:** U1 → U5 → U3 → U2 → U4 → U6.
- **Files:** `skills/agency-orchestrator/SKILL.md`, `agency/scripts/agency_ctl.py` (documented user start command), `extensions/multi-agency/index.ts` + extension tests (optional pipeline initialization on `agency_spawn`), `agency/pipelines.yaml`, pipeline integration tests, `docs/architecture.md`.
- **Approach:** Extend existing `agency_spawn` with optional `{name, topic}` pipeline initialization valid only for role `pipeline-runner`; it validates config, allocates the ID, acquires the lock, writes initial state, opens the waiting process, and returns the ID. The orchestrator then sends the matching assignment with `agency_delegate` and requires claim acknowledgement. Add `agency_ctl run-pipeline --name <pipeline> --topic <text>` as the user entry into the same sequence—no alternate execution semantics and no new native tool. Seed the validated four-stage example. Document final synthesis, `needs_attention`, explicit retry, resume, and abandon flows.
- **Test scenarios:**
  - Orchestrator-triggered and user-triggered runs produce the same state/envelopes for equivalent input.
  - Four-stage integration inherits selected named artifacts and emits one complete final synthesis.
  - Unknown pipeline, invalid config, second active run, and `needs_attention` produce clear user-facing errors/status.
  - `agency_init --force` seeds a valid example.
- **Verification:** full `pytest agency/scripts/tests/` green plus one cmux integration run from each entry path.

## Definition of Done

- KTD11–KTD14 are implemented exactly: delegate-claim handshake, no automatic retry, temporary runner lifecycle, and state-first one-way dependencies.
- `pipelines.yaml` validates unique stage IDs, declared outputs, named prior-stage artifact selectors, known roles, and failure policy before opening panes.
- Orchestrator and user CLI entry paths create the same deterministic run; no LLM decides stage advancement.
- Runner authority is bound to a live registered surface + active pipeline ID; unauthenticated callers and stale/mismatched runners fail closed.
- No project-controlled shell command reaches process launch; the runner uses a code-owned fixed argv.
- The backward-compatible `agency_report` extension enforces status/summary/declared-artifact results for pipeline tasks; inherited paths exist and remain inside the allowed root.
- `pipelines.json` is atomically persisted, prior valid state survives interrupted writes, and a per-project lock enforces one active run.
- Resume reconciles dispatched tasks and never blindly repeats uncertain non-idempotent work.
- Only state-owned intermediate reports are filtered; asks remain visible; exactly one bound-runner final synthesis is delivered.
- Tests cover loader validation, authority denials, command injection, driver state transitions, report identity/schema, path containment, locking, atomic recovery, resume reconciliation, filtering, both entry paths, and init seeding; full suite is green.

## Assumptions

- Project-local `agents.yaml` and `pipelines.yaml` are operator-controlled configuration for v1. Supporting pipelines from untrusted cloned repositories requires a separate prompt/config trust review.
- Concurrent pipeline supervision, automatic stale-time discovery, automatic retries, and cancellation UI remain deferred; v1 enforces one active run and explicit operator recovery.
