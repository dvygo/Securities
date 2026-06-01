#!/usr/bin/env python3
"""Download NSE F&O (NFO) symbol master from Fyers public sym_details."""

from __future__ import annotations

import sys
from pathlib import Path

_HELPERS_DIR = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPERS_DIR))

from fyers_download import main_segment

if __name__ == "__main__":
    raise SystemExit(main_segment("xnfo", sys.argv[1:]))
