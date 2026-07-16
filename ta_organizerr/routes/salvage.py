"""Authenticated API for non-destructive media diagnosis and salvage jobs."""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request, url_for

from ..auth import requires_auth
from ..salvage import (
    SalvageError,
    cancel_job,
    exact_id_candidate_inventory,
    get_job,
    list_jobs,
    parse_expected_duration,
    parse_expected_sha256,
    start_job,
)
from ..utils import is_valid_youtube_id


bp = Blueprint("salvage", __name__, url_prefix="/api/salvage")


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return data, None


def _source_path(data: dict):
    # source_path is the documented spelling; filepath remains an API alias so
    # the recovery/reconciliation components can hand off without translation.
    return data.get("source_path", data.get("filepath"))


@bp.post("/diagnose")
@requires_auth
def api_salvage_diagnose():
    data, error = _json_body()
    if error:
        return error
    expected, duration_error = parse_expected_duration(
        data.get("expected_duration_seconds")
    )
    if duration_error:
        return jsonify({"error": duration_error}), 400
    job, message, status = start_job(
        _source_path(data),
        "diagnose",
        candidate_path=data.get("candidate_path"),
        expected_duration_seconds=expected,
    )
    if job is None:
        return jsonify({"error": message}), status
    return jsonify({"job": job}), status


@bp.post("/start")
@requires_auth
def api_salvage_start():
    data, error = _json_body()
    if error:
        return error
    mode = data.get("operation", data.get("mode"))
    if mode not in {"remux", "reencode"}:
        return jsonify({"error": "operation must be remux or reencode"}), 400
    expected, duration_error = parse_expected_duration(
        data.get("expected_duration_seconds")
    )
    if duration_error:
        return jsonify({"error": duration_error}), 400
    expected_sha256, checksum_error = parse_expected_sha256(
        data.get("expected_source_sha256")
    )
    if checksum_error:
        return jsonify({"error": checksum_error}), 400
    job, message, status = start_job(
        _source_path(data),
        mode,
        expected_duration_seconds=expected,
        expected_source_sha256=expected_sha256,
    )
    if job is None:
        return jsonify({"error": message}), status
    return jsonify({"job": job}), status


@bp.get("/jobs")
@requires_auth
def api_salvage_jobs():
    return jsonify({"jobs": list_jobs()})


@bp.get("/jobs/<job_id>")
@requires_auth
def api_salvage_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job})


@bp.post("/jobs/<job_id>/cancel")
@requires_auth
def api_salvage_cancel(job_id: str):
    job, message, status = cancel_job(job_id)
    payload = {"job": job} if job is not None else {}
    if message:
        payload["error"] = message
    return jsonify(payload), status


@bp.get("/candidates")
@requires_auth
def api_salvage_candidates():
    video_id = request.args.get("video_id", "")
    if not is_valid_youtube_id(video_id):
        return jsonify({
            "error": "video_id must be an exact case-sensitive 11-character YouTube ID"
        }), 400
    try:
        exclude_path = request.args.get("exclude_path") or None
        inventory = exact_id_candidate_inventory(video_id, exclude_path=exclude_path)
    except SalvageError as exc:
        status = 429 if "already running" in str(exc).lower() else 400
        return jsonify({"error": str(exc)}), status
    return jsonify({
        "video_id": video_id,
        "match_policy": "exact_case_sensitive_video_id_only",
        **inventory,
    })


def _preview_redirect(endpoint: str):
    filepath = request.args.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    # The recovery preview endpoint performs the actual O_NOFOLLOW open,
    # allow-list validation, range parsing, and transcoding.  Do not duplicate
    # that security-sensitive serving code here.
    return redirect(url_for(endpoint, filepath=filepath), code=307)


@bp.get("/preview")
@requires_auth
def api_salvage_preview():
    return _preview_redirect("recovery.api_recovery_preview")


@bp.get("/preview/audio")
@bp.get("/preview-audio")
@requires_auth
def api_salvage_preview_audio():
    return _preview_redirect("recovery.api_recovery_preview_audio")
