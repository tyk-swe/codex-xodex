#!/usr/bin/env python3
"""Compatibility wrapper for the installed read-only MCP probe."""
import sys

from xodex.cli import main

if __name__ == "__main__":
    sys.argv[1:1] = ["smoke"]
    main()
