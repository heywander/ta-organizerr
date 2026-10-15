"""Entry point used by ``python -m ta_organizerr`` (development server)."""
from __future__ import annotations

import os
import threading

from .app import create_app
from .scheduler import scheduler_loop


def _start_scheduler() -> None:
    threading.Thread(target=scheduler_loop, daemon=True).start()


def main() -> None:
    app = create_app()
    _start_scheduler()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
