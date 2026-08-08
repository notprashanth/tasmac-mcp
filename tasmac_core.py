#!/usr/bin/env python3
"""Compatibility shim. The core now lives in tasmac_mcp/core.py.

Kept so the existing `/tasmac` slash command and any older instructions keep
working from a checkout. Prefer the `tasmac` command when installed from PyPI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tasmac_mcp.core import *          # noqa: F401,F403  (re-export for importers)
from tasmac_mcp.core import main

if __name__ == "__main__":
    main()
