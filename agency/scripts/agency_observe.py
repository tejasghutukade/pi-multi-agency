#!/usr/bin/env python3
"""Local ops observer — stdlib HTTP + static UI over agency root."""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agency_paths import agency_root  # noqa: E402
from observe_state import snapshot  # noqa: E402

STATIC_DIR = _SCRIPTS_DIR.parent / "observe" / "static"


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2) + "\n").encode()


class ObserveHandler(BaseHTTPRequestHandler):
    agency: Path = Path(".")
    server_version = "AgencyObserve/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            return self._static(rel, None)
        if path == "/api/snapshot":
            snap = snapshot(self.agency)
            return self._send(200, _json_bytes(snap), "application/json; charset=utf-8")
        if path == "/api/events/stream":
            return self._sse()
        if path in ("/app.js", "/app.css"):
            ctype = "application/javascript" if path.endswith(".js") else "text/css"
            return self._static(path.lstrip("/"), ctype)
        self._send(404, b'{"ok":false,"error":"not found"}\n', "application/json")

    def _static(self, name: str, content_type: str | None) -> None:
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found\n", "text/plain")
            return
        data = target.read_bytes()
        if content_type is None:
            if name.endswith(".js"):
                content_type = "application/javascript"
            elif name.endswith(".css"):
                content_type = "text/css"
            elif name.endswith(".html"):
                content_type = "text/html; charset=utf-8"
            else:
                content_type = "application/octet-stream"
        self._send(200, data, content_type)

    def _sse(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        after = (qs.get("after") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = after
        try:
            while True:
                snap = snapshot(self.agency)
                events = snap.get("timeline", {}).get("events") or []
                for ev in events:
                    key = f"{ev.get('ts')}:{ev.get('type')}:{ev.get('instance')}:{ev.get('taskId')}"
                    if seen and key <= seen:
                        continue
                    payload = json.dumps(ev)
                    self.wfile.write(f"id: {key}\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    seen = key
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return


def run_server(root: Path, host: str, port: int) -> int:
    if not root.is_dir():
        print(json.dumps({"ok": False, "error": f"agency root not a directory: {root}"}), file=sys.stderr)
        return 1
    handler = type("BoundHandler", (ObserveHandler,), {"agency": root})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(json.dumps({"ok": True, "url": url, "agencyRoot": str(root)}, indent=2))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agency_observe", description="Local Multi-Agency ops observer")
    p.add_argument("--root", help="Agency root (default AGENCY_ROOT / .pi/agency)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--snapshot", action="store_true", help="Print one snapshot JSON and exit")
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else agency_root()
    if args.snapshot:
        if not root.is_dir():
            print(json.dumps({"ok": False, "error": f"missing root {root}"}), file=sys.stderr)
            return 1
        print(json.dumps(snapshot(root), indent=2))
        return 0
    return run_server(root, args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
