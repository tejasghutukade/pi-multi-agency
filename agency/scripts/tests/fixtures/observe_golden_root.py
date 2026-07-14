from __future__ import annotations

import json
from pathlib import Path


def build_observe_golden_root(root: Path) -> Path:
    """AE1: scout working + hub pending report."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "intercomName": "orchestrator",
                        "role": "orchestrator",
                        "status": "idle",
                        "lifecycle": "persistent",
                        "cmuxSurface": None,
                        "taskId": None,
                    },
                    {
                        "intercomName": "scout-t01",
                        "role": "scout",
                        "status": "working",
                        "lifecycle": "temporary",
                        "taskId": "task-ae1",
                        "cmuxSurface": "surface:scout",
                    },
                ],
            },
            indent=2,
        )
        + "\n"
    )
    pending = root / "inbox" / "orchestrator" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (root / "inbox" / "orchestrator" / "processing").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / "orchestrator" / "done").mkdir(parents=True, exist_ok=True)
    (pending / "20260713T000000Z-ae1-report.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "ae1",
                "type": "report",
                "from": "scout-t01",
                "to": "orchestrator",
                "taskId": "task-ae1",
                "payload": {"summary": "recon done"},
            },
            indent=2,
        )
        + "\n"
    )
    return root
