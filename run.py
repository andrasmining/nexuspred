#!/usr/bin/env python3
"""Entry point for the Tradovate Webhook Bridge.

Usage:
    python run.py                # serve on 0.0.0.0:9000
    HOST=127.0.0.1 PORT=9000 python run.py

The auto-updater re-execs this same command, so keep it self-contained.
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9000"))
    # The per-request access log is off by default: the bridge keeps its own
    # event / signal / order logs, and one synchronous stdout line per webhook
    # is measurable at the rates the fan-out reaches. NEXUSPRED_ACCESS_LOG=1
    # switches it on for debugging a proxy.
    uvicorn.run("app.main:app", host=host, port=port, reload=False,
                access_log=os.environ.get("NEXUSPRED_ACCESS_LOG", "") == "1")
