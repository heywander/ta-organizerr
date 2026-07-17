const INTERESTING_EVIDENCE_KEYS = new Set([
    "reported_id",
    "channel_id",
    "returned_id",
    "returned_ids",
    "api_resource_exists",
    "availability",
    "live_status",
    "item_count",
]);

function textValue(...values) {
    for (const value of values) {
        if (value !== null && value !== undefined && String(value).trim()) {
            return String(value);
        }
    }
    return "";
}

function boolValue(...values) {
    for (const value of values) {
        if (typeof value === "boolean") return value;
        if (value === 1 || value === "1" || value === "true") return true;
        if (value === 0 || value === "0" || value === "false") return false;
    }
    return false;
}

export function normalizeAvailabilityEvidence(raw, index = 0) {
    const structured =
        raw?.evidence && typeof raw.evidence === "object" && !Array.isArray(raw.evidence)
            ? raw.evidence
            : {};
    const normalizedState = textValue(
        raw?.normalized_state,
        raw?.state,
        raw?.classification,
        "unknown",
    );
    return {
        id: textValue(raw?.observation_id, raw?.id, index),
        jobId: textValue(raw?.job_id),
        subjectId: textValue(raw?.subject_id),
        state: normalizedState,
        reasonCode: textValue(raw?.reason_code),
        provider: textValue(raw?.provider, raw?.source, "yt-dlp"),
        rawReason: textValue(
            raw?.raw_reason,
            raw?.reason,
            raw?.raw_message,
            raw?.message,
        ),
        detail: textValue(raw?.detail, raw?.subreason),
        observedAt: textValue(
            raw?.observed_at,
            raw?.created_at,
            raw?.checked_at,
            raw?.timestamp,
        ),
        explicit: boolValue(
            raw?.explicit,
            raw?.is_explicit,
            raw?.explicit_termination,
        ) || normalizedState === "terminated_explicit",
        toolVersion: textValue(raw?.tool_version),
        structured,
    };
}

function displayValue(value) {
    if (Array.isArray(value)) return value.map(String).join(", ");
    if (value === null) return "null";
    return String(value);
}

export function structuredEvidenceSignals(structured, maxSignals = 16) {
    const signals = [];
    const visit = (value, path, depth) => {
        if (signals.length >= maxSignals || depth > 6 || value === null) return;
        if (Array.isArray(value)) return;
        if (typeof value !== "object") return;
        for (const [key, child] of Object.entries(value)) {
            if (signals.length >= maxSignals) break;
            const childPath = path ? `${path}.${key}` : key;
            if (
                INTERESTING_EVIDENCE_KEYS.has(key) &&
                (child === null ||
                    ["string", "number", "boolean"].includes(typeof child) ||
                    (Array.isArray(child) && child.every((item) =>
                        ["string", "number", "boolean"].includes(typeof item),
                    )))
            ) {
                signals.push({ key: childPath, value: displayValue(child) });
            }
            visit(child, childPath, depth + 1);
        }
    };
    visit(structured, "", 0);
    return signals;
}

export function boundedEvidenceJson(structured, maxCharacters = 12000) {
    let rendered;
    try {
        rendered = JSON.stringify(structured ?? {}, null, 2);
    } catch {
        return "Structured evidence could not be rendered.";
    }
    if (rendered.length <= maxCharacters) return rendered;
    return `${rendered.slice(0, maxCharacters)}\n… evidence display truncated …`;
}

export function preservedObservationCount(currentCount, returnedCount, endpointTotal) {
    const current = Number.isFinite(Number(currentCount)) ? Number(currentCount) : 0;
    const returned = Number.isFinite(Number(returnedCount)) ? Number(returnedCount) : 0;
    if (
        endpointTotal !== undefined &&
        endpointTotal !== null &&
        endpointTotal !== "" &&
        Number.isFinite(Number(endpointTotal))
    ) {
        return Math.max(0, Number(endpointTotal));
    }
    return Math.max(current, returned);
}
