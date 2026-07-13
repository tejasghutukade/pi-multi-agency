#!/usr/bin/env bash
# Phase 1 live-run helper — must be run from a terminal INSIDE cmux.
# External shells cannot talk to the cmux control socket.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PATH="$HOME/bin:/Applications/cmux.app/Contents/Resources/bin:$PATH"
export PATH

cd "$ROOT"

echo "== Multi-Agency Phase 1 bootstrap =="
echo "cwd: $ROOT"

if ! command -v cmux >/dev/null; then
  echo "cmux not on PATH. Symlink: ln -sf /Applications/cmux.app/Contents/Resources/bin/cmux \$HOME/bin/cmux"
  exit 1
fi

if ! cmux ping >/dev/null 2>&1; then
  echo "cmux ping failed."
  echo "This script must run inside a cmux terminal (cmux only accepts socket clients started inside cmux)."
  exit 1
fi

if ! command -v pi >/dev/null; then
  echo "pi not found on PATH"
  exit 1
fi

echo "cmux ping: ok"
echo "pi: $(command -v pi)"

echo
echo "Next steps (Orchestrator pane — this pane):"
echo "  1. pi"
echo "  2. /name orchestrator"
echo "  3. Tell pi to load: .pi/agency/skills/orchestrator/SKILL.md"
echo "  4. Ask it to run golden path: Explore auth options for this multi-agency project"
echo
echo "Spawn helpers the Orchestrator should run (from inside cmux):"
echo "  cmux new-split right"
echo "  sleep 0.5"
echo "  # then send into the new surface — see playbook"
echo
echo "Manifest: $ROOT/.pi/agency/sessions.json"
echo "Agents:   $ROOT/.pi/agency/agents.yaml"
