"""Background periodic scan loop."""
from __future__ import annotations

import time

from .config import SCAN_INTERVAL
from .logging_util import log
from .symlinks import process_videos


def scheduler_loop() -> None:
    log(f"🕒 Background scheduler started. Scanning every {SCAN_INTERVAL} minutes.")
    while True:
        log("🔄 Running scheduled scan...")
        try:
            process_videos()
        except Exception as exc:  # noqa: BLE001 — must never kill the loop
            log(f"❌ Scheduled scan crashed: {exc}")
        time.sleep(SCAN_INTERVAL * 60)
