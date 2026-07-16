"""Authenticated Link Reconciliation inventory and guarded actions."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..auth import requires_auth
from .. import reconciliation
from ..logging_util import log


bp = Blueprint("reconciliation", __name__, url_prefix="/api/reconciliation")


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise reconciliation.ReconciliationError(
            "Request body must be a JSON object", 400
        )
    return data


def _run(operation, *, action: str | None = None):
    try:
        result = operation()
        if action:
            actor = session.get("username") or "authenticated user"
            log(f"✅ [RECONCILIATION] {action} completed by {actor}")
        return jsonify(result)
    except reconciliation.ReconciliationError as exc:
        if action:
            actor = session.get("username") or "authenticated user"
            log(f"⛔ [RECONCILIATION] Rejected {action} by {actor}: {exc}")
        body = {"error": str(exc)}
        cached = reconciliation.get_cached_inventory()
        if cached is not None:
            body["inventory"] = cached
        return jsonify(body), exc.status


@bp.route("/inventory", methods=["GET"])
@requires_auth
def api_reconciliation_inventory():
    return _run(reconciliation.build_inventory)


@bp.route("/rescan", methods=["POST"])
@requires_auth
def api_reconciliation_rescan():
    return _run(reconciliation.build_inventory)


@bp.route("/repair-link", methods=["POST"])
@requires_auth
def api_reconciliation_repair_link():
    return _run(lambda: reconciliation.repair_link(_payload()), action="Repair Link")


@bp.route("/adopt-real-file", methods=["POST"])
@requires_auth
def api_reconciliation_adopt_real_file():
    return _run(
        lambda: reconciliation.adopt_existing_real_file(_payload()),
        action="Adopt Existing Real File",
    )


@bp.route("/preview-candidate", methods=["POST"])
@requires_auth
def api_reconciliation_preview_candidate():
    return _run(lambda: reconciliation.preview_candidate(_payload()), action="Preview Candidate")


@bp.route("/remove-broken-link", methods=["POST"])
@requires_auth
def api_reconciliation_remove_broken_link():
    return _run(
        lambda: reconciliation.remove_broken_link(_payload()),
        action="Remove Broken Link",
    )


@bp.route("/forget-stale-record", methods=["POST"])
@requires_auth
def api_reconciliation_forget_stale_record():
    return _run(
        lambda: reconciliation.forget_stale_record(_payload()),
        action="Forget Stale Record",
    )
