# Agency Message Transport Spec (v1)

Live agency traffic is broker-primary: each Pi pane registers with the Multi-Agency broker and sends `delegate`, `report`, `ask`, `reply`, `progress`, and `release` messages over a local socket with delivered/failed acknowledgement. Filesystem envelopes remain as compatibility, fallback, and audit. Specialists should use `agency_report`, `agency_ask`, and `agency_progress`; `$BUS` polling is only for legacy fallback files.

## Layout

```text
.pi/agency/
  inbox/
    orchestrator/pending/     # specialists → hub
    orchestrator/processing/
    orchestrator/done/
    <instanceName>/pending/   # e.g. scout-t3f2, plan
    <instanceName>/processing/
    <instanceName>/done/
  outbox/                     # optional copies of sent envelopes (audit)
  artifacts/<taskId>/         # large payloads referenced by path
  sessions.json
  bus-spec.md                 # this file
```

- Inbox folder name = **instance name** from `sessions.json` (`plan`, `scout-t8785`, `orchestrator`).
- Always write new messages into `pending/` using atomic create (`*.tmp` → rename to `*.json`).
- Receiver moves `pending → processing` when claimed, then `processing → done` when handled (or delete after archive to outbox). This state machine applies to fallback files only; live broker messages are delivered directly and do not enter `processing/`.

## Scripts location

`bus.py` / `memory.py` / `agency_ctl.py` live in the **installed multi-agency package** (`…/agency/scripts/`), **not** under `.pi/agency/scripts/`. Project `.pi/agency/` holds state (inbox, sessions, charters copy). Boot prompts pass absolute package script paths — use `$BUS` and `$MEMORY`.

```bash
export AGENCY_ROOT="$PWD/.pi/agency"
export AGENCY_PROJECT_ROOT="$PWD"
export BUS="/path/to/multi-agency/agency/scripts/bus.py"
export MEMORY="/path/to/multi-agency/agency/scripts/memory.py"
# or: absolute paths printed in specialist boot / /agency-hub
# fallback only, after broker delivery fails or no live delegate appears
python3 "$BUS" recv --as <instanceName> --wait 60 --interval 2
```

## Envelope schema

File name: `{createdAtCompact}-{shortId}-{type}.json`  
Example: `20260712T165630Z-a1b2-delegate.json`

```json
{
  "schemaVersion": 1,
  "id": "a1b2c3d4",
  "type": "delegate",
  "from": "orchestrator",
  "to": "scout-t3f2",
  "taskId": "auth-explore-scout-1",
  "workflowId": "auth-explore-1",
  "correlationId": null,
  "replyToId": null,
  "createdAt": "2026-07-12T16:56:30Z",
  "ttlSec": 3600,
  "priority": "normal",
  "aclChecked": true,
  "notify": {
    "title": "scout",
    "body": "new delegate",
    "cmux": true
  },
  "payload": {
    "goal": "…",
    "contextPaths": ["…"],
    "successCriteria": "…",
    "constraints": ["hub-only", "read-only"],
    "charterPath": ".pi/agency/charters/scout.md",
    "skillPath": null,
    "outputShape": "see charter",
    "stopRules": "ask orchestrator if blocked"
  },
  "payloadPath": null
}
```

| Field | Rules |
|-------|--------|
| `type` | `delegate` \| `report` \| `ask` \| `reply` \| `progress` \| `release` |
| `from` / `to` | Must match session instance names; ACL must allow edge |
| `correlationId` | Shared across a workflow thread |
| `replyToId` | Set on `reply` / follow-up `ask` to prior `id` |
| `payload` | Inline object; keep small |
| `payloadPath` | Prefer for large content; file under `artifacts/<taskId>/` |
| Exactly one of | Prefer `payloadPath` when body > ~2KB |
| `notify.cmux` | If true, sender fires `cmux notify` after write (injectable in tests) |
| `ttlSec` | Receiver may ignore/expire stale pending messages |

### Type semantics

| Type | Who → whom | Expectation |
|------|------------|-------------|
| `delegate` | Orchestrator → specialist | Specialist claims, works, ends with `report` or `ask` |
| `report` | Specialist → Orchestrator | Terminal result for this task slice |
| `ask` | Specialist → Orchestrator (Phase 1 hub-only) | Blocking for specialist until `reply` |
| `reply` | Orchestrator → specialist | Answers `replyToId` |
| `progress` | Specialist → Orchestrator | Non-blocking checkpoint |
| `release` | Orchestrator → specialist | Teardown hint (temp) or idle (persistent) |

Phase 1: specialists do **not** write to peer inboxes even if ACL would allow later. ACL peers come from `agents.yaml` via the catalog layer.

## Send protocol

1. Validate ACL (`from`→`to`): hub edges always; peer edges require `--allow-peers` (Phase 2+).
2. If large body → write `artifacts/<taskId>/…`, set `payloadPath`.
3. Write envelope to `inbox/<to>/pending/<filename>.json` (atomic rename).
4. Optionally copy to `outbox/<id>.json`.
5. If `notify.cmux` and inside cmux:  
   `cmux notify --title "<from>" --body "<type> <taskId>"`  
   Optional nudge: `cmux send --surface <id> $'\n'` only to wake a stuck idle TTY — never paste full JSON into the TTY.
6. Update `sessions.json` status if needed (`working` when delegating).

## Receive protocol (specialist poll)

Each specialist on an idle loop or between tool turns:

1. List `inbox/<me>/pending/*.json` sorted by filename (time order).
2. Claim oldest: rename into `processing/`.
3. Handle by `type`.
4. Move to `done/` (or delete after copying summary to artifacts).
5. For `ask`: write `reply` into asker’s pending; notify.
6. Poll interval default: **5s** while waiting; **1s** after self-notify or when status is `working` and awaiting reply. No busy-spin.

Always `send` a `report`/`ask` then `done` before going idle. Silent settle without a hub message triggers lifecycle recovery (nudge → abandon/respawn).

## Hub delivery (lifecycle bridge — primary)

Preferred control plane: **spawn → delegate → free hub**. Do not use a one-shot run tool. Do **not** block in `agency_wait` for normal completion.

1. Specialist writes `report`/`ask` into `inbox/orchestrator/pending/`.
2. Lifecycle extension + `hub_delivery` claim the envelope.
3. If hub is idle → **push** into Orchestrator chat; if hub is busy → **queue** banner and deliver on settle.
4. Bus files remain the durable audit trail; push is delivery UX only.

### Legacy wait-by-taskId (fallback)

Manual poll when push delivery is unavailable:

```bash
$BUS wait --as orchestrator --task-id <id> --timeout 120 --interval 2
# or: agency_ctl wait / agency_wait tool
```

Behavior:

1. Scan `inbox/orchestrator/pending/` for envelopes whose `taskId` matches (leave other tasks untouched).
2. Matching `progress` → claim, move to `done/`, keep waiting.
3. Matching `ask` or `report` → claim into `processing/`, return immediately (caller runs `done` after handling).
4. Timeout with no match → `{ status: "timeout" }` — **safe to call wait again** with the same `taskId`.
5. Non-matching pending messages stay in `pending/` for later waits.

Helpers:

```bash
$BUS send --from orchestrator --to scout-t3f2 --type delegate --task-id … --payload-json '…'
$BUS recv --as scout-t3f2
$BUS wait --as orchestrator --task-id … --timeout 120
$BUS done --as scout-t3f2 --path …
$BUS list --as orchestrator
$BUS init <instanceName>
```

## Notify conventions

| Event | Title | Body |
|-------|-------|------|
| New delegate | `{to}` | `delegate {taskId}` |
| Report ready | `orchestrator` | `report {taskId} from {from}` |
| Ask | `orchestrator` | `ask {taskId} from {from}` |
| Reply | `{to}` | `reply {replyToId}` |
| Progress | `orchestrator` | `progress {taskId}` |

Prefer **CLI** `cmux notify` from Orchestrator (reliable when Orchestrator is in cmux). Specialists may use OSC 777 from their pane if CLI socket access is awkward:

```bash
printf '\e]777;notify;%s;%s\a' "orchestrator" "report auth-explore-scout-1"
```

## Failure / stale

- Claim stuck in `processing/` > 15m → Orchestrator may requeue to `pending/` or mark failed.
- `ttlSec` exceeded in `pending/` → move to `done/` with `expired` marker file or delete.
- Missing `to` inbox dir → create on spawn (Orchestrator spawn playbook).
- Sender outside cmux: still write files; skip `cmux notify` and tell human to check Orchestrator pane.
- Silent settle without report → recovery layer: grace → one nudge → abandon/respawn same `taskId`.

## Out of scope (v0)

- Encrypted payloads / cross-machine bus
- Exactly-once delivery guarantees beyond rename claim
- Peer specialist→specialist envelopes (Phase 2+)
- Replacing cmux for spawn (still cmux panes)
