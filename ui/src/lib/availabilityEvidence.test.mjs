import assert from "node:assert/strict";
import test from "node:test";

import {
    boundedEvidenceJson,
    normalizeAvailabilityEvidence,
    preservedObservationCount,
    structuredEvidenceSignals,
} from "./availabilityEvidence.js";

test("normalizes identifiers and keeps nested provider evidence", () => {
    const observation = normalizeAvailabilityEvidence({
        observation_id: 42,
        job_id: "job-1",
        subject_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
        normalized_state: "terminated_explicit",
        reason_code: "channel_terminated",
        raw_reason: "This account has been terminated",
        tool_version: "2026.03.17",
        evidence: {
            yt_dlp: {
                reported_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
                availability: "public",
                live_status: "not_live",
            },
            youtube_api: { evidence: { returned_ids: ["UCaaaaaaaaaaaaaaaaaaaaaa"] } },
            api_resource_exists: true,
        },
    });

    assert.equal(observation.id, "42");
    assert.equal(observation.jobId, "job-1");
    assert.equal(observation.reasonCode, "channel_terminated");
    assert.equal(observation.explicit, true);
    assert.equal(observation.structured.api_resource_exists, true);
});

test("extracts bounded identity, API, availability, and live signals", () => {
    const signals = structuredEvidenceSignals({
        yt_dlp: {
            reported_id: "video123456",
            channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
            availability: "needs_auth",
            live_status: "is_upcoming",
        },
        youtube_api: { evidence: { returned_ids: ["video123456"] } },
        api_resource_exists: false,
    });
    const keys = signals.map((signal) => signal.key);

    assert.ok(keys.includes("yt_dlp.reported_id"));
    assert.ok(keys.includes("yt_dlp.channel_id"));
    assert.ok(keys.includes("yt_dlp.availability"));
    assert.ok(keys.includes("yt_dlp.live_status"));
    assert.ok(keys.includes("youtube_api.evidence.returned_ids"));
    assert.ok(keys.includes("api_resource_exists"));
});

test("preserves the registry count when a limited history has no total", () => {
    assert.equal(preservedObservationCount(83, 50, undefined), 83);
    assert.equal(preservedObservationCount(2, 5, undefined), 5);
    assert.equal(preservedObservationCount(83, 50, 97), 97);
});

test("bounds structured JSON rendered into the disclosure", () => {
    const rendered = boundedEvidenceJson({ payload: "x".repeat(100) }, 40);
    assert.ok(rendered.length < 100);
    assert.match(rendered, /truncated/);
});
