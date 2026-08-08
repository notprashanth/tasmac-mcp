#!/usr/bin/env python3
"""Compatibility shim. The server now lives in tasmac_mcp/server.py.

Kept so anyone who registered this path before the package restructure, or who
follows an older README, keeps working. Prefer the `tasmac-mcp` command when
installed from PyPI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tasmac_mcp.server import main

if __name__ == "__main__":
    main()
