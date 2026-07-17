function finiteNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function textValue(...values) {
    for (const value of values) {
        if (value !== null && value !== undefined && String(value).trim()) {
            return String(value);
        }
    }
    return "";
}

function booleanValue(value, fallback = false) {
    if (typeof value === "boolean") return value;
    if (value === 1 || value === "1" || value === "true") return true;
    if (value === 0 || value === "0" || value === "false") return false;
    return fallback;
}

function addHours(value, hours) {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "";
    return new Date(timestamp + hours * 60 * 60 * 1000).toISOString();
}

function normalizeCountingObservation(raw) {
    if (!raw || typeof raw !== "object") return null;
    const id = textValue(raw.observation_id, raw.id);
    if (!id) return null;
    return {
        id,
        observedAt: textValue(raw.observed_at, raw.timestamp),
        reasonCode: textValue(raw.reason_code),
    };
}

function canonicalStatus(value) {
    const status = textValue(value).toLowerCase().replace(/[^a-z0-9]+/g, "_");
    const aliases = {
        no_explicit_evidence: "no_matching_evidence",
        awaiting_explicit_observation: "no_matching_evidence",
        awaiting_second_observation: "waiting_for_interval",
        ready_for_followup_check: "ready_for_followup",
        latest_observation_not_explicit: "latest_not_explicit",
        reset_by_accessible_observation: "evidence_reset",
        conflicting_or_accessible: "evidence_reset",
        current_explicit: "waiting_for_interval",
        awaiting_confirmation: "waiting_for_interval",
    };
    return aliases[status] || status;
}

export function normalizeTerminationEligibility(raw, fallbackCandidate = false) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
    const required = Math.max(
        1,
        finiteNumber(source?.required_observations, 2),
    );
    const minimumHours = Math.max(
        0,
        finiteNumber(source?.minimum_separation_hours, 24),
    );
    const matching = Math.max(0, finiteNumber(source?.matching_observations, 0));
    const eligible = booleanValue(source?.eligible, Boolean(fallbackCandidate));
    const firstObservedAt = textValue(
        source?.first_observed_at,
        source?.first_matching_observed_at,
    );
    const latestObservedAt = textValue(
        source?.latest_observed_at,
        source?.latest_matching_observed_at,
    );
    const nextQualifyingAt = textValue(
        source?.next_qualifying_observation_at,
        source?.next_qualifying_check_at,
        source?.next_qualifying_at,
        !eligible && firstObservedAt ? addHours(firstObservedAt, minimumHours) : "",
    );
    const countingSource = Array.isArray(source?.counting_observations)
        ? source.counting_observations
        : Array.isArray(source?.qualifying_observations)
          ? source.qualifying_observations
          : [];
    const countingObservations = countingSource
        .map(normalizeCountingObservation)
        .filter(Boolean)
        .slice(0, required);
    const qualifying = Math.max(
        0,
        finiteNumber(source?.qualifying_observations, matching > 0 ? 1 : 0),
    );
    const fallbackProgress = eligible ? required : qualifying;
    const thresholdProgress = Math.max(
        0,
        Math.min(
            required,
            finiteNumber(source?.threshold_progress, fallbackProgress),
        ),
    );
    let status = canonicalStatus(source?.status);
    if (status === "not_explicit") {
        status = matching > 0 ? "latest_not_explicit" : "no_matching_evidence";
    }
    if (!status) {
        if (!source) status = fallbackCandidate ? "eligible" : "diagnostics_unavailable";
        else if (eligible) status = "eligible";
        else if (matching > 0 || firstObservedAt) status = "waiting_for_interval";
        else status = "no_matching_evidence";
    }

    return {
        diagnosticsAvailable: Boolean(source),
        eligible,
        status,
        requiredObservations: required,
        thresholdProgress,
        matchingObservations: matching,
        qualifyingObservationsNeeded: Math.max(
            0,
            finiteNumber(
                source?.qualifying_observations_needed,
                finiteNumber(
                    source?.remaining_observations,
                    eligible ? 0 : thresholdProgress > 0 ? 1 : required,
                ),
            ),
        ),
        minimumSeparationHours: minimumHours,
        matchingReasonCode: textValue(
            source?.matching_reason_code,
            source?.reason_code,
        ),
        firstObservedAt,
        latestObservedAt,
        nextQualifyingAt,
        qualifiedAt: textValue(source?.qualified_at),
        countingObservations,
        countingObservationIds: countingObservations.map((item) => item.id),
        lastBlockingObservation:
            source?.last_blocking_observation &&
            typeof source.last_blocking_observation === "object"
                ? {
                      id: textValue(
                          source.last_blocking_observation.observation_id,
                          source.last_blocking_observation.id,
                      ),
                      state: textValue(source.last_blocking_observation.state),
                      observedAt: textValue(
                          source.last_blocking_observation.observed_at,
                      ),
                  }
                : null,
        apiCorroborationRequired: booleanValue(
            source?.api_corroboration_required,
            false,
        ),
    };
}

export function terminationEligibilityMessage(eligibility, now = Date.now()) {
    const nextTimestamp = Date.parse(eligibility?.nextQualifyingAt || "");
    const followupReady = Number.isFinite(nextTimestamp) && nextTimestamp <= Number(now);
    const status = canonicalStatus(eligibility?.status);

    if (eligibility?.eligible || status === "eligible") {
        return {
            tone: "good",
            title: "Evidence threshold satisfied",
            detail: "Two matching explicit observations meet the required separation. Administrator review is available.",
            followupReady: false,
        };
    }
    if (status === "evidence_reset") {
        return {
            tone: "warning",
            title: "Evidence sequence reset",
            detail: "A later accessible or conflicting observation prevents earlier explicit evidence from counting.",
            followupReady: false,
        };
    }
    if (status === "latest_not_explicit") {
        return {
            tone: "warning",
            title: "Latest result is not explicit",
            detail: "Matching history exists, but the current channel state is not explicit termination evidence. Check again before review.",
            followupReady,
        };
    }
    if (status === "invalid_timestamp_evidence") {
        return {
            tone: "warning",
            title: "Timestamp evidence cannot qualify",
            detail: "One or more observations has a malformed or future timestamp. Those rows are excluded and cannot unlock review.",
            followupReady: false,
        };
    }
    if (status === "ready_for_followup" || followupReady) {
        return {
            tone: "warning",
            title: "A follow-up check can qualify now",
            detail: "The minimum interval has passed. A new matching explicit response can advance the threshold; other results remain non-qualifying.",
            followupReady: true,
        };
    }
    if (status === "waiting_for_interval") {
        return {
            tone: "muted",
            title: "Waiting for the minimum interval",
            detail: "At least one matching explicit observation exists. A later matching result must be separated by the full interval.",
            followupReady: false,
        };
    }
    if (status === "diagnostics_unavailable") {
        return {
            tone: "muted",
            title: "Threshold details unavailable",
            detail: "This server returned only the legacy candidate result. The server still controls whether review is unlocked.",
            followupReady: false,
        };
    }
    return {
        tone: "muted",
        title: "No matching explicit evidence yet",
        detail: "Two matching explicit channel responses are required. Generic unavailability, video results, and API absence do not count.",
        followupReady: false,
    };
}
