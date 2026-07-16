"""Authenticated recovered-channel investigation and organization API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..auth import requires_auth
from .. import recovered_channels


bp = Blueprint(
    "recovered_channels",
    __name__,
    url_prefix="/api/recovery/channel-cases",
)


def _actor() -> str:
    return str(session.get("username") or "authenticated user")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise recovered_channels.RecoveredChannelError(
            "Request body must be a JSON object"
        )
    return data


@bp.errorhandler(recovered_channels.RecoveredChannelError)
def _workflow_error(exc: recovered_channels.RecoveredChannelError):
    return jsonify({"error": str(exc)}), exc.status


@bp.get("")
@requires_auth
def api_recovered_channel_cases():
    return jsonify({"cases": recovered_channels.list_cases()})


@bp.post("")
@requires_auth
def api_recovered_channel_create():
    data = _body()
    case = recovered_channels.create_case(
        data.get("source_channel_id"),
        data.get("target_channel_name"),
        actor=_actor(),
    )
    return jsonify({"case": case}), 201


@bp.get("/<case_id>")
@requires_auth
def api_recovered_channel_case(case_id: str):
    return jsonify(recovered_channels.get_case(case_id))


@bp.patch("/<case_id>")
@requires_auth
def api_recovered_channel_update(case_id: str):
    case = recovered_channels.update_case(case_id, _body(), actor=_actor())
    return jsonify({"case": case})


@bp.get("/<case_id>/items")
@requires_auth
def api_recovered_channel_items(case_id: str):
    try:
        page = int(request.args.get("page", "1"))
        per_page = int(request.args.get("per_page", "100"))
    except ValueError:
        raise recovered_channels.RecoveredChannelError(
            "page and per_page must be integers"
        ) from None
    return jsonify(recovered_channels.list_items(
        case_id,
        page=page,
        per_page=per_page,
        health=request.args.get("health") or None,
        decision=request.args.get("decision") or None,
    ))


@bp.patch("/<case_id>/items/<item_id>")
@requires_auth
def api_recovered_channel_item(case_id: str, item_id: str):
    item = recovered_channels.update_item(
        case_id, item_id, _body(), actor=_actor()
    )
    return jsonify({"item": item})


@bp.post("/<case_id>/items/<item_id>/repair-link")
@requires_auth
def api_recovered_channel_repair_link(case_id: str, item_id: str):
    data = request.get_json(silent=True)
    if data not in (None, {}):
        raise recovered_channels.RecoveredChannelError(
            "Repair Link does not accept client paths or options"
        )
    return jsonify(
        recovered_channels.repair_recovered_link(
            case_id, item_id, actor=_actor()
        )
    )


@bp.post("/<case_id>/decisions")
@requires_auth
def api_recovered_channel_decisions(case_id: str):
    data = _body()
    updated = recovered_channels.bulk_decision(
        case_id,
        data.get("item_ids"),
        data.get("decision"),
        actor=_actor(),
    )
    return jsonify({"updated": updated})


@bp.post("/<case_id>/inspect")
@requires_auth
def api_recovered_channel_inspect(case_id: str):
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise recovered_channels.RecoveredChannelError(
            "Request body must be a JSON object"
        )
    rescan = data.get("rescan", False)
    if type(rescan) is not bool:
        raise recovered_channels.RecoveredChannelError("rescan must be a boolean")
    job = recovered_channels.start_inspection(
        case_id,
        rescan=rescan,
        actor=_actor(),
        background=True,
    )
    return jsonify({"job": job}), 202


@bp.get("/<case_id>/jobs/<job_id>")
@requires_auth
def api_recovered_channel_job(case_id: str, job_id: str):
    return jsonify({"job": recovered_channels.get_job(case_id, job_id)})


@bp.post("/<case_id>/jobs/<job_id>/cancel")
@requires_auth
def api_recovered_channel_cancel(case_id: str, job_id: str):
    return jsonify({
        "job": recovered_channels.cancel_job(
            case_id, job_id, actor=_actor()
        )
    }), 202


@bp.post("/<case_id>/organization-plan")
@requires_auth
def api_recovered_channel_plan(case_id: str):
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise recovered_channels.RecoveredChannelError(
            "Request body must be a JSON object"
        )
    if not set(data).issubset({"scope"}):
        raise recovered_channels.RecoveredChannelError(
            "Organization plan accepts only scope"
        )
    scope = data.get("scope", "approved")
    return jsonify({
        "plan": recovered_channels.build_organization_plan(
            case_id, scope=scope
        )
    })


@bp.post("/<case_id>/organize")
@requires_auth
def api_recovered_channel_organize(case_id: str):
    data = _body()
    if not set(data).issubset({"plan_token", "scope"}):
        raise recovered_channels.RecoveredChannelError(
            "Organize accepts only plan_token and scope"
        )
    job = recovered_channels.start_organization(
        case_id,
        data.get("plan_token"),
        actor=_actor(),
        background=True,
        scope=data.get("scope", "approved"),
    )
    return jsonify({"job": job}), 202
