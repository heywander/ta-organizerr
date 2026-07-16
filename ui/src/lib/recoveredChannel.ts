export type RecoveredChannelStep =
    | "channel"
    | "inspect"
    | "review"
    | "plan"
    | "complete";

export type ReviewDecision = "undecided" | "approved" | "held";

export type PreviewMode = "video" | "audio";

export type RecoveredChannelItem = {
    id: string;
    video_id: string;
    path: string;
    filename: string;
    original_suffix: string;
    source_channel_id?: string;
    inspection_status: string;
    assessment: string;
    severity: string;
    bucket: string;
    decision: ReviewDecision;
    destination_path?: string;
    organized_path?: string;
    expected_organized_path?: string;
    link_status: string;
    repairable_link: boolean;
    link_target?: string;
    actual_link_target?: string;
    symlink_target?: string;
    source_integrity_error?: string;
    database_owner_valid?: boolean;
    database_owner_error?: string;
    size_bytes?: number | null;
    duration_seconds?: number | null;
    readable_duration_seconds?: number | null;
    expected_duration_seconds?: number | null;
    readable_percent?: number | null;
    progress_percent?: number | null;
    streams?: Array<Record<string, unknown>>;
    findings: string[];
    warnings: string[];
    error?: string;
    sha256?: string;
    [key: string]: unknown;
};

export type RecoveredChannelInvestigation = {
    id: string;
    source_channel_id: string;
    channel_name: string;
    target_folder_name: string;
    target_folder_path?: string;
    status: string;
    phase?: string;
    message?: string;
    total: number;
    completed: number;
    failed: number;
    repair_needed: number;
    items: RecoveredChannelItem[];
    active_job?: Record<string, unknown> | null;
    counts?: Record<string, number>;
    [key: string]: unknown;
};

export type RecoveredChannelPlanItem = {
    video_id: string;
    source_path: string;
    destination_path: string;
    link_target?: string;
    status: string;
    error?: string;
    [key: string]: unknown;
};

export type RecoveredChannelPlan = {
    token?: string;
    plan_token?: string;
    approved_count: number;
    ready_count: number;
    blocked_count: number;
    target_folder_name?: string;
    target_folder_path?: string;
    items: RecoveredChannelPlanItem[];
    [key: string]: unknown;
};

export type RecoveredChannelResult = {
    success_count: number;
    failed_count: number;
    held_count: number;
    items: RecoveredChannelPlanItem[];
    message?: string;
    [key: string]: unknown;
};

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord {
    return value && typeof value === "object" && !Array.isArray(value)
        ? (value as UnknownRecord)
        : {};
}

function text(value: unknown, fallback = ""): string {
    return typeof value === "string" ? value : fallback;
}

function numberOrNull(value: unknown): number | null {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function boolean(value: unknown, fallback = false): boolean {
    if (value === true || value === "true" || value === 1) return true;
    if (value === false || value === "false" || value === 0) return false;
    return fallback;
}

function stringList(value: unknown): string[] {
    if (Array.isArray(value)) return value.filter((item) => typeof item === "string");
    return typeof value === "string" && value ? [value] : [];
}

function findingList(value: unknown): string[] {
    if (!Array.isArray(value)) return stringList(value);
    const values: string[] = [];
    for (const entry of value) {
        if (typeof entry === "string" && entry) {
            values.push(entry);
            continue;
        }
        const item = record(entry);
        const message = text(item.message || item.summary || item.code);
        if (message) values.push(message);
        for (const detail of stringList(item.details)) {
            if (detail && detail !== message) values.push(detail);
        }
    }
    return values;
}

function decisionFrom(raw: UnknownRecord): ReviewDecision {
    const value = text(raw.decision || raw.review_decision).toLowerCase();
    if (value === "approved" || raw.approved === true) return "approved";
    if (value === "held" || value === "hold" || raw.held === true) return "held";
    return "undecided";
}

export function normalizeRecoveredChannelItem(
    value: unknown,
    sourceChannelId = "",
): RecoveredChannelItem {
    const raw = record(value);
    const diagnosis = record(raw.diagnosis || raw.report || raw.inspection);
    const source = record(diagnosis.source || raw.source_report);
    const sourceDecode = record(source.decode);
    const link = record(raw.link_health || raw.link || raw.published_link);
    const path = text(
        raw.path || raw.source_path || source.path || source.resolved_path,
    );
    const filename = text(raw.filename || source.filename) || basename(path);
    const suffix = text(raw.original_suffix || raw.suffix) || suffixFrom(filename);
    const organizedPath = text(
        raw.organized_path || raw.published_path || link.organized_path,
    );
    const expectedOrganizedPath = text(
        raw.expected_organized_path ||
            raw.expected_destination_path ||
            raw.expected_link_path ||
            link.expected_organized_path ||
            link.expected_path ||
            organizedPath,
    );
    const reportedLinkStatus = text(
        raw.link_status ||
            raw.organized_link_status ||
            raw.publication_status ||
            link.link_status ||
            link.status,
        organizedPath ? "unknown" : "unpublished",
    )
        .trim()
        .toLowerCase()
        .replaceAll("-", "_");
    const sourceIntegrityError = text(
        raw.source_integrity_error ||
            raw.source_error ||
            link.source_integrity_error,
    );
    const ownerValue = raw.database_owner_valid ?? link.database_owner_valid;
    const databaseOwnerValid =
        ownerValue === undefined ? undefined : boolean(ownerValue);
    const databaseOwnerError = text(
        raw.database_owner_error ||
            raw.owner_error ||
            link.database_owner_error,
    );
    const linkStatus =
        reportedLinkStatus === "healthy" && sourceIntegrityError
            ? "source_invalid"
            : reportedLinkStatus === "healthy" && databaseOwnerValid === false
              ? "owner_invalid"
              : reportedLinkStatus;
    const findings = [
        ...findingList(raw.findings),
        ...findingList(diagnosis.findings),
        ...findingList(source.errors),
    ];
    const warnings = [
        ...stringList(raw.warnings),
        ...stringList(diagnosis.warnings),
        ...stringList(source.warnings),
    ];
    return {
        ...raw,
        id: text(raw.id || raw.item_id),
        video_id: text(raw.video_id || source.video_id),
        path,
        filename,
        original_suffix: suffix,
        source_channel_id:
            text(raw.source_channel_id) || sourceChannelId || undefined,
        inspection_status: text(
            raw.inspection_status || raw.scan_status || raw.status,
            "pending",
        ).toLowerCase(),
        assessment: text(
            raw.assessment || diagnosis.assessment,
            "inconclusive",
        ).toLowerCase(),
        severity: text(
            raw.health_tier || raw.severity || diagnosis.severity,
        ).toLowerCase(),
        bucket: text(
            raw.health_tier || raw.bucket || raw.condition_bucket,
        ).toLowerCase(),
        decision: decisionFrom(raw),
        destination_path:
            text(raw.destination_path || raw.destination) || undefined,
        organized_path: organizedPath || undefined,
        expected_organized_path: expectedOrganizedPath || undefined,
        link_status: linkStatus,
        repairable_link: boolean(
            raw.repairable_link ??
                raw.link_repairable ??
                raw.can_repair_link ??
                link.repairable_link ??
                link.repairable,
        ),
        link_target:
            text(
                raw.link_target ||
                    raw.expected_link_target ||
                    link.link_target ||
                    link.expected_target,
            ) || undefined,
        actual_link_target:
            text(
                raw.actual_link_target ||
                    raw.actual_symlink_target ||
                    raw.symlink_target ||
                    link.actual_link_target ||
                    link.actual_target ||
                    link.symlink_target,
            ) || undefined,
        symlink_target:
            text(
                raw.symlink_target ||
                    raw.actual_symlink_target ||
                    raw.actual_link_target ||
                    link.symlink_target ||
                    link.actual_target,
            ) || undefined,
        source_integrity_error: sourceIntegrityError || undefined,
        database_owner_valid: databaseOwnerValid,
        database_owner_error: databaseOwnerError || undefined,
        size_bytes: numberOrNull(raw.size_bytes || source.size_bytes),
        duration_seconds: numberOrNull(
            raw.duration_seconds || source.duration_seconds,
        ),
        readable_duration_seconds: numberOrNull(
            raw.readable_duration_seconds || source.readable_duration_seconds,
        ),
        expected_duration_seconds: numberOrNull(
            raw.expected_duration_seconds ?? diagnosis.expected_duration_seconds,
        ),
        readable_percent: numberOrNull(
            raw.readable_percent || diagnosis.readable_percent,
        ),
        progress_percent: numberOrNull(
            raw.progress_percent || raw.progress,
        ),
        streams: Array.isArray(raw.streams)
            ? (raw.streams as Array<Record<string, unknown>>)
            : Array.isArray(source.streams)
              ? (source.streams as Array<Record<string, unknown>>)
              : Array.isArray(sourceDecode.streams)
                ? (sourceDecode.streams as Array<Record<string, unknown>>)
                : [],
        findings: Array.from(new Set(findings)),
        warnings: Array.from(new Set(warnings)),
        error: text(raw.error || diagnosis.error) || undefined,
        sha256: text(raw.sha256 || diagnosis.input_sha256 || source.sha256) || undefined,
    };
}

export function normalizeRecoveredChannelInvestigation(
    value: unknown,
): RecoveredChannelInvestigation {
    const envelope = record(value);
    const raw = record(
        envelope.case ||
            envelope.investigation ||
            envelope.session ||
            envelope.workflow ||
            value,
    );
    const sourceChannelId = text(raw.source_channel_id || raw.channel_id);
    const rawItems = Array.isArray(raw.items)
        ? raw.items
        : Array.isArray(raw.videos)
          ? raw.videos
          : [];
    const items = rawItems.map((item) =>
        normalizeRecoveredChannelItem(item, sourceChannelId),
    );
    const rawCounts = record(envelope.counts || raw.counts);
    const rawLinkHealth = record(
        envelope.link_health || raw.link_health || raw.link_health_counts,
    );
    const counts: Record<string, number> = {};
    for (const [key, count] of Object.entries(rawCounts)) {
        const parsed = numberOrNull(count);
        if (parsed !== null) counts[key] = parsed;
    }
    const countedTotal = numberOrNull(rawCounts.total);
    const countedPending = numberOrNull(rawCounts.pending);
    const countedInspecting = numberOrNull(rawCounts.inspecting);
    const countedFinished =
        countedTotal !== null && countedPending !== null && countedInspecting !== null
            ? Math.max(0, countedTotal - countedPending - countedInspecting)
            : null;
    const completed =
        countedFinished ??
        numberOrNull(
            raw.completed ||
                raw.completed_count ||
                raw.inspected_count ||
                rawCounts.completed ||
                rawCounts.inspected,
        ) ??
        items.filter((item) => isInspectionComplete(item)).length;
    const failed =
        numberOrNull(raw.failed || raw.failed_count || rawCounts.failed) ??
        items.filter((item) => item.inspection_status === "failed").length;
    return {
        ...raw,
        id: text(raw.id || raw.case_id || raw.investigation_id || raw.session_id),
        source_channel_id: sourceChannelId,
        channel_name: text(
            raw.target_channel_name ||
                raw.channel_name ||
                raw.recovered_channel_name,
        ),
        target_folder_name: text(
            raw.target_component ||
                raw.target_folder_name ||
                raw.folder_name ||
                raw.target_name,
        ),
        target_folder_path:
            text(raw.target_folder_path || raw.target_path) || undefined,
        status: text(raw.status, "draft").toLowerCase(),
        phase: text(raw.phase) || undefined,
        message: text(raw.message) || undefined,
        total:
            numberOrNull(
                raw.total ||
                    raw.total_count ||
                    raw.item_count ||
                    rawCounts.total,
            ) ?? items.length,
        completed,
        failed,
        repair_needed:
            numberOrNull(
                raw.repair_needed ??
                    raw.links_needing_repair ??
                    rawCounts.repair_needed ??
                    rawCounts.links_needing_repair ??
                    rawLinkHealth.repair_needed ??
                    rawLinkHealth.missing_repairable,
            ) ?? 0,
        items,
        active_job:
            record(envelope.active_job || raw.active_job).id ||
            record(envelope.active_job || raw.active_job).job_id
                ? record(envelope.active_job || raw.active_job)
                : null,
        counts,
    };
}

export function normalizeRecoveredChannelItems(
    value: unknown,
    sourceChannelId = "",
): { items: RecoveredChannelItem[]; page: number; per_page: number; total: number } {
    const raw = record(value);
    const rawItems = Array.isArray(raw.items) ? raw.items : [];
    const items = rawItems.map((item) =>
        normalizeRecoveredChannelItem(item, sourceChannelId),
    );
    return {
        items,
        page: numberOrNull(raw.page) ?? 1,
        per_page: numberOrNull(raw.per_page) ?? Math.max(1, items.length),
        total: numberOrNull(raw.total) ?? items.length,
    };
}

export function normalizeRecoveredChannelPlan(
    value: unknown,
): RecoveredChannelPlan {
    const envelope = record(value);
    const raw = record(envelope.plan || value);
    const rawItems = Array.isArray(raw.items)
        ? raw.items
        : Array.isArray(raw.entries)
          ? raw.entries
          : [];
    const items = rawItems.map((item) => {
        const entry = record(item);
        return {
            ...entry,
            video_id: text(entry.video_id),
            source_path: text(entry.source_path || entry.path),
            destination_path: text(
                entry.destination_path || entry.destination || entry.link_path,
            ),
            link_target:
                text(
                    entry.link_target ||
                        entry.symlink_target ||
                        entry.actual_link_target ||
                        entry.true_link_target,
                ) || undefined,
            status: text(
                entry.status,
                entry.ready === false || entry.conflict ? "blocked" : "ready",
            ).toLowerCase(),
            error:
                text(entry.error || entry.message || entry.conflict) || undefined,
        };
    });
    const blockedCount = items.filter((item) =>
        ["blocked", "conflict", "invalid", "error"].includes(item.status),
    ).length;
    const targetFolderPath = text(raw.target_folder_path || raw.target_path);
    return {
        ...raw,
        approved_count:
            numberOrNull(raw.approved_count) ?? items.length,
        ready_count:
            numberOrNull(raw.ready_count) ?? items.length - blockedCount,
        blocked_count:
            numberOrNull(raw.blocked_count) ?? blockedCount,
        target_folder_name:
            text(raw.target_folder_name || raw.folder_name) ||
            (targetFolderPath ? basename(targetFolderPath) : undefined),
        target_folder_path:
            targetFolderPath || undefined,
        items,
    };
}

export function normalizeRecoveredChannelResult(
    value: unknown,
): RecoveredChannelResult {
    const envelope = record(value);
    const raw = record(envelope.result || value);
    const plan = normalizeRecoveredChannelPlan(raw);
    return {
        ...raw,
        success_count:
            numberOrNull(raw.success_count || raw.organized_count) ??
            plan.items.filter((item) =>
                ["success", "organized", "created"].includes(item.status),
            ).length,
        failed_count:
            numberOrNull(raw.failed_count || raw.fail_count) ??
            plan.items.filter((item) =>
                ["failed", "blocked", "error"].includes(item.status),
            ).length,
        held_count: numberOrNull(raw.held_count) ?? 0,
        items: plan.items,
        message: text(raw.message) || undefined,
    };
}

export function isInspectionComplete(item: RecoveredChannelItem): boolean {
    return ["complete", "completed", "healthy", "warning", "damaged"].includes(
        item.inspection_status,
    );
}

export function isLikelyUsable(item: RecoveredChannelItem): boolean {
    const tier = (item.severity || item.bucket || item.assessment)
        .trim()
        .toLowerCase();
    return tier === "clear" || tier === "minimal";
}

export function investigationIsActive(status: string): boolean {
    return [
        "queued",
        "running",
        "inspecting",
        "scanning",
        "organizing",
    ].includes(status.toLowerCase());
}

export function basename(path: string): string {
    const pieces = path.replaceAll("\\", "/").split("/");
    return pieces[pieces.length - 1] || path;
}

export function suffixFrom(path: string): string {
    const filename = basename(path);
    const position = filename.lastIndexOf(".");
    return position > 0 ? filename.slice(position).toLowerCase() : "";
}
