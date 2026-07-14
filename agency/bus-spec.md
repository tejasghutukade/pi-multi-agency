# Agency Message Transport (broker-only)

All agency traffic is brokered over a local socket between Pi panes. Specialists
**never** touch the filesystem bus or run `bus.py` directly.

## What specialists do

- Receive delegates / replies: **wait for them to arrive as turns in this Pi
  session** (the lifecycle bridge injects them). Do not poll any folder.
- Send a report: use the **`agency_report`** tool.
- Ask the orchestrator a question: use the **`agency_ask`** tool.
- Send a progress/standby note: use the **`agency_progress`** tool.

These tools are the only supported transport. There is no `bus.py recv` / `bus.py
send` step and no `$BUS` variable in a specialist session.

## Layout (orchestrator/audit only — not for specialists)

```text
.pi/agency/
  inbox/
    orchestrator/pending|processing|done/   # specialist -> orchestrator
    <instanceName>/pending|processing|done/ # orchestrator -> specialist
  outbox/                                  # audit copies
  artifacts/<taskId>/                        # large payloads by path
  sessions.json
```

Filesystem envelopes are written by the broker for audit/compatibility only.
Specialists must not read or write them and must not run the bus scripts.
