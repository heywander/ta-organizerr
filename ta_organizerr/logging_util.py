"""In-memory log buffers shared across modules."""
from __future__ import annotations

import threading

_MAIN_BUFFER_LIMIT = 1000
_TRANSCODE_BUFFER_LIMIT = 500

log_buffer: list[str] = []
log_lock = threading.Lock()

transcode_log_buffer: list[str] = []
transcode_log_lock = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)
    with log_lock:
        log_buffer.append(msg)
        if len(log_buffer) > _MAIN_BUFFER_LIMIT:
            log_buffer.pop(0)


def tlog(msg: str) -> None:
    print(f"[TRANSCODE] {msg}", flush=True)
    with transcode_log_lock:
        transcode_log_buffer.append(msg)
        if len(transcode_log_buffer) > _TRANSCODE_BUFFER_LIMIT:
            transcode_log_buffer.pop(0)
