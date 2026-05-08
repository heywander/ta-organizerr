"""Maintenance endpoints (broken-symlink repair, write-permission probe)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify

from ..auth import requires_auth
from ..config import DATA_DIR, HIDDEN_DIR, SOURCE_DIR, TARGET_DIR
from ..logging_util import log
from ..symlinks import fix_broken_symlinks, process_videos


bp = Blueprint("maintenance", __name__, url_prefix="/api")


@bp.route("/maintenance/fix-symlinks", methods=["POST"])
@requires_auth
def api_fix_symlinks():
    def run_fix():
        fixed = fix_broken_symlinks()
        log(f"✅ Maintenance complete. Fixed {len(fixed)} entries.")
        if fixed:
            log("🔄 Triggering rescan to recreate symlinks...")
            process_videos()

    threading.Thread(target=run_fix, daemon=True).start()
    return jsonify({"status": "started", "message": "Symlink repair running in background"})


@bp.route("/system/check-permissions", methods=["GET"])
@requires_auth
def api_check_permissions():
    results: dict[str, dict] = {}
    test_dirs = [
        ("source", SOURCE_DIR),
        ("target", TARGET_DIR),
        ("hidden", HIDDEN_DIR),
        ("data", DATA_DIR),
    ]
    log("🔍 Running System Permission Check...")
    for name, path in test_dirs:
        if not path:
            results[name] = {"status": "unset", "writeable": False}
            continue
        p = Path(path)
        if not p.exists():
            results[name] = {"status": "missing", "writeable": False, "message": "Directory does not exist"}
            log(f"  ❌ {name} ({path}): MISSING")
            continue

        test_file = p / f".write_test_{os.getpid()}"
        try:
            log(f"  🧪 Testing write on {name}...")
            if test_file.exists():
                test_file.unlink()
            with open(test_file, "w", encoding="utf-8") as fh:
                fh.write("test")
            test_file.unlink()
            results[name] = {"status": "ok", "writeable": True}
            log(f"  ✅ {name} ({path}): WRITEABLE")
        except OSError as exc:
            msg = str(exc)
            results[name] = {"status": "error", "writeable": False, "message": msg}
            log(f"  ❌ {name} ({path}): READ-ONLY or PERMISSION DENIED - {msg}")
            if "Read-only file system" in msg:
                log(f"  🚨 POSITIVE R/O MOUNT DETECTED for {name}")
    return jsonify(results)
