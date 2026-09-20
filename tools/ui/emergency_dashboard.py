#!/usr/bin/env python3
"""Retired legacy Streamlit emergency dashboard.

Emergency control is maintained by the DeviceRegistry-backed FastAPI/Web
application. Keeping a second dashboard would create competing serial owners
and a safety path outside the global OperationGate.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "This dashboard is retired. Start the maintained service with:\n"
        "  python tools/run_webserver.py\n\n"
        "Use its authenticated POST /api/estop endpoint or the Web emergency "
        "stop control. No hardware connection was opened.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
