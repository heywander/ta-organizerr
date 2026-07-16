"""In-memory log buffers shared across modules."""
from __future__ import annotations

import threading

_MAIN_BUFFER_LIMIT = 1000
_TRANSCODE_BUFFER_LIMIT = 500

log_buffer: list[str] = []
log_lock = threading.Lock()
_log_start_index = 0

transcode_log_buffer: list[str] = []
transcode_log_lock = threading.Lock()
_transcode_log_start_index = 0


def log(msg: str) -> None:
    global _log_start_index
    print(msg, flush=True)
    with log_lock:
        log_buffer.append(msg)
        if len(log_buffer) > _MAIN_BUFFER_LIMIT:
            overflow = len(log_buffer) - _MAIN_BUFFER_LIMIT
            del log_buffer[:overflow]
            _log_start_index += overflow


def tlog(msg: str) -> None:
    global _transcode_log_start_index
    print(f"[TRANSCODE] {msg}", flush=True)
    with transcode_log_lock:
        transcode_log_buffer.append(msg)
        if len(transcode_log_buffer) > _TRANSCODE_BUFFER_LIMIT:
            overflow = len(transcode_log_buffer) - _TRANSCODE_BUFFER_LIMIT
            del transcode_log_buffer[:overflow]
            _transcode_log_start_index += overflow


def _read_buffer(buffer: list[str], start_index: int, requested: int) -> tuple[list[str], int]:
    """Read a bounded buffer using a process-lifetime monotonic cursor."""
    end_index = start_index + len(buffer)
    if requested < start_index or requested > end_index:
        offset = 0
    else:
        offset = requested - start_index
    return list(buffer[offset:]), end_index


def read_logs(start: int) -> tuple[list[str], int]:
    with log_lock:
        return _read_buffer(log_buffer, _log_start_index, start)


def read_transcode_logs(start: int) -> tuple[list[str], int]:
    with transcode_log_lock:
        return _read_buffer(
            transcode_log_buffer,
            _transcode_log_start_index,
            start,
        )
