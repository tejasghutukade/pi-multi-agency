#!/usr/bin/env python3
"""Compatibility CLI — re-exports agent_spawn."""

from __future__ import annotations

from agent_spawn import main, spawn_specialist

__all__ = ["main", "spawn_specialist"]

if __name__ == "__main__":
    raise SystemExit(main())
