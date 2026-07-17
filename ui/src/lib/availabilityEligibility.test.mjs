import assert from "node:assert/strict";
import test from "node:test";

import {
    normalizeTerminationEligibility,
    terminationEligibilityMessage,
} from "./availabilityEligibility.js";

test("normalizes a complete server threshold without changing eligibility", () => {
    const normalized = normalizeTerminationEligibility({
        eligible: false,
        status: "awaiting_confirmation",
        required_observations: 2,
        matching_observations: 3,
        qualifying_observations: 1,
        remaining_observations: 1,
        minimum_separation_hours: 24,
        reason_code: "channel_terminated",
        first_observed_at: "2026-07-16T12:00:00+00:00",
        latest_observed_at: "2026-07-16T18:00:00+00:00",
        next_qualifying_check_at: "2026-07-17T12:00:00+00:00",
        counting_observations: [{
            observation_id: 41,
            observed_at: "2026-07-16T12:00:00+00:00",
            reason_code: "channel_terminated",
        }],
        api_corroboration_required: false,
    }, true);

    assert.equal(normalized.eligible, false);
    assert.equal(normalized.thresholdProgress, 1);
    assert.equal(normalized.matchingObservations, 3);
    assert.deepEqual(normalized.countingObservationIds, ["41"]);
    assert.equal(normalized.apiCorroborationRequired, false);
});

test("derives the legacy next-check time but never invents candidate eligibility", () => {
    const normalized = normalizeTerminationEligibility({
        eligible: false,
        required_observations: 2,
        matching_observations: 1,
        minimum_separation_hours: 24,
        first_observed_at: "2026-07-16T12:00:00Z",
        reason_code: "channel_terminated",
    });

    assert.equal(normalized.eligible, false);
    assert.equal(normalized.thresholdProgress, 1);
    assert.equal(normalized.nextQualifyingAt, "2026-07-17T12:00:00.000Z");
});

test("supports a legacy candidate while identifying missing diagnostics", () => {
    const normalized = normalizeTerminationEligibility(undefined, true);
    assert.equal(normalized.eligible, true);
    assert.equal(normalized.status, "eligible");
    assert.equal(normalized.diagnosticsAvailable, false);
});

test("maps the backend status vocabulary without inventing progress", () => {
    const noEvidence = normalizeTerminationEligibility({
        status: "not_explicit",
        matching_observations: 0,
        qualifying_observations: 0,
        remaining_observations: 2,
    });
    assert.equal(noEvidence.status, "no_matching_evidence");
    assert.equal(noEvidence.thresholdProgress, 0);
    assert.equal(noEvidence.qualifyingObservationsNeeded, 2);

    const latestNotExplicit = normalizeTerminationEligibility({
        status: "not_explicit",
        matching_observations: 1,
        qualifying_observations: 0,
    });
    assert.equal(latestNotExplicit.status, "latest_not_explicit");
    assert.equal(latestNotExplicit.thresholdProgress, 0);

    assert.equal(
        normalizeTerminationEligibility({
            status: "conflicting_or_accessible",
        }).status,
        "evidence_reset",
    );
    assert.equal(
        normalizeTerminationEligibility({
            status: "current_explicit",
            qualifying_observations: 1,
        }).status,
        "waiting_for_interval",
    );
});

test("formats waiting, check-now, reset, latest-state, and eligible messages", () => {
    const waiting = terminationEligibilityMessage({
        status: "waiting_for_interval",
        nextQualifyingAt: "2026-07-18T00:00:00Z",
    }, Date.parse("2026-07-17T00:00:00Z"));
    assert.match(waiting.title, /Waiting/);

    const ready = terminationEligibilityMessage({
        status: "waiting_for_interval",
        nextQualifyingAt: "2026-07-16T00:00:00Z",
    }, Date.parse("2026-07-17T00:00:00Z"));
    assert.equal(ready.followupReady, true);
    assert.match(ready.title, /qualify now/);

    assert.match(
        terminationEligibilityMessage({ status: "evidence_reset" }).title,
        /reset/,
    );
    assert.match(
        terminationEligibilityMessage({ status: "latest_not_explicit" }).title,
        /not explicit/,
    );
    assert.equal(
        terminationEligibilityMessage({ status: "eligible", eligible: true }).tone,
        "good",
    );
    assert.match(
        terminationEligibilityMessage({ status: "invalid_timestamp_evidence" }).title,
        /Timestamp/,
    );
});

test("preserves the server-selected qualifying pair without inventing rows", () => {
    const normalized = normalizeTerminationEligibility({
        eligible: true,
        status: "eligible",
        required_observations: 2,
        threshold_progress: 2,
        matching_observations: 3,
        counting_observations: [
            { observation_id: 1, observed_at: "2026-07-15T00:00:00Z" },
            { observation_id: 3, observed_at: "2026-07-16T01:00:00Z" },
        ],
    });
    assert.deepEqual(normalized.countingObservationIds, ["1", "3"]);
});
