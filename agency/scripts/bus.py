#!/usr/bin/env python3
"""Multi-Agency legacy file-bus CLI — see .pi/agency/bus-spec.md"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agency_paths import agency_root as paths_agency_root  # noqa: E402
from catalog import load_agents, role_of  # noqa: E402
from catalog import HUB as CATALOG_HUB  # noqa: E402
from cmux_pane import caller_surface, surface_alive  # noqa: E402
from ledger import load_sessions  # noqa: E402

TYPES = ("delegate", "report", "ask", "reply", "progress", "release")
HUB = "orchestrator"
assert CATALOG_HUB == HUB


def agency_root() -> Path:
    env = os.environ.get("AGENCY_ROOT")
    if env:
        return Path(env).resolve()
    # Prefer project AGENCY_ROOT semantics when available; kit fallback for bus CLI alone.
    root = paths_agency_root()
    return root


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compact_ts(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def ensure_inbox(root: Path, name: str) -> Path:
    base = root / "inbox" / name
    for sub in ("pending", "processing", "done", "rejected"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (root / "outbox").mkdir(parents=True, exist_ok=True)
    return base


def load_agents_yaml(root: Path) -> dict[str, Any]:
    return load_agents(root)


def acl_allows(root: Path, frm: str, to: str, phase1_hub_only: bool = True) -> bool:
    if frm == HUB or to == HUB:
        return True
    if phase1_hub_only:
        return False
    data = load_agents_yaml(root)
    agents = data.get("agents") or {}
    peers = (agents.get(role_of(frm)) or {}).get("peers") or []
    return role_of(to) in peers


def cmux_notify(title: str, body: str) -> bool:
    return _notify(title, body)


def _default_cmux_notify(title: str, body: str) -> bool:
    cmux = shutil.which("cmux")
    if not cmux:
        home_bin = Path.home() / "bin" / "cmux"
        app = Path("/Applications/cmux.app/Contents/Resources/bin/cmux")
        cmux = str(home_bin if home_bin.exists() else app if app.exists() else "")
    if not cmux:
        return False
    try:
        r = subprocess.run(
            [cmux, "notify", "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_notify = _default_cmux_notify


def set_notify(fn) -> None:
    """Override notify side-effect (tests). Pass None to restore default."""
    global _notify
    _notify = fn if fn is not None else _default_cmux_notify


def _authenticated_sender(root: Path, from_name: str) -> dict[str, str]:
    surface, _pane = caller_surface()
    rows = [
        row
        for row in (load_sessions(root).get("instances") or [])
        if row.get("cmuxSurface") == surface
    ]
    if len(rows) != 1:
        raise RuntimeError(
            f"sender authentication denied: caller surface {surface!r} maps to {len(rows)} rows"
        )
    row = rows[0]
    if row.get("intercomName") != from_name:
        raise RuntimeError(
            f"sender authentication denied: caller is {row.get('intercomName')!r}, not {from_name!r}"
        )
    if surface_alive(surface) is not True:
        raise RuntimeError("sender authentication denied: caller surface is not confirmed alive")
    instance_id = row.get("instanceId")
    if not isinstance(instance_id, str) or not instance_id:
        raise RuntimeError("sender authentication denied: session instanceId is missing")
    return {"instanceId": instance_id, "intercomName": from_name, "surface": surface}


def _message_locations(root: Path, recipient: str, message_id: str) -> list[Path]:
    ensure_inbox(root, recipient)
    found: list[Path] = []
    inbox_root = root / "inbox"
    for inbox in sorted(path for path in inbox_root.iterdir() if path.is_dir()):
        for state in ("pending", "processing", "done"):
            for path in sorted((inbox / state).glob("*.json")):
                try:
                    envelope = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(envelope, dict) and envelope.get("id") == message_id:
                    found.append(path)
    outbox = root / "outbox" / f"{message_id}.json"
    if outbox.is_file():
        found.append(outbox)
    return found


def _immutable_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in envelope.items() if key != "createdAt"}


def _write_pending_envelope(root: Path, envelope: dict[str, Any]) -> Path:
    recipient = envelope["to"]
    pending = ensure_inbox(root, recipient) / "pending"
    created = datetime.fromisoformat(envelope["createdAt"].replace("Z", "+00:00"))
    fname = f"{compact_ts(created)}-{envelope['id']}-{envelope['type']}.json"
    tmp = pending / f".{fname}.tmp"
    final = pending / fname
    tmp.write_text(json.dumps(envelope, indent=2) + "\n")
    tmp.replace(final)
    return final


def cmd_send(args: argparse.Namespace) -> int:
    root = agency_root()
    if args.type not in TYPES:
        print(f"error: type must be one of {TYPES}", file=sys.stderr)
        return 2
    sender_auth = None
    if bool(getattr(args, "require_caller", False)):
        try:
            sender_auth = _authenticated_sender(root, args.from_name)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4

    acl_allowed = acl_allows(
        root,
        args.from_name,
        args.to,
        phase1_hub_only=not getattr(args, "allow_peers", False),
    )
    if not acl_allowed and sender_auth is not None:
        sender_row = next(
            (
                row
                for row in (load_sessions(root).get("instances") or [])
                if row.get("instanceId") == sender_auth["instanceId"]
            ),
            None,
        )
        acl_allowed = bool(sender_row and sender_row.get("role") == "pipeline-runner")
    if not acl_allowed:
        print(
            f"error: ACL denied {args.from_name} → {args.to} (Phase 1 hub-only unless --allow-peers)",
            file=sys.stderr,
        )
        return 3

    requested_id = getattr(args, "message_id", None)
    if requested_id is not None and (
        not isinstance(requested_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", requested_id)
    ):
        print("error: message id must be a safe identifier", file=sys.stderr)
        return 2

    payload: Any = None
    payload_path = args.payload_path
    if args.payload_json:
        payload = json.loads(args.payload_json)
    elif args.payload_file:
        p = Path(args.payload_file)
        text = p.read_text()
        if len(text.encode()) > 2048 and not payload_path:
            art = root / "artifacts" / (args.task_id or "misc")
            art.mkdir(parents=True, exist_ok=True)
            dest = art / f"{short_id()}-payload.json"
            dest.write_text(text)
            try:
                payload_path = str(dest.relative_to(root))
            except ValueError:
                payload_path = str(dest)
            payload = None
        else:
            payload = json.loads(text)

    now = utc_now()
    msg_id = requested_id or short_id()
    notify_title = args.notify_title or args.to
    notify_body = args.notify_body or f"{args.type} {args.task_id or msg_id}"
    env = {
        "schemaVersion": 1,
        "id": msg_id,
        "type": args.type,
        "from": args.from_name,
        "to": args.to,
        "taskId": args.task_id,
        "workflowId": args.workflow_id,
        "correlationId": args.correlation_id,
        "replyToId": args.reply_to,
        "createdAt": now.isoformat().replace("+00:00", "Z"),
        "ttlSec": args.ttl,
        "priority": args.priority,
        "aclChecked": True,
        "notify": {
            "title": notify_title,
            "body": notify_body,
            "cmux": not args.no_notify,
        },
        "payload": payload,
        "payloadPath": payload_path,
    }
    if sender_auth is not None:
        env["senderAuth"] = sender_auth

    locations = _message_locations(root, args.to, msg_id) if requested_id else []
    if locations:
        existing: dict[str, Any] | None = None
        for location in locations:
            try:
                candidate = json.loads(location.read_text())
            except (OSError, json.JSONDecodeError):
                print(f"error: existing message {msg_id!r} is unreadable", file=sys.stderr)
                return 5
            if not isinstance(candidate, dict):
                print(f"error: existing message {msg_id!r} is invalid", file=sys.stderr)
                return 5
            if existing is None:
                existing = candidate
            elif candidate != existing:
                print(f"error: conflicting durable copies for message {msg_id!r}", file=sys.stderr)
                return 5
        assert existing is not None
        env["createdAt"] = existing.get("createdAt")
        if _immutable_envelope(existing) != _immutable_envelope(env):
            print(f"error: message id {msg_id!r} conflicts with existing envelope", file=sys.stderr)
            return 5
        inbox_locations = [path for path in locations if path.parent.name in {"pending", "processing", "done"}]
        out = root / "outbox" / f"{msg_id}.json"
        if not inbox_locations:
            final = _write_pending_envelope(root, existing)
        else:
            final = inbox_locations[0]
        if not out.is_file():
            out.write_text(json.dumps(existing, indent=2) + "\n")
        print(
            json.dumps(
                {"ok": True, "id": msg_id, "path": str(final), "notified": False, "replay": True},
                indent=2,
            )
        )
        return 0

    final = _write_pending_envelope(root, env)
    out = root / "outbox" / f"{msg_id}.json"
    out.write_text(json.dumps(env, indent=2) + "\n")

    from agency_events import emit

    emit(
        "bus.sent",
        root=root,
        instance=args.to,
        taskId=args.task_id,
        envelopeType=args.type,
        fromName=args.from_name,
        path=str(final),
    )

    notified = False
    if not args.no_notify:
        notified = cmux_notify(notify_title, notify_body)

    print(
        json.dumps(
            {
                "ok": True,
                "id": msg_id,
                "path": str(final),
                "notified": notified,
            },
            indent=2,
        )
    )
    return 0


def list_pending(root: Path, name: str) -> list[Path]:
    pending = root / "inbox" / name / "pending"
    if not pending.exists():
        return []
    return sorted(p for p in pending.glob("*.json") if p.is_file())


def claim_pending(root: Path, name: str, src: Path) -> tuple[Path, dict]:
    processing = root / "inbox" / name / "processing" / src.name
    src.replace(processing)
    data = json.loads(processing.read_text())
    from agency_events import emit

    emit(
        "bus.claimed",
        root=root,
        instance=name,
        taskId=data.get("taskId"),
        envelopeType=data.get("type"),
        path=str(processing),
    )
    return processing, data


def move_to_done(root: Path, name: str, path: Path) -> Path:
    dest = root / "inbox" / name / "done" / path.name
    path.replace(dest)
    try:
        data = json.loads(dest.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    from agency_events import emit

    emit(
        "bus.done",
        root=root,
        instance=name,
        taskId=data.get("taskId"),
        envelopeType=data.get("type"),
        path=str(dest),
    )
    return dest


def cmd_recv(args: argparse.Namespace) -> int:
    root = agency_root()
    ensure_inbox(root, args.as_name)
    files = list_pending(root, args.as_name)
    if not files:
        if args.wait:
            deadline = time.time() + args.wait
            while time.time() < deadline:
                time.sleep(args.interval)
                files = list_pending(root, args.as_name)
                if files:
                    break
        if not files:
            print(json.dumps({"ok": True, "empty": True}))
            return 0

    processing, data = claim_pending(root, args.as_name, files[0])
    print(json.dumps({"ok": True, "empty": False, "path": str(processing), "envelope": data}, indent=2))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    """Poll pending for a matching taskId; leave other tasks untouched."""
    root = agency_root()
    ensure_inbox(root, args.as_name)
    deadline = time.time() + args.timeout
    progress_acked = 0

    while True:
        matched: list[tuple[Path, dict]] = []
        for src in list_pending(root, args.as_name):
            try:
                peek = json.loads(src.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if peek.get("taskId") != args.task_id:
                continue
            expected_type = getattr(args, "type", None)
            if expected_type is not None and peek.get("type") != expected_type:
                continue
            expected_sender = getattr(args, "from_name", None)
            if expected_sender is not None and peek.get("from") != expected_sender:
                continue
            matched.append((src, peek))

        if args.auto_done_progress:
            for src, peek in list(matched):
                if peek.get("type") != "progress":
                    continue
                if not src.is_file():
                    continue
                processing, _data = claim_pending(root, args.as_name, src)
                move_to_done(root, args.as_name, processing)
                progress_acked += 1
            matched = [(s, p) for s, p in matched if p.get("type") != "progress" and s.is_file()]

        # Prefer ask over report if both are pending for the same task.
        order = {"ask": 0, "report": 1, "reply": 2, "progress": 3}
        matched.sort(key=lambda sp: order.get(sp[1].get("type") or "", 9))

        for src, peek in matched:
            typ = peek.get("type")
            if typ not in ("ask", "report", "progress", "reply"):
                continue
            if not src.is_file():
                continue
            processing, data = claim_pending(root, args.as_name, src)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "message",
                        "taskId": args.task_id,
                        "type": data.get("type"),
                        "path": str(processing),
                        "envelope": data,
                        "progressAcked": progress_acked,
                    },
                    indent=2,
                )
            )
            return 0

        now = time.time()
        if now >= deadline:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "timeout",
                        "taskId": args.task_id,
                        "empty": True,
                        "progressAcked": progress_acked,
                    },
                    indent=2,
                )
            )
            return 0

        time.sleep(min(args.interval, max(0.05, deadline - now)))


def cmd_done(args: argparse.Namespace) -> int:
    root = agency_root()
    path = Path(args.path)
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2
    name = args.as_name
    ensure_inbox(root, name)
    dest = move_to_done(root, name, path)
    print(json.dumps({"ok": True, "path": str(dest)}))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = agency_root()
    ensure_inbox(root, args.as_name)
    files = list_pending(root, args.as_name)
    print(
        json.dumps(
            {
                "ok": True,
                "as": args.as_name,
                "pending": [str(p) for p in files],
                "count": len(files),
            },
            indent=2,
        )
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = agency_root()
    for name in args.names:
        ensure_inbox(root, name)
    print(json.dumps({"ok": True, "inboxes": args.names}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="bus", description="Multi-Agency hybrid message bus")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="Write envelope to recipient pending/")
    s.add_argument("--from", dest="from_name", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--type", required=True, choices=TYPES)
    s.add_argument("--task-id")
    s.add_argument("--workflow-id")
    s.add_argument("--correlation-id")
    s.add_argument("--reply-to")
    s.add_argument("--payload-json")
    s.add_argument("--payload-file")
    s.add_argument("--payload-path")
    s.add_argument("--ttl", type=int, default=3600)
    s.add_argument("--priority", default="normal")
    s.add_argument("--notify-title")
    s.add_argument("--notify-body")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--allow-peers", action="store_true", help="Allow specialist↔specialist (Phase 2+)")
    s.add_argument("--require-caller", action="store_true", help="Authenticate --from against the live caller surface")
    s.add_argument("--message-id", help="Validated stable id for idempotent durable delivery")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("recv", help="Claim oldest pending → processing")
    r.add_argument("--as", dest="as_name", required=True)
    r.add_argument("--wait", type=float, default=0, help="Seconds to wait for a message")
    r.add_argument("--interval", type=float, default=1.0)
    r.set_defaults(func=cmd_recv)

    w = sub.add_parser("wait", help="Wait for pending envelope matching taskId")
    w.add_argument("--as", dest="as_name", required=True)
    w.add_argument("--task-id", required=True)
    w.add_argument("--from", dest="from_name", help="Match only envelopes from this sender")
    w.add_argument("--type", choices=TYPES, help="Match only this exact envelope type")
    w.add_argument("--timeout", type=float, default=120.0)
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument(
        "--auto-done-progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ack matching progress envelopes and keep waiting (default: true)",
    )
    w.set_defaults(func=cmd_wait)

    d = sub.add_parser("done", help="Move processing file → done/")
    d.add_argument("--as", dest="as_name", required=True)
    d.add_argument("--path", required=True)
    d.set_defaults(func=cmd_done)

    l = sub.add_parser("list", help="List pending for an agent")
    l.add_argument("--as", dest="as_name", required=True)
    l.set_defaults(func=cmd_list)

    i = sub.add_parser("init", help="Create inbox dirs for names")
    i.add_argument("names", nargs="+")
    i.set_defaults(func=cmd_init)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
