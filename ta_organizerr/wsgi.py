"""WSGI entry point for production servers (gunicorn).

Usage:
    gunicorn ta_organizerr.wsgi:app

The scheduler thread is started here on import so that under gunicorn it runs
exactly once per worker (we run a single worker by default — see Dockerfile).
"""
from __future__ import annotations

import os
import threading

from .app import create_app
from .scheduler import scheduler_loop


app = create_app()


if os.getenv("DISABLE_SCHEDULER", "false").lower() != "true":
    threading.Thread(target=scheduler_loop, daemon=True).start()
