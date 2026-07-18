"""Authenticated recovered-channel investigation and organization API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..auth import requires_auth
from .. import recovered_channels, reference_repair


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


@bp.errorhandler(reference_repair.ReferenceRepairError)
def _repair_error(exc: reference_repair.ReferenceRepairError):
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


@bp.post("/attach-plan")
@requires_auth
def api_recovered_channel_attach_plan():
    data = _body()
    if not set(data).issubset({"source_channel_id", "target_component"}) or "source_channel_id" not in data:
        raise recovered_channels.RecoveredChannelError(
            "Attach planning accepts source_channel_id and optional target_component only"
        )
    return jsonify({
        "plan": recovered_channels.build_attach_plan(
            data.get("source_channel_id"),
            data.get("target_component"),
            actor=_actor(),
        )
    })


@bp.post("/attach")
@requires_auth
def api_recovered_channel_attach():
    data = _body()
    if set(data) != {"plan_token", "confirmation"}:
        raise recovered_channels.RecoveredChannelError(
            "Attach requires plan_token and exact confirmation only"
        )
    return jsonify(
        recovered_channels.apply_attach_plan(
            data.get("plan_token"), data.get("confirmation"), actor=_actor()
        )
    ), 201


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


@bp.get("/<case_id>/items/<item_id>/repair-references")
@requires_auth
def api_recovered_channel_repair_references(case_id: str, item_id: str):
    return jsonify(reference_repair.ranked_references(case_id, item_id))


@bp.get("/<case_id>/reference-repairs")
@requires_auth
def api_recovered_channel_reference_repairs(case_id: str):
    return jsonify({
        "repairs": reference_repair.list_repairs(
            case_id, request.args.get("damaged_item_id") or None
        )
    })


@bp.post("/<case_id>/reference-repairs")
@requires_auth
def api_recovered_channel_reference_repair_start(case_id: str):
    data = _body()
    if set(data) != {"damaged_item_id", "reference_item_id"}:
        raise reference_repair.ReferenceRepairError(
            "Start repair requires damaged_item_id and reference_item_id only"
        )
    repair = reference_repair.start_repair(
        case_id,
        data.get("damaged_item_id"),
        data.get("reference_item_id"),
        actor=_actor(),
    )
    return jsonify({"repair": repair}), 202


@bp.get("/<case_id>/reference-repairs/<repair_id>")
@requires_auth
def api_recovered_channel_reference_repair(case_id: str, repair_id: str):
    return jsonify({"repair": reference_repair.get_repair(case_id, repair_id)})


@bp.post("/<case_id>/reference-repairs/<repair_id>/cancel")
@requires_auth
def api_recovered_channel_reference_repair_cancel(case_id: str, repair_id: str):
    data = request.get_json(silent=True)
    if data not in (None, {}):
        raise reference_repair.ReferenceRepairError("Cancel does not accept options")
    return jsonify({
        "repair": reference_repair.cancel_repair(
            case_id, repair_id, actor=_actor()
        )
    }), 202


@bp.post("/<case_id>/reference-repairs/<repair_id>/retry")
@requires_auth
def api_recovered_channel_reference_repair_retry(case_id: str, repair_id: str):
    data = request.get_json(silent=True)
    if data not in (None, {}):
        raise reference_repair.ReferenceRepairError("Retry does not accept options")
    return jsonify({
        "repair": reference_repair.retry_repair(
            case_id, repair_id, actor=_actor()
        )
    }), 202


@bp.post("/<case_id>/reference-repairs/<repair_id>/<action>-plan")
@requires_auth
def api_recovered_channel_reference_repair_plan(
    case_id: str, repair_id: str, action: str
):
    data = request.get_json(silent=True)
    if data not in (None, {}):
        raise reference_repair.ReferenceRepairError("Action planning accepts no options")
    return jsonify({
        "plan": reference_repair.build_action_plan(
            case_id, repair_id, action, actor=_actor()
        )
    })


@bp.post("/<case_id>/reference-repairs/<repair_id>/<action>")
@requires_auth
def api_recovered_channel_reference_repair_apply(
    case_id: str, repair_id: str, action: str
):
    data = _body()
    if set(data) != {"plan_token", "confirmation"}:
        raise reference_repair.ReferenceRepairError(
            "Apply requires plan_token and exact confirmation only"
        )
    return jsonify(reference_repair.apply_action(
        case_id, repair_id, action, data.get("plan_token"),
        data.get("confirmation"), actor=_actor()
    ))


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
