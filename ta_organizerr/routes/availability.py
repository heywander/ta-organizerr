"""Authenticated channel and archived-video availability API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .. import availability, availability_designation, video_designation
from ..auth import requires_auth
from ..config import YOUTUBE_API_KEY


bp = Blueprint("availability", __name__, url_prefix="/api/availability")


def _actor() -> str:
    return str(session.get("username") or "authenticated user")


def _optional_body() -> dict:
    raw_body = request.get_data(cache=True)
    data = request.get_json(silent=True)
    if data is None:
        if raw_body:
            raise availability.AvailabilityError(
                "Request body must be a valid JSON object"
            )
        return {}
    if not isinstance(data, dict):
        raise availability.AvailabilityError("Request body must be a JSON object")
    return data


@bp.errorhandler(availability.AvailabilityError)
def _availability_error(exc: availability.AvailabilityError):
    return jsonify({"error": str(exc)}), exc.status


@bp.get("/channels")
@requires_auth
def api_availability_channels():
    return jsonify({
        "channels": availability.list_channels(),
        "api_corroboration_enabled": bool(YOUTUBE_API_KEY),
    })


@bp.get("/channels/<channel_id>")
@requires_auth
def api_availability_channel(channel_id: str):
    return jsonify({"channel": availability.get_channel(channel_id)})


@bp.post("/channels/<channel_id>/check")
@requires_auth
def api_availability_channel_check(channel_id: str):
    data = _optional_body()
    if data:
        raise availability.AvailabilityError("Channel check does not accept options")
    job = availability.start_channel_check(channel_id, actor=_actor(), background=True)
    return jsonify({"job": job}), 202


@bp.post("/channels/<channel_id>/designation/plan")
@requires_auth
def api_availability_designation_plan(channel_id: str):
    data = _optional_body()
    if "designation" not in data or not set(data).issubset({
        "designation", "override_minimum_separation"
    }):
        raise availability.AvailabilityError(
            "Designation plan requires designation and optionally "
            "override_minimum_separation"
        )
    override = data.get("override_minimum_separation", False)
    if not isinstance(override, bool):
        raise availability.AvailabilityError(
            "override_minimum_separation must be true or false"
        )
    return jsonify({
        "plan": availability_designation.build_designation_plan(
            channel_id,
            data["designation"],
            override_minimum_separation=override,
            actor=_actor(),
        )
    })


@bp.post("/channels/<channel_id>/designation/apply")
@requires_auth
def api_availability_designation_apply(channel_id: str):
    data = _optional_body()
    if not {"plan_token", "confirmation"}.issubset(data) or not set(data).issubset({
        "plan_token", "confirmation", "override_minimum_separation"
    }):
        raise availability.AvailabilityError(
            "Designation apply requires plan_token and confirmation, with optional "
            "override_minimum_separation"
        )
    override = data.get("override_minimum_separation", False)
    if not isinstance(override, bool):
        raise availability.AvailabilityError(
            "override_minimum_separation must be true or false"
        )
    return jsonify({
        "result": availability_designation.apply_designation(
            channel_id,
            data["plan_token"],
            data["confirmation"],
            actor=_actor(),
            override_minimum_separation=override,
        )
    })


@bp.get("/channels/<channel_id>/videos")
@requires_auth
def api_availability_channel_videos(channel_id: str):
    try:
        page = int(request.args.get("page", "1"))
        per_page = int(request.args.get("per_page", "100"))
    except ValueError:
        raise availability.AvailabilityError("page and per_page must be integers") from None
    return jsonify(availability.list_videos(
        channel_id,
        page=page,
        per_page=per_page,
        state=request.args.get("state") or None,
        query=request.args.get("q") or None,
    ))


@bp.post("/channels/<channel_id>/videos/check")
@requires_auth
def api_availability_video_check(channel_id: str):
    data = _optional_body()
    if not set(data).issubset({"video_ids", "scope", "refresh_inventory"}):
        raise availability.AvailabilityError(
            "Video check accepts only scope, video_ids, and refresh_inventory"
        )
    if "video_ids" in data and data["video_ids"] is None:
        raise availability.AvailabilityError(
            "video_ids must be a non-empty array when supplied"
        )
    job = availability.start_video_check(
        channel_id,
        data.get("video_ids"),
        scope=data.get(
            "scope", "selected" if "video_ids" in data else "smart"
        ),
        refresh_inventory=data.get("refresh_inventory", False),
        actor=_actor(),
        background=True,
    )
    return jsonify({"job": job}), 202


@bp.post("/channels/<channel_id>/videos/designation/plan")
@requires_auth
def api_availability_video_designation_plan(channel_id: str):
    data = _optional_body()
    if not set(data).issubset({"video_ids", "action"}) or "video_ids" not in data:
        raise availability.AvailabilityError(
            "Video designation plan requires video_ids and optionally action"
        )
    return jsonify({
        "plan": video_designation.build_plan(
            channel_id,
            data["video_ids"],
            action=data.get("action", "designate"),
            actor=_actor(),
        )
    })


@bp.post("/channels/<channel_id>/videos/designation/apply")
@requires_auth
def api_availability_video_designation_apply(channel_id: str):
    data = _optional_body()
    if not {"plan_token", "confirmation"}.issubset(data) or not set(data).issubset({
        "plan_token", "confirmation"
    }):
        raise availability.AvailabilityError(
            "Video designation apply requires plan_token and confirmation"
        )
    return jsonify({
        "result": video_designation.apply_plan(
            channel_id,
            data["plan_token"],
            data["confirmation"],
            actor=_actor(),
        )
    })


@bp.get("/channels/<channel_id>/observations")
@requires_auth
def api_availability_observations(channel_id: str):
    subject_type = request.args.get("subject_type", "channel")
    return jsonify({
        "observations": availability.list_observations(
            channel_id,
            subject_type=subject_type,
            subject_id=request.args.get("subject_id") or None,
            limit=request.args.get("limit", "100"),
        )
    })


@bp.get("/jobs/<job_id>")
@requires_auth
def api_availability_job(job_id: str):
    return jsonify({"job": availability.get_job(job_id)})


@bp.post("/jobs/<job_id>/cancel")
@requires_auth
def api_availability_cancel_job(job_id: str):
    data = _optional_body()
    if data:
        raise availability.AvailabilityError("Cancel does not accept options")
    return jsonify({"job": availability.cancel_job(job_id)}), 202
