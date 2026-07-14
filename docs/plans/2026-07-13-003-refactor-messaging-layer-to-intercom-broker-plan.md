---
title: "refactor: Messaging layer onto pi-intercom-style broker"
date: 2026-07-13
type: refactor
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# refactor: Messaging layer onto pi-intercom-style broker

## Goal Capsule

- **Objective:** Replace the current file-polling bus as the primary live transport with a pi-intercom-style local broker: persistent session connections, length-prefixed JSON frames, delivered/failed acknowledgements, presence, and ask/reply correlation.
- **Product authority:** User request to rework the messaging layer using the core approach from `nicobailon/pi-intercom`; current Multi-Agency architecture remains hub-led, role-scoped, cmux-spawned, and tool-facing through `agency_*` APIs.
- **Stop:** Stop when delegates, reports, asks, replies, progress, and release messages flow through the broker in normal operation; the old filesystem bus is demoted to compatibility/audit/fallback; tests prove delivery, failure, reconnection, ask/reply, and legacy fallback behavior.

## Product Contract

### Summary

The current bus stores JSON envelopes under `.pi/agency/inbox/<instance>/{pending,processing,done}` and relies on lifecycle polling to claim and inject messages. This is durable, but it creates race/stranding cases: a message can move to `processing` before the Pi follow-up is actually delivered, and normal queue counts no longer surface it if the push/ack fails.

`pi-intercom` solves a related problem with a small local broker. Each Pi session keeps a connected client socket. The broker knows connected sessions, routes targeted messages by session id/name, sends immediate `delivered` or `delivery_failed` acknowledgements, tracks pending ask edges, and uses extension lifecycle code to trigger or queue inbound messages in the recipient session.

We will adopt that core model for Multi-Agency: broker for live delivery, ledger for process/task truth, and a compact journal/file fallback for recovery/audit.

### External source evidence

- `pi-intercom` uses a local broker with in-memory sessions and ask edges: `IntercomBroker` stores `sessions` and `askEdges` in maps ([broker.ts#L132-L139](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/broker.ts#L132-L139)).
- The broker listens on a local IPC/TCP target, writes PID/endpoint state, and restricts runtime files ([broker.ts#L153-L180](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/broker.ts#L153-L180)).
- Messages are length-prefixed frames: 4-byte big-endian length plus JSON payload; the reader handles partial reads and oversized frames ([framing.ts#L1-L67](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/framing.ts#L1-L67)).
- Broker startup is guarded by health checks and a spawn lock, preventing multiple brokers racing at startup ([spawn.ts#L179-L240](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/spawn.ts#L179-L240), [spawn.ts#L315-L382](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/spawn.ts#L315-L382)).
- `send` validates target, ask/reply edges, mutual ask deadlocks, then writes directly to the target socket and returns `delivered` to the sender ([broker.ts#L413-L482](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/broker.ts#L413-L482)).
- Ask edges are pruned and removed when sessions disconnect or replies arrive ([broker.ts#L565-L579](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/broker.ts#L565-L579)).
- The client tracks pending sends/lists and fails them on disconnect ([client.ts#L119-L135](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/client.ts#L119-L135)); `send()` resolves with delivered/failed or times out ([client.ts#L504-L549](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/broker/client.ts#L504-L549)).
- Inbound messages are delivered through Pi as trigger turns or follow-ups; busy UI sessions queue until idle, while busy non-interactive sessions get a best-effort auto-reply ([index.ts#L647-L766](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/index.ts#L647-L766)).
- `ask` waits for a correlated reply and cancels the ask on abort/timeout ([index.ts#L1570-L1658](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/index.ts#L1570-L1658)); `reply` resolves the right pending ask using local reply tracking ([index.ts#L1676-L1716](https://github.com/nicobailon/pi-intercom/blob/e234a4446e2b3f9c13a1ec3151ae2169315c810f/index.ts#L1676-L1716)).

### Current local constraints

- `agency/scripts/bus.py` currently owns envelope creation, ACL checks, `pending -> processing -> done`, legacy `wait`, and cmux notify.
- `agency/scripts/hub_delivery.py` and `agency/scripts/specialist_delivery.py` currently claim file envelopes and format follow-up text for lifecycle delivery.
- `extensions/multi-agency/lifecycle.ts` currently polls hub/specialist inboxes and calls Python lifecycle commands to claim/ack messages.
- `agency/scripts/ledger.py` / `sessions.json` remain the source of process/task truth; this plan does not replace spawn policy or role constraints.

### Requirements

- R1. Normal live delivery uses a local broker socket, not pending-file polling.
- R2. Every agency Pi pane registers with stable identity: `instanceName`, role, lifecycle class, cwd, pid, model/status, and current `taskId` when known.
- R3. Sender gets synchronous delivery status: delivered, target-not-connected, ambiguous target, ACL denied, broker unavailable, timeout.
- R4. Existing agency message types remain: `delegate`, `report`, `ask`, `reply`, `progress`, `release`.
- R5. `ask`/`reply` correlation is explicit and enforced: replies must match a pending ask edge; mutual blocking asks are refused or routed through hub policy.
- R6. Specialist-facing work must not depend on shell polling. Inbound `delegate`/`reply` should appear as Pi follow-up/trigger messages through the extension.
- R7. Specialist outbound `report`/`ask`/`progress` must use extension-owned identity, not an ephemeral CLI registration that can steal the pane's broker session.
- R8. File bus remains as compatibility/audit/fallback during migration, but it is no longer the primary source for live message delivery.
- R9. A failed live send does not silently mark work as delegated or completed. Ledger transitions happen only after broker delivery succeeds or a deliberate fallback spool is written.
- R10. Role ACLs still come from `agency/agents.yaml`/catalog. Phase-1 hub-only routing remains unless peer mode is explicitly enabled.
- R11. The implementation includes tests ported/adapted from `pi-intercom` for framing, paths, spawn lock, client/broker send, ask/reply, failure, and reconnection.
- R12. MIT attribution for copied/adapted `pi-intercom` code is preserved in `NOTICE` and/or a local source header.

### Acceptance Examples

- AE1 (R1, R3). `agency_delegate` to a connected specialist returns delivered and the specialist receives one triggered/follow-up message without any file appearing in `pending/`.
- AE2 (R3, R9). `agency_delegate` to a non-connected specialist returns target-not-connected and does not mark the specialist `working` unless fallback spool is explicitly chosen.
- AE3 (R5). A specialist `ask` blocks/waits for an orchestrator reply; the reply contains `replyTo`, resolves the ask, and clears the ask edge.
- AE4 (R5). A reply with the wrong `replyTo` is rejected with a delivery failure rather than becoming an uncorrelated chat message.
- AE5 (R6). When orchestrator is busy, report delivery queues in the extension and appears after settle; no claimed file can get stranded in `processing/`.
- AE6 (R7). A specialist report uses the specialist pane's connected broker client; no subprocess registers the same session id and disconnects the real pane.
- AE7 (R8). Legacy `$BUS send` can still write an audit/fallback envelope, and a compatibility drain can replay it through the broker.
- AE8 (R11). Broker tests cover fragmented frames, oversized frames, ambiguous names, disconnected targets, ask timeout/cancel, and mutual ask refusal.

### Scope Boundaries

#### In scope

- Vendored/adapted broker core under the Multi-Agency extension/package.
- New agency message schema mapped onto broker messages.
- Extension-level client lifecycle in every agency pane.
- Orchestrator and specialist delivery changes.
- Specialist outbound tool surface for `report`, `ask`, `progress`, and possibly `done/release` acknowledgements.
- Compatibility layer for old `bus.py` and existing scripts while charters are updated.
- Tests and docs updates.

#### Deferred for later

- Cross-machine transport.
- Encrypted payloads.
- Peer specialist-to-specialist messaging beyond existing catalog policy.
- Full removal of file bus directories and legacy `agency_wait`.
- Replacing `sessions.json` with broker presence as durable process truth.

#### Outside this product's identity

- A general-purpose chat UI; this is an agency control transport, not a user-facing intercom clone.
- Depending on the separate `pi-intercom` extension being installed by users. We can reuse/adapt its MIT core, but Multi-Agency must remain self-contained.

## Key Technical Decisions

- KTD1. **Broker primary, journal fallback.** Broker delivery is the normal transport; files become audit/fallback rather than source-of-truth delivery queues.
- KTD2. **Extension-owned identity.** Only the running Pi extension client owns a broker session id. Shell/Python compatibility commands must not register as the same specialist.
- KTD3. **Add specialist agency tools.** Replace report-by-`$BUS send` with explicit extension tools such as `agency_report`, `agency_ask`, and `agency_progress`, scoped by role/charter.
- KTD4. **Keep Python for spawn/ledger initially.** Do not rewrite the control plane in TypeScript. Python remains responsible for sessions, spawn, policy, and recovery; TypeScript owns live socket delivery.
- KTD5. **Use pi-intercom framing/spawn/client patterns directly.** Start from length-prefixed frames, local runtime dir permissions, broker health checks, spawn lock, pending send maps, ask edge map, and inbound idle queueing.
- KTD6. **No processing-stage live messages.** The broker never has `pending/processing/done`; delivery either reaches a connected recipient socket and returns delivered, or fails. If fallback is used, it is explicit and observable.
- KTD7. **Ledger transitions after send result.** `delegate` marks a specialist working only after delivered or after fallback spool is accepted. `report` clears task only after orchestrator receives/acks the broker message.
- KTD8. **Compatibility bridge is temporary.** `bus.py` remains but should call/journal through the new layer where safe. New charters should teach tools, not shell polling.

## High-Level Design

### New modules

- `extensions/multi-agency/broker/framing.ts` — adapted length-prefixed JSON frame reader/writer.
- `extensions/multi-agency/broker/paths.ts` — agency runtime socket/PID/lock paths under `.pi/agency/runtime/` or `~/.pi/agent/agency-broker/` with restrictive permissions.
- `extensions/multi-agency/broker/spawn.ts` — auto-start broker with health check and lock.
- `extensions/multi-agency/broker/broker.ts` — local agency broker with sessions, message routing, ACL hook, ask edges, presence, and delivery status.
- `extensions/multi-agency/broker/client.ts` — extension client with pending sends, pending asks, reconnect, and presence update.
- `extensions/multi-agency/messages.ts` — agency message schema and mapping to/from broker frames.
- `extensions/multi-agency/agency-tools.ts` — specialist-safe tools: `agency_report`, `agency_ask`, `agency_progress`; orchestrator-safe reply/delegate uses existing tools.
- `agency/scripts/bus_compat.py` or refactored `agency/scripts/bus.py` — legacy shell/file compatibility and fallback spool.

### Message shape

Broker frame payload should carry:

```ts
type AgencyMessage = {
  id: string;
  kind: "delegate" | "report" | "ask" | "reply" | "progress" | "release";
  from: string;
  to: string;
  taskId?: string;
  workflowId?: string;
  correlationId?: string;
  replyTo?: string;
  expectsReply?: boolean;
  createdAt: number;
  payload?: unknown;
  payloadPath?: string;
};
```

The broker transport can wrap this in a generic `send` frame, but the agency schema should remain explicit so tests and logs can validate task semantics.

### Runtime flow

1. **Session start:** extension resolves instance identity from Python lifecycle `whoami`, connects to broker, registers presence.
2. **Delegate:** `agency_delegate` updates intended ledger state, sends broker `delegate`, waits for delivered/failed, then commits working state or fallback state.
3. **Inbound specialist:** specialist extension receives broker `delegate`, injects `pi.sendUserMessage(...)`, and agent acts immediately. No polling.
4. **Report/progress:** specialist calls `agency_report` / `agency_progress`; extension sends broker message as the connected specialist identity.
5. **Ask:** specialist calls `agency_ask`; extension sends with `expectsReply`, waits for broker-correlated `reply`, and returns answer in the same tool call.
6. **Hub inbound:** orchestrator extension receives `report`/`ask`, queues if busy, injects when idle, and acks/updates ledger after delivery.
7. **Fallback:** if broker unavailable or target missing, optional compatibility spool writes an envelope under `.pi/agency/fallback/` and surfaces an operator-visible status; no silent success.

## Implementation Units

### U1. Vendor/adapt broker core

- Files: `extensions/multi-agency/broker/framing.ts`, `paths.ts`, `spawn.ts`, `broker.ts`, `client.ts`, `types.ts`, `NOTICE`.
- Work:
  - Copy/adapt pi-intercom core with MIT attribution.
  - Rename protocol to `multi-agency-broker` and use agency runtime paths.
  - Keep 4-byte big-endian frame protocol and max-frame guard.
  - Keep spawn lock and broker health check.
- Tests:
  - Port `broker/framing.test.ts`, `broker/paths.test.ts`, `broker/spawn.test.ts` equivalents.

### U2. Define agency message schema and ACL hooks

- Files: `extensions/multi-agency/messages.ts`, `agency/agents.yaml`, `agency/scripts/catalog.py` if needed.
- Work:
  - Define `AgencyMessage` and validation.
  - Add broker-side ACL callback/loader from current catalog policy or a snapshot generated by Python.
  - Preserve hub-only default.
- Tests:
  - Valid/invalid message shapes.
  - ACL allow hub edges, deny peer edges by default, allow configured peer mode.

### U3. Extension client lifecycle registration

- Files: `extensions/multi-agency/lifecycle.ts`, `extensions/multi-agency/index.ts`, new broker client module.
- Work:
  - On `session_start`, `whoami`, connect/register with stable instance identity.
  - On `agent_start`/`agent_settled`, update broker presence/status.
  - Add reconnect with backoff and status banner.
- Tests:
  - Unit test lifecycle registration with fake client and fake `runCtl`.
  - Reconnect keeps identity and does not duplicate sessions.

### U4. Orchestrator delegate/reply over broker

- Files: `extensions/multi-agency/index.ts`, `agency/scripts/agency_ctl.py`, possibly `agency/scripts/ledger.py`.
- Work:
  - Change `agency_delegate` path: ledger preflight -> broker send -> ledger commit.
  - Keep `agency_ctl delegate` compatibility, but mark it fallback/legacy or have it call a spool path.
  - Add explicit delivered/failed result to tool output.
- Tests:
  - Connected target delivered and ledger working.
  - Missing target returns failed and ledger unchanged.
  - Fallback spool path is visible and does not pretend live delivery succeeded.

### U5. Specialist inbound delivery without polling

- Files: `extensions/multi-agency/lifecycle.ts`, remove/soft-disable `agency/scripts/specialist_delivery.py` usage.
- Work:
  - Broker `delegate`/`reply` inbound messages inject follow-up/trigger directly.
  - Remove `claim-specialist` polling from normal lifecycle loop.
  - Keep fallback drain only for compatibility files.
- Tests:
  - Delegate renders once.
  - Busy specialist queues or follows current desired policy.
  - Duplicate/reconnect does not replay already delivered live message unless fallback replay is requested.

### U6. Specialist outbound tools

- Files: `extensions/multi-agency/index.ts`, new `agency-tools.ts`, `agents/*.md`, `agency/charters/*.md`, `skills/*/SKILL.md` where they instruct `$BUS` usage.
- Work:
  - Add role-scoped `agency_report`, `agency_ask`, `agency_progress` tools.
  - Tools use connected broker client identity; no subprocess registration.
  - Update charters: specialists report/ask through tools, not `python3 "$BUS" send`.
- Tests:
  - Report sends as correct instance and reaches orchestrator.
  - Ask waits for reply and handles timeout/cancel.
  - Tool unavailable/unauthorized in hub-only contexts where inappropriate.

### U7. Hub inbound delivery and ask/reply

- Files: `extensions/multi-agency/lifecycle.ts`, `agency/scripts/hub_delivery.py` compatibility only.
- Work:
  - Broker inbound `report`/`ask` queues in memory while hub busy and injects after settle.
  - Report ack clears task fields in ledger after hub receives/injects.
  - Ask creates a pending reply context; `agency_reply` or existing response flow sends broker `reply`.
- Tests:
  - Busy hub queue drains after settle.
  - Report clears task fields.
  - Ask keeps task open until reply/report.

### U8. Compatibility/fallback bus

- Files: `agency/scripts/bus.py`, `agency/bus-spec.md`, `README.md`.
- Work:
  - Rewrite docs: broker primary, filesystem fallback/audit.
  - Keep legacy `send/recv/wait/done/list/init` but label as compatibility.
  - Add fallback drain command/tool for stranded file envelopes.
  - Quarantine malformed fallback envelopes instead of silently skipping.
- Tests:
  - Legacy commands still pass old tests where promised.
  - Fallback drain replays to connected broker target.
  - Malformed envelope moves to quarantine/diagnostic marker.

### U9. End-to-end smoke and migration cleanup

- Files: tests/docs as needed.
- Work:
  - Add a local broker integration test with two fake clients and agency message payloads.
  - Run existing Python control-plane tests.
  - Remove normal lifecycle polling timers for hub/specialist file inboxes.
  - Update generated explainer/docs if needed.
- Tests:
  - `npm`/`tsx` broker tests.
  - `python3 -m pytest -q -p no:cacheprovider agency/scripts/tests`.
  - Manual cmux/Pi smoke: spawn scout -> delegate -> report -> hub receive; specialist ask -> hub reply.

## Risks and Mitigations

- **Risk: broker is live/in-memory, current file bus is durable.** Mitigation: keep explicit fallback/journal and make delivery failures visible; do not silently discard or mark work done.
- **Risk: CLI subprocess identity conflict.** Mitigation: outbound live sends must be extension-owned tools, not `bus.py` registering as the same session.
- **Risk: larger TypeScript surface in a Python-heavy control plane.** Mitigation: keep Python for spawn/ledger/recovery; TypeScript owns only live session transport already adjacent to Pi lifecycle.
- **Risk: hidden dependency on separate `pi-intercom`.** Mitigation: vendor/adapt MIT core into this package; do not require users to install `pi-intercom` separately.
- **Risk: migration breaks existing specialist prompts.** Mitigation: ship compatibility bus for one release and update charters in same change as new tools.

## Validation Plan

1. Port/adapt `pi-intercom` broker unit tests.
2. Add Multi-Agency broker schema/ACL tests.
3. Keep existing `agency/scripts/tests/test_bus.py`, `test_hub_delivery.py`, `test_specialist_delivery.py`, `test_recovery.py`, `test_agency_ctl_parity.py` green until their behavior is explicitly deprecated.
4. Add integration test: two clients register, delegate delivered, report delivered, ask/reply resolved, missing target failed.
5. Manual smoke in cmux with real Pi panes before removing legacy polling.

## Open Questions

- Should fallback spool live under current `.pi/agency/inbox/` for operator familiarity, or a new `.pi/agency/fallback/` to avoid implying it is primary?
- Should `agency_ask` be one tool that blocks until reply, or should it split into `agency_ask` + `agency_check_reply` for long-running specialists?
- Should orchestrator replies use a new explicit `agency_reply` tool, or should inbound ask text continue to instruct the hub to call existing agency tooling?
