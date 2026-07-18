<script lang="ts">
    import { createEventDispatcher, onDestroy, onMount, tick } from "svelte";
    import {
        basename,
        investigationIsActive,
        isLikelyUsable,
        normalizeRecoveredChannelInvestigation,
        normalizeRecoveredChannelItem,
        normalizeRecoveredChannelItems,
        normalizeRecoveredChannelPlan,
        normalizeRecoveredChannelResult,
        type PreviewMode,
        type RecoveredChannelInvestigation,
        type RecoveredChannelItem,
        type RecoveredChannelPlan,
        type RecoveredChannelPlanItem,
        type RecoveredChannelPlanScope,
        type RecoveredChannelResult,
        type RecoveredChannelStep,
        type ReviewDecision,
    } from "../lib/recoveredChannel";

    export let sourceChannelId: string;
    export let sourceItems: Array<Record<string, unknown>> = [];

    const dispatch = createEventDispatcher<{
        exit: { changed: boolean };
        organized: { caseId: string; result: RecoveredChannelResult };
        dirtychange: { dirty: boolean };
        preview: {
            item: Record<string, unknown>;
            mode: PreviewMode;
        };
    }>();

    const API_ROOT = "/api/recovery/channel-cases";
    const PAGE_SIZE = 100;
    const terminalJobStatuses = new Set([
        "complete",
        "completed",
        "failed",
        "cancelled",
        "canceled",
        "partial",
        "interrupted",
        "adopted",
        "restored",
        "recovery_blocked",
    ]);

    let step: RecoveredChannelStep = "channel";
    let targetChannelName = "";
    let investigation: RecoveredChannelInvestigation | null = null;
    let items: RecoveredChannelItem[] = [];
    let itemsPage = 0;
    let itemsTotal = sourceItems.length;
    let plan: RecoveredChannelPlan | null = null;
    let organizeResult: RecoveredChannelResult | null = null;
    let organizationOutcomeWarning: string | null = null;
    let activeJob: Record<string, unknown> | null = null;
    let activeJobKind: "inspect" | "organize" | "" = "";
    let loadingInitial = true;
    let loadingItems = false;
    let actionBusy = false;
    let error: string | null = null;
    let notice: string | null = null;
    let changed = false;
    let destroyed = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let pollInFlight = false;
    let decisionReconciliationSequence = 0;
    let updatingItemIds = new Set<string>();
    let repairingItemIds = new Set<string>();
    let expectedDurationDrafts: Record<string, string> = {};
    let expectedDurationErrors: Record<string, string> = {};
    let reportedDirtyExpectedDurations = false;
    let mobileReviewBucket: "likely" | "review" = "likely";
    let likelyPlanConfirmationOpen = false;
    let stepHeading: HTMLElement | null = null;
    let backButton: HTMLButtonElement | null = null;
    let likelyPlanTrigger: HTMLButtonElement | null = null;
    let likelyPlanConfirmationHeading: HTMLElement | null = null;
    type RepairReference = {
        item_id: string;
        video_id: string;
        filename: string;
        score: number;
        compatibility: string;
        reasons: string[];
        health_tier: string;
        size_bytes?: number;
        preview_url?: string;
        preview_audio_url?: string;
    };
    type ReferenceRepair = Record<string, any> & {
        repair_id: string;
        status: string;
        phase: string;
        assessment?: string;
        decision?: string;
        progress?: number;
        error?: string;
        candidate_preview_url?: string;
        candidate_audio_url?: string;
        backup_preview_url?: string;
        backup_audio_url?: string;
    };
    let repairDamaged: RecoveredChannelItem | null = null;
    let repairReferences: RepairReference[] = [];
    let repairHistory: ReferenceRepair[] = [];
    let selectedReferenceId = "";
    let activeReferenceRepair: ReferenceRepair | null = null;
    let repairCompatibilityUncertain = false;
    let referenceRepairBusy = false;
    let referenceRepairError: string | null = null;
    let repairActionPlan: Record<string, any> | null = null;
    let repairPollTimer: ReturnType<typeof setTimeout> | null = null;
    let acknowledgeWeakReference = false;
    let repairConfirmationInput = "";
    let existingTargetComponent = "";
    let attachPlan: Record<string, any> | null = null;
    let attachConfirmation = "";

    function asRecord(value: unknown): Record<string, unknown> {
        return value && typeof value === "object" && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
    }

    function text(value: unknown, fallback = ""): string {
        return typeof value === "string" ? value : fallback;
    }

    function numeric(value: unknown, fallback = 0): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function messageFrom(value: unknown): string {
        return value instanceof Error ? value.message : String(value);
    }

    async function readResponse(response: Response): Promise<any> {
        const body = await response.json().catch(() => ({}));
        if (response.status === 401) {
            window.location.href = "/login";
            throw new Error("Your session expired.");
        }
        if (!response.ok) {
            throw new Error(
                body.error ||
                    body.message ||
                    `Request failed with status ${response.status}`,
            );
        }
        return body;
    }

    async function request(path: string, init?: RequestInit): Promise<any> {
        return readResponse(
            await fetch(path, {
                ...init,
                headers: init?.body
                    ? {
                          "Content-Type": "application/json",
                          ...(init.headers || {}),
                      }
                    : init?.headers,
            }),
        );
    }

    async function moveTo(nextStep: RecoveredChannelStep) {
        step = nextStep;
        error = null;
        await tick();
        stepHeading?.focus();
    }

    async function selectMobileReviewBucket(
        bucket: "likely" | "review",
        focusTab = false,
    ) {
        mobileReviewBucket = bucket;
        if (bucket === "review") likelyPlanConfirmationOpen = false;
        if (!focusTab) return;
        await tick();
        document
            .getElementById(`recovered-bucket-${bucket}`)
            ?.focus();
    }

    function handleMobileBucketKeydown(
        event: KeyboardEvent,
        current: "likely" | "review",
    ) {
        let next: "likely" | "review" | null = null;
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            next = current === "likely" ? "review" : "likely";
        } else if (event.key === "Home") {
            next = "likely";
        } else if (event.key === "End") {
            next = "review";
        }
        if (!next) return;
        event.preventDefault();
        void selectMobileReviewBucket(next, true);
    }

    async function openLikelyPlanConfirmation() {
        if (dirtyExpectedDurationItems.length > 0 || actionBusy) return;
        likelyPlanConfirmationOpen = true;
        await tick();
        likelyPlanConfirmationHeading?.focus();
    }

    async function closeLikelyPlanConfirmation(restoreFocus = true) {
        likelyPlanConfirmationOpen = false;
        if (!restoreFocus) return;
        await tick();
        likelyPlanTrigger?.focus();
    }

    function handleLikelyPlanConfirmationKeydown(event: KeyboardEvent) {
        if (event.key !== "Escape") return;
        event.preventDefault();
        void closeLikelyPlanConfirmation();
    }

    function requestedTargetComponent(): string {
        const name = targetChannelName.trim();
        if (!name) return "Channel name [Recovered]";
        return /\[Recovered\]$/i.test(name) ? name : `${name} [Recovered]`;
    }

    function casePath(suffix = ""): string {
        return `${API_ROOT}/${encodeURIComponent(investigation?.id || "")}${suffix}`;
    }

    function jobId(job: Record<string, unknown> | null): string {
        return text(job?.id || job?.job_id);
    }

    function jobStatus(job: Record<string, unknown> | null): string {
        return text(job?.status, "unknown").toLowerCase();
    }

    function inferJobKind(job: Record<string, unknown> | null): "inspect" | "organize" | "" {
        const value = text(
            job?.kind || job?.operation || job?.mode || job?.job_type,
        ).toLowerCase();
        if (value.includes("organ")) return "organize";
        if (value.includes("inspect") || value.includes("diagn")) return "inspect";
        return "";
    }

    function inspectionFinished(): boolean {
        if (!investigation) return false;
        const total = numeric(investigation.counts?.remaining, Math.max(investigation.total, itemsTotal));
        const finished = numeric(
            investigation.counts?.remaining_finished,
            investigation.completed + investigation.failed,
        );
        return total > 0 && finished >= total;
    }

    function hasItemsNeedingInspection(): boolean {
        const counts = investigation?.counts || {};
        const total = numeric(counts.remaining, totalCount);
        const completed = numeric(counts.remaining_finished, counts.completed);
        return total > completed;
    }

    function deriveStepFromCase() {
        if (!investigation) return;
        const status = investigation.status.toLowerCase();
        if (
            investigation.repair_needed > 0 ||
            ["organized", "complete", "completed"].includes(status)
        ) {
            step = "complete";
            return;
        }
        const current = investigation.active_job || activeJob;
        const kind = inferJobKind(current);
        if (current && !terminalJobStatuses.has(jobStatus(current))) {
            activeJob = current;
            activeJobKind = kind;
            step = kind === "organize" ? "plan" : "inspect";
            return;
        }
        if (
            inspectionFinished() ||
            ["review", "reviewing", "inspected", "ready"].includes(status)
        ) {
            step = "review";
        } else {
            step = "inspect";
        }
    }

    async function discoverExistingCase() {
        loadingInitial = true;
        error = null;
        try {
            const data = await request(API_ROOT);
            const rawCases = Array.isArray(data?.cases) ? data.cases : [];
            const matching = rawCases.find(
                (entry: unknown) =>
                    text(
                        asRecord(entry).source_channel_id ||
                            asRecord(entry).channel_id,
                    ) === sourceChannelId,
            );
            if (matching) {
                const summary = normalizeRecoveredChannelInvestigation({
                    case: matching,
                });
                if (summary.id) await loadCase(summary.id, true);
            }
        } catch (value) {
            error = `Could not check for an existing investigation: ${messageFrom(value)}`;
        } finally {
            loadingInitial = false;
        }
    }

    async function loadCase(
        id: string,
        refreshItems = false,
        shouldApply: () => boolean = () => true,
    ) {
        const data = await request(`${API_ROOT}/${encodeURIComponent(id)}`);
        if (!shouldApply()) return;
        const next = normalizeRecoveredChannelInvestigation(data);
        investigation = next;
        targetChannelName = next.channel_name || targetChannelName;
        activeJob = next.active_job || null;
        activeJobKind = inferJobKind(activeJob);
        if (refreshItems || items.length === 0) {
            await loadItems(true);
        }
        deriveStepFromCase();
        if (activeJob && !terminalJobStatuses.has(jobStatus(activeJob))) {
            schedulePoll(400);
        }
    }

    async function loadItems(reset = false) {
        if (!investigation?.id || loadingItems) return;
        const nextPage = reset ? 1 : itemsPage + 1;
        loadingItems = true;
        try {
            const query = new URLSearchParams({
                page: String(nextPage),
                per_page: String(PAGE_SIZE),
            });
            const data = await request(`${casePath("/items")}?${query}`);
            const page = normalizeRecoveredChannelItems(
                data,
                investigation.source_channel_id,
            );
            if (reset) {
                items = page.items;
            } else {
                const byId = new Map(items.map((item) => [item.id, item]));
                for (const item of page.items) byId.set(item.id, item);
                items = Array.from(byId.values());
            }
            itemsPage = page.page;
            itemsTotal = page.total;
        } catch (value) {
            error = `Could not load investigation videos: ${messageFrom(value)}`;
        } finally {
            loadingItems = false;
        }
    }

    async function createCase() {
        const name = targetChannelName.trim();
        if (!name) {
            error = "Enter the recovered channel name first.";
            return;
        }
        actionBusy = true;
        error = null;
        notice = null;
        try {
            const data = await request(API_ROOT, {
                method: "POST",
                body: JSON.stringify({
                    source_channel_id: sourceChannelId,
                    target_channel_name: name,
                }),
            });
            investigation = normalizeRecoveredChannelInvestigation(data);
            if (!investigation.id) {
                throw new Error("The server did not return an investigation ID");
            }
            targetChannelName = investigation.channel_name || name;
            changed = true;
            items = [];
            itemsPage = 0;
            await loadCase(investigation.id, true);
            await moveTo("inspect");
        } catch (value) {
            error = `Investigation could not be created: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    async function buildExistingTargetPlan(component = existingTargetComponent) {
        actionBusy = true;
        error = null;
        notice = null;
        attachPlan = null;
        attachConfirmation = "";
        try {
            const body: Record<string, string> = {
                source_channel_id: sourceChannelId,
            };
            if (component.trim()) body.target_component = component.trim();
            const data = await request(`${API_ROOT}/attach-plan`, {
                method: "POST",
                body: JSON.stringify(body),
            });
            attachPlan = asRecord(data?.plan || data);
            if (text(attachPlan.status) === "ready") {
                existingTargetComponent = text(attachPlan.target_component);
                notice = "Dry run complete. No source or Target entry has been changed.";
            } else {
                notice = text(attachPlan.message, "Choose the exact existing Target folder.");
            }
        } catch (value) {
            error = `Existing Target folder could not be reconciled: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    async function applyExistingTargetPlan() {
        if (!attachPlan?.plan_token || actionBusy) return;
        actionBusy = true;
        error = null;
        try {
            const data = await request(`${API_ROOT}/attach`, {
                method: "POST",
                body: JSON.stringify({
                    plan_token: attachPlan.plan_token,
                    confirmation: attachConfirmation,
                }),
            });
            investigation = normalizeRecoveredChannelInvestigation(data);
            if (!investigation.id) throw new Error("The server did not return an investigation ID");
            targetChannelName = investigation.channel_name || existingTargetComponent;
            changed = true;
            items = [];
            itemsPage = 0;
            notice = `${numeric(data?.adopted_links)} existing links attached; ${numeric(data?.source_only)} source-only videos remain for review.`;
            await loadCase(investigation.id, true);
            await moveTo("inspect");
        } catch (value) {
            error = `Existing Target folder could not be attached: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    async function startInspection(rescan = false) {
        if (!investigation || actionBusy) return;
        if (dirtyExpectedDurationItems.length > 0) {
            error = "Save or discard every expected-duration edit before inspecting. Unsaved baselines are not used by the server.";
            return;
        }
        if (
            rescan &&
            !confirm(
                "Run a full inspection again?\n\nSaved diagnoses will be replaced and the current organization plan will be invalidated. Review approval decisions again after the rescan.",
            )
        )
            return;
        plan = null;
        actionBusy = true;
        error = null;
        notice = null;
        try {
            const data = await request(casePath("/inspect"), {
                method: "POST",
                body: JSON.stringify({ rescan }),
            });
            const job = asRecord(data?.job || data);
            activeJob = job;
            activeJobKind = "inspect";
            notice =
                text(data?.message) ||
                "Inspection queued. Each source is decoded sequentially and the results are saved to this investigation.";
            await moveTo("inspect");
            schedulePoll(350);
        } catch (value) {
            error = `Inspection could not start: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    function schedulePoll(delay = 1400) {
        if (destroyed || !activeJob || terminalJobStatuses.has(jobStatus(activeJob)))
            return;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(() => void pollActiveJob(), delay);
    }

    async function pollActiveJob() {
        if (
            destroyed ||
            pollInFlight ||
            !investigation ||
            !jobId(activeJob)
        )
            return;
        pollInFlight = true;
        try {
            const data = await request(
                casePath(`/jobs/${encodeURIComponent(jobId(activeJob))}`),
            );
            const job = asRecord(data?.job || data);
            activeJob = job;
            const status = jobStatus(job);
            const kind = activeJobKind || inferJobKind(job);
            if (terminalJobStatuses.has(status)) {
                await loadCase(investigation.id, true);
                if (status === "failed") {
                    error = text(job.error || job.message, `${kind || "Background"} job failed.`);
                    await moveTo(kind === "organize" ? "plan" : "review");
                } else if (status === "interrupted") {
                    if (kind === "organize") {
                        error = "Organization was interrupted by a server restart. Build a fresh dry-run plan before retrying.";
                        plan = null;
                        await moveTo("plan");
                    } else {
                        notice = "Inspection was interrupted. Completed reports were kept; resume inspection for the remaining videos.";
                        await moveTo("review");
                    }
                } else if (status === "cancelled" || status === "canceled") {
                    notice = "The remaining queued work was cancelled. Completed inspection reports were kept.";
                    await moveTo("review");
                } else if (kind === "organize") {
                    organizeResult = normalizeRecoveredChannelResult(
                        job.result || data.result || job,
                    );
                    organizationOutcomeWarning =
                        status === "partial"
                            ? text(
                                  job.error || data.error || job.message,
                                  "Organization completed only partially. Review the failed or blocked count before continuing.",
                              )
                            : null;
                    changed = true;
                    await moveTo("complete");
                    dispatch("organized", {
                        caseId: investigation.id,
                        result: organizeResult,
                    });
                } else {
                    notice = "Inspection complete. Review the two condition lists before approving anything.";
                    await moveTo("review");
                }
                activeJob = null;
                activeJobKind = "";
            } else {
                const currentId = investigation.id;
                const caseData = await request(
                    `${API_ROOT}/${encodeURIComponent(currentId)}`,
                );
                const refreshed = normalizeRecoveredChannelInvestigation(caseData);
                investigation = {
                    ...refreshed,
                    active_job: job,
                };
                schedulePoll();
            }
        } catch (value) {
            error = `Could not refresh background work: ${messageFrom(value)}`;
            schedulePoll(3000);
        } finally {
            pollInFlight = false;
        }
    }

    async function cancelInspection() {
        if (
            !investigation ||
            !activeJob ||
            activeJobKind !== "inspect" ||
            !confirm("Cancel the remaining inspection queue? Completed reports will be kept.")
        )
            return;
        actionBusy = true;
        error = null;
        try {
            const data = await request(
                casePath(`/jobs/${encodeURIComponent(jobId(activeJob))}/cancel`),
                { method: "POST" },
            );
            activeJob = asRecord(data?.job || data);
            notice = "Cancellation requested. The current decoder will stop safely.";
            schedulePoll(300);
        } catch (value) {
            error = `Inspection could not be cancelled: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    function replaceItem(next: RecoveredChannelItem) {
        items = items.map((item) => (item.id === next.id ? next : item));
    }

    function applyDecisionItem(
        previous: RecoveredChannelItem,
        next: RecoveredChannelItem,
    ) {
        if (investigation) {
            const counts = { ...(investigation.counts || {}) };
            const approvedDelta =
                Number(next.decision === "approved") -
                Number(previous.decision === "approved");
            const holdDelta =
                Number(next.decision === "held") -
                Number(previous.decision === "held");
            const currentApproved = countFromCase(
                counts,
                ["approved", "remaining_approved", "decision_approved"],
                unresolvedItems.filter(
                    (entry) => entry.decision === "approved",
                ).length,
            );
            const currentHold = countFromCase(
                counts,
                ["hold", "held", "decision_hold"],
                unresolvedItems.filter(
                    (entry) => entry.decision === "held",
                ).length,
            );
            const approved = Math.max(0, currentApproved + approvedDelta);
            const hold = Math.max(0, currentHold + holdDelta);
            counts.approved = approved;
            counts.hold = hold;
            for (const alias of ["remaining_approved", "decision_approved"]) {
                if (alias in counts) counts[alias] = approved;
            }
            for (const alias of ["held", "decision_hold"]) {
                if (alias in counts) counts[alias] = hold;
            }
            investigation = { ...investigation, counts };
        }
        replaceItem(next);
    }

    function formatExpectedDurationInput(value?: number | null): string {
        const seconds = Number(value);
        if (!Number.isFinite(seconds) || seconds <= 0) return "";
        if (!Number.isInteger(seconds) || seconds < 60) return String(seconds);
        const whole = Math.round(seconds);
        const hours = Math.floor(whole / 3600);
        const minutes = Math.floor((whole % 3600) / 60);
        const remainder = whole % 60;
        return hours
            ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
            : `${minutes}:${String(remainder).padStart(2, "0")}`;
    }

    function expectedDurationInput(item: RecoveredChannelItem): string {
        if (Object.prototype.hasOwnProperty.call(expectedDurationDrafts, item.id)) {
            return expectedDurationDrafts[item.id];
        }
        return formatExpectedDurationInput(item.expected_duration_seconds);
    }

    function expectedDurationDisplay(value?: number | null): string {
        const formatted = formatExpectedDurationInput(value);
        if (!formatted) return "Not set";
        return formatted.includes(":") ? formatted : `${formatted} seconds`;
    }

    function setExpectedDurationDraft(itemId: string, value: string) {
        expectedDurationDrafts = {
            ...expectedDurationDrafts,
            [itemId]: value,
        };
        plan = null;
        if (expectedDurationErrors[itemId]) {
            const next = { ...expectedDurationErrors };
            delete next[itemId];
            expectedDurationErrors = next;
        }
    }

    function setExpectedDurationError(itemId: string, message?: string) {
        const next = { ...expectedDurationErrors };
        if (message) next[itemId] = message;
        else delete next[itemId];
        expectedDurationErrors = next;
    }

    function parseExpectedDuration(value: string): {
        seconds: number | null;
        error?: string;
    } {
        const trimmed = value.trim();
        if (!trimmed) return { seconds: null };

        let seconds: number;
        if (trimmed.includes(":")) {
            const parts = trimmed.split(":");
            if (
                (parts.length !== 2 && parts.length !== 3) ||
                parts.some((part, index) =>
                    index === parts.length - 1
                        ? !/^\d+(?:\.\d+)?$/.test(part)
                        : !/^\d+$/.test(part),
                )
            ) {
                return { seconds: null, error: "Use seconds or a time such as 10:00." };
            }
            const values = parts.map(Number);
            const finalSeconds = values.at(-1) || 0;
            const finalMinutes = values.at(-2) || 0;
            if (finalSeconds >= 60 || (parts.length === 3 && finalMinutes >= 60)) {
                return { seconds: null, error: "Minutes and seconds must be below 60 in a colon-formatted time." };
            }
            seconds =
                parts.length === 3
                    ? values[0] * 3600 + values[1] * 60 + values[2]
                    : values[0] * 60 + values[1];
        } else if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
            seconds = Number(trimmed);
        } else {
            return { seconds: null, error: "Use seconds or a time such as 10:00." };
        }

        if (!Number.isFinite(seconds) || seconds <= 0) {
            return { seconds: null, error: "Expected duration must be greater than zero." };
        }
        if (seconds > 31 * 24 * 3600) {
            return { seconds: null, error: "Expected duration cannot exceed 31 days." };
        }
        return { seconds };
    }

    function expectedDurationChanged(item: RecoveredChannelItem): boolean {
        const parsed = parseExpectedDuration(expectedDurationInput(item));
        if (parsed.error) return true;
        const current = item.expected_duration_seconds ?? null;
        return parsed.seconds !== current;
    }

    function discardExpectedDurationDraft(item: RecoveredChannelItem) {
        setExpectedDurationDraft(
            item.id,
            formatExpectedDurationInput(item.expected_duration_seconds),
        );
        setExpectedDurationError(item.id);
        notice = "Unsaved expected-duration edit discarded.";
    }

    async function saveExpectedDuration(
        item: RecoveredChannelItem,
        clear = false,
    ) {
        if (!investigation || !item.id || updatingItemIds.has(item.id)) return;
        const parsed = clear
            ? { seconds: null }
            : parseExpectedDuration(expectedDurationInput(item));
        if (parsed.error) {
            setExpectedDurationError(item.id, parsed.error);
            return;
        }
        const current = item.expected_duration_seconds ?? null;
        if (parsed.seconds === current) {
            setExpectedDurationDraft(item.id, expectedDurationInput(item));
            return;
        }

        const action =
            parsed.seconds === null
                ? "Clear the expected full duration?"
                : `Set the expected full duration to ${expectedDurationDisplay(parsed.seconds)}?`;
        if (
            !confirm(
                `${action}\n\nThis clears the prior diagnosis, checksum, and approval for this video. You must inspect it again before it can be approved.`,
            )
        )
            return;

        const nextUpdating = new Set(updatingItemIds);
        nextUpdating.add(item.id);
        updatingItemIds = nextUpdating;
        setExpectedDurationError(item.id);
        error = null;
        notice = null;
        try {
            const data = await request(
                casePath(`/items/${encodeURIComponent(item.id)}`),
                {
                    method: "PATCH",
                    body: JSON.stringify({
                        expected_duration_seconds: parsed.seconds,
                    }),
                },
            );
            const next = normalizeRecoveredChannelItem(
                data?.item || data,
                investigation.source_channel_id,
            );
            replaceItem(next);
            setExpectedDurationDraft(
                next.id,
                formatExpectedDurationInput(next.expected_duration_seconds),
            );
            changed = true;
            plan = null;
            mobileReviewBucket = "review";
            notice = parsed.seconds === null
                ? "Expected duration cleared. The prior diagnosis and approval were reset; reinspect this video before approving it again."
                : "Expected duration saved. The prior diagnosis and approval were reset; reinspect this video before approving it again.";
            await loadCase(investigation.id, false);
            if (!activeJob || terminalJobStatuses.has(jobStatus(activeJob))) {
                step = "review";
            }
        } catch (value) {
            setExpectedDurationError(
                item.id,
                `Expected duration was not saved: ${messageFrom(value)}`,
            );
        } finally {
            const remaining = new Set(updatingItemIds);
            remaining.delete(item.id);
            updatingItemIds = remaining;
        }
    }

    async function updateDecision(
        item: RecoveredChannelItem,
        decision: ReviewDecision,
    ) {
        if (!investigation || !item.id || updatingItemIds.has(item.id)) return;
        const nextUpdating = new Set(updatingItemIds);
        nextUpdating.add(item.id);
        updatingItemIds = nextUpdating;
        error = null;
        let decisionSaved = false;
        try {
            const data = await request(
                casePath(`/items/${encodeURIComponent(item.id)}`),
                {
                    method: "PATCH",
                    body: JSON.stringify({
                        decision:
                            decision === "held"
                                ? "hold"
                                : decision === "undecided"
                                  ? "pending"
                                  : decision,
                    }),
                },
            );
            applyDecisionItem(
                item,
                normalizeRecoveredChannelItem(
                    data?.item || data,
                    investigation.source_channel_id,
                ),
            );
            changed = true;
            plan = null;
            decisionSaved = true;
        } catch (value) {
            error = `Decision was not saved: ${messageFrom(value)}`;
        }

        if (decisionSaved) {
            const reconciliation = ++decisionReconciliationSequence;
            try {
                await loadCase(
                    investigation.id,
                    false,
                    () => reconciliation === decisionReconciliationSequence,
                );
            } catch (value) {
                if (reconciliation === decisionReconciliationSequence) {
                    error = `Decision was saved, but the case summary could not be refreshed: ${messageFrom(value)}. The saved card state and local counts are retained.`;
                }
            }
        }

        const remaining = new Set(updatingItemIds);
        remaining.delete(item.id);
        updatingItemIds = remaining;
    }

    function openPreview(item: RecoveredChannelItem, mode: PreviewMode) {
        if (!item.path) return;
        dispatch("preview", {
            mode,
            item: {
                path: item.path,
                filename: item.filename,
                video_id: item.video_id,
                source_channel_id: investigation?.source_channel_id || sourceChannelId,
                title: item.filename,
            },
        });
    }

    function repairPath(suffix = ""): string {
        if (!investigation) throw new Error("Recovered channel case is not loaded");
        return casePath(`/reference-repairs${suffix}`);
    }

    function itemEligibleForReferenceRepair(item: RecoveredChannelItem): boolean {
        return ["serious", "inconclusive"].includes(item.bucket || item.severity);
    }

    function selectRepairAttempt(repairId: string) {
        if (repairPollTimer) clearTimeout(repairPollTimer);
        repairPollTimer = null;
        activeReferenceRepair =
            repairHistory.find((entry) => entry.repair_id === repairId) || null;
        repairActionPlan = null;
        repairConfirmationInput = "";
        if (activeReferenceRepair && !terminalJobStatuses.has(activeReferenceRepair.status)) {
            scheduleRepairPoll(250);
        }
    }

    async function openReferenceRepair(item: RecoveredChannelItem) {
        if (!investigation) return;
        repairDamaged = item;
        step = "review";
        mobileReviewBucket = "review";
        repairReferences = [];
        repairHistory = [];
        selectedReferenceId = "";
        activeReferenceRepair = null;
        repairActionPlan = null;
        referenceRepairError = null;
        referenceRepairBusy = true;
        try {
            const repairData = await request(
                repairPath(`?damaged_item_id=${encodeURIComponent(item.id)}`),
            );
            repairHistory = Array.isArray(repairData?.repairs) ? repairData.repairs : [];
            activeReferenceRepair = repairHistory[0] || null;
            if (itemEligibleForReferenceRepair(item)) {
                try {
                    const referenceData = await request(
                        casePath(`/items/${encodeURIComponent(item.id)}/repair-references`),
                    );
                    repairReferences = Array.isArray(referenceData?.references)
                        ? referenceData.references
                        : [];
                    repairCompatibilityUncertain = Boolean(
                        referenceData?.compatibility_uncertain,
                    );
                } catch (value) {
                    referenceRepairError = `Reference candidates could not be loaded: ${messageFrom(value)}`;
                }
            }
            selectedReferenceId = "";
            acknowledgeWeakReference = false;
            if (activeReferenceRepair && !terminalJobStatuses.has(activeReferenceRepair.status)) {
                scheduleRepairPoll(500);
            }
            await tick();
            document.getElementById("reference-repair-heading")?.scrollIntoView({ block: "nearest" });
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    function closeReferenceRepair() {
        if (repairPollTimer) clearTimeout(repairPollTimer);
        repairPollTimer = null;
        repairDamaged = null;
        repairReferences = [];
        repairHistory = [];
        activeReferenceRepair = null;
        repairActionPlan = null;
        repairConfirmationInput = "";
        referenceRepairError = null;
    }

    function scheduleRepairPoll(delay = 1000) {
        if (repairPollTimer) clearTimeout(repairPollTimer);
        repairPollTimer = setTimeout(() => void pollReferenceRepair(), delay);
    }

    async function pollReferenceRepair() {
        if (!investigation || !activeReferenceRepair || destroyed) return;
        try {
            const data = await request(
                repairPath(`/${encodeURIComponent(activeReferenceRepair.repair_id)}`),
            );
            activeReferenceRepair = data?.repair || data;
            repairHistory = [
                activeReferenceRepair!,
                ...repairHistory.filter(
                    (entry) => entry.repair_id !== activeReferenceRepair?.repair_id,
                ),
            ];
            if (!terminalJobStatuses.has(activeReferenceRepair?.status || "")) {
                scheduleRepairPoll(1000);
            }
        } catch (value) {
            referenceRepairError = `Repair status could not be refreshed: ${messageFrom(value)}`;
            scheduleRepairPoll(3000);
        }
    }

    async function startReferenceRepair() {
        if (!repairDamaged || !selectedReferenceId) return;
        referenceRepairBusy = true;
        referenceRepairError = null;
        repairActionPlan = null;
        try {
            const data = await request(repairPath(), {
                method: "POST",
                body: JSON.stringify({
                    damaged_item_id: repairDamaged.id,
                    reference_item_id: selectedReferenceId,
                }),
            });
            activeReferenceRepair = data?.repair || data;
            repairHistory = [activeReferenceRepair!, ...repairHistory];
            scheduleRepairPoll(400);
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    async function cancelReferenceRepair() {
        if (!activeReferenceRepair) return;
        referenceRepairBusy = true;
        try {
            const data = await request(
                repairPath(`/${encodeURIComponent(activeReferenceRepair.repair_id)}/cancel`),
                { method: "POST" },
            );
            activeReferenceRepair = data?.repair || data;
            repairHistory = [
                activeReferenceRepair!,
                ...repairHistory.filter(
                    (entry) => entry.repair_id !== activeReferenceRepair?.repair_id,
                ),
            ];
            scheduleRepairPoll(300);
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    async function retryReferenceRepair() {
        if (!activeReferenceRepair) return;
        referenceRepairBusy = true;
        try {
            const data = await request(
                repairPath(`/${encodeURIComponent(activeReferenceRepair.repair_id)}/retry`),
                { method: "POST" },
            );
            activeReferenceRepair = data?.repair || data;
            repairHistory = [activeReferenceRepair!, ...repairHistory];
            repairActionPlan = null;
            repairConfirmationInput = "";
            scheduleRepairPoll(300);
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    async function prepareRepairAction(action: "adopt" | "restore") {
        if (!activeReferenceRepair) return;
        referenceRepairBusy = true;
        referenceRepairError = null;
        try {
            const data = await request(
                repairPath(`/${encodeURIComponent(activeReferenceRepair.repair_id)}/${action}-plan`),
                { method: "POST" },
            );
            repairActionPlan = data?.plan || data;
            repairConfirmationInput = "";
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    async function applyRepairAction() {
        if (!activeReferenceRepair || !repairActionPlan) return;
        const action = text(repairActionPlan.action) as "adopt" | "restore";
        referenceRepairBusy = true;
        referenceRepairError = null;
        try {
            const data = await request(
                repairPath(`/${encodeURIComponent(activeReferenceRepair.repair_id)}/${action}`),
                {
                    method: "POST",
                    body: JSON.stringify({
                        plan_token: repairActionPlan.plan_token,
                        confirmation: repairConfirmationInput,
                    }),
                },
            );
            activeReferenceRepair = data?.repair || data;
            repairHistory = repairHistory.map((entry) =>
                entry.repair_id === activeReferenceRepair?.repair_id
                    ? activeReferenceRepair!
                    : entry,
            );
            repairActionPlan = null;
            changed = true;
            await loadCase(investigation!.id, false);
            itemsPage = 0;
            items = [];
            await loadItems(true);
            repairDamaged = items.find((item) => item.id === repairDamaged?.id) || repairDamaged;
        } catch (value) {
            referenceRepairError = messageFrom(value);
        } finally {
            referenceRepairBusy = false;
        }
    }

    function repairReport(which: "before" | "after"): Record<string, any> {
        return asRecord(activeReferenceRepair?.[which]);
    }

    function repairReportStreams(which: "before" | "after"): string {
        const streams = Array.isArray(repairReport(which).streams)
            ? repairReport(which).streams
            : [];
        return streams.length
            ? streams
                  .map((entry: Record<string, unknown>) =>
                      `${text(entry.type, "media")}: ${text(entry.codec, "unknown")}`,
                  )
                  .join(" · ")
            : "No streams reported";
    }

    function repairAssessmentLabel(value?: string): string {
        const labels: Record<string, string> = {
            better: "Likely better",
            worse: "Likely worse",
            uncertain: "Uncertain — human review required",
        };
        return labels[value || ""] || value || "Not assessed";
    }

    function selectedRepairReference(): RepairReference | undefined {
        return repairReferences.find((entry) => entry.item_id === selectedReferenceId);
    }

    function selectedReferenceNeedsAcknowledgement(): boolean {
        return ["weak", "uncertain"].includes(
            selectedRepairReference()?.compatibility || "",
        );
    }

    function itemIsPublished(item: RecoveredChannelItem): boolean {
        return (
            Boolean(item.organized_path) ||
            !["", "unorganized", "unpublished"].includes(item.link_status)
        );
    }

    function publishedPath(item: RecoveredChannelItem): string {
        return item.expected_organized_path || item.organized_path || "";
    }

    function canRepairPublishedLink(item: RecoveredChannelItem): boolean {
        return (
            item.link_status === "missing" &&
            item.repairable_link === true &&
            Boolean(item.path) &&
            Boolean(publishedPath(item)) &&
            Boolean(item.link_target)
        );
    }

    function linkStatusLabel(item: RecoveredChannelItem): string {
        const labels: Record<string, string> = {
            healthy: "Healthy link",
            missing: item.repairable_link
                ? "Missing — repair available"
                : "Missing — manual review",
            wrong_symlink: "Wrong symlink target",
            unexpected_real_file: "Real file at managed path",
            source_invalid: "Source integrity failure",
            owner_invalid: "Recovered owner mismatch",
            unsafe: "Unsafe path state",
            unknown: "Link state unknown",
            unorganized: "Not published",
            unpublished: "Not published",
        };
        return labels[item.link_status] || item.link_status.replaceAll("_", " ");
    }

    function linkStatusExplanation(item: RecoveredChannelItem): string {
        const explanations: Record<string, string> = {
            healthy: "The managed path is a symlink to the expected immutable source.",
            missing: item.repairable_link
                ? "The managed symlink is absent, but the checksum-bound source is still valid."
                : item.source_integrity_error
                  ? `The managed path is absent and source integrity failed: ${item.source_integrity_error}`
                  : item.database_owner_valid === false
                    ? "The managed path is absent, but recovered database ownership no longer matches this item."
                    : "The managed path is absent and the server did not authorize automatic repair.",
            wrong_symlink: "A symlink exists, but it does not point to the expected source. Automatic repair is blocked.",
            unexpected_real_file: "A real file occupies the managed path. Automatic repair will not replace it.",
            source_invalid:
                item.source_integrity_error ||
                "The durable source is missing or its fingerprint changed. Automatic repair is blocked.",
            owner_invalid:
                item.database_owner_error ||
                "The exact recovered database owner is missing or belongs to another managed path. Automatic repair is blocked.",
            unsafe: "The managed path failed server safety validation. Automatic repair is blocked.",
            unknown: "The server could not establish the current managed-link state.",
        };
        return explanations[item.link_status] || "This published entry requires no automatic action.";
    }

    async function repairPublishedLink(item: RecoveredChannelItem) {
        if (
            !investigation ||
            !item.id ||
            !canRepairPublishedLink(item) ||
            repairingItemIds.has(item.id)
        )
            return;
        const source = item.path;
        const destination = publishedPath(item);
        if (
            !confirm(
                `Repair this missing managed link?\n\nSource: ${source}\nDestination: ${destination}\nSymlink target: ${item.link_target || "Unavailable"}\n\nThe server will revalidate the exact source fingerprint, database owner, destination, and symlink target before creating anything. It will not replace a real file or a wrong/unsafe link.`,
            )
        )
            return;

        const nextRepairing = new Set(repairingItemIds);
        nextRepairing.add(item.id);
        repairingItemIds = nextRepairing;
        error = null;
        notice = null;
        try {
            const data = await request(
                casePath(`/items/${encodeURIComponent(item.id)}/repair-link`),
                {
                    method: "POST",
                    body: JSON.stringify({}),
                },
            );
            if (data?.item) {
                replaceItem(
                    normalizeRecoveredChannelItem(
                        data.item,
                        investigation.source_channel_id,
                    ),
                );
            }
            changed = true;
            notice = `Managed link repaired and revalidated: ${destination}`;
            await loadCase(investigation.id, true);
        } catch (value) {
            error = `Managed link was not repaired: ${messageFrom(value)}`;
        } finally {
            const remaining = new Set(repairingItemIds);
            remaining.delete(item.id);
            repairingItemIds = remaining;
        }
    }

    async function buildPlan(scope: RecoveredChannelPlanScope) {
        if (
            !investigation ||
            actionBusy ||
            (scope === "approved" && approvedCount === 0) ||
            (scope === "likely_usable" && authoritativeLikelyCount === 0)
        )
            return;
        if (dirtyExpectedDurationItems.length > 0) {
            error = "Save or discard all expected-duration edits before building an organization plan.";
            return;
        }
        actionBusy = true;
        error = null;
        notice = null;
        organizationOutcomeWarning = null;
        try {
            const data = await request(casePath("/organization-plan"), {
                method: "POST",
                body: JSON.stringify({ scope }),
            });
            plan = normalizeRecoveredChannelPlan(data, scope);
            likelyPlanConfirmationOpen = false;
            await moveTo("plan");
        } catch (value) {
            error = `${scope === "likely_usable" ? "Likely-usable" : "Approved-video"} organization plan could not be built: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    async function organizeApproved() {
        if (!investigation || !plan || actionBusy) return;
        if (dirtyExpectedDurationItems.length > 0) {
            error = "Organization is blocked until every expected-duration edit is saved or discarded.";
            return;
        }
        const token = text(plan.token || plan.plan_token);
        if (!token) {
            error = "The dry-run plan has no validation token. Build it again.";
            return;
        }
        const likelyScope = plan.scope === "likely_usable";
        if (
            !confirm(
                likelyScope
                    ? `Publish ${plan.ready_count} likely usable videos under ${plan.target_folder_name || investigation.target_folder_name}?\n\nOnly the clear/minimal, non-held items bound to this dry-run plan will receive managed links. Successful pending items become approved. Needs closer review and held videos stay in the investigation. Source files remain in place.`
                    : `Create ${plan.ready_count} managed links under ${plan.target_folder_name || investigation.target_folder_name}? Source files will remain in place.`,
            )
        )
            return;
        actionBusy = true;
        error = null;
        notice = null;
        organizationOutcomeWarning = null;
        try {
            const data = await request(casePath("/organize"), {
                method: "POST",
                body: JSON.stringify({
                    plan_token: token,
                    scope: plan.scope,
                }),
            });
            activeJob = asRecord(data?.job || data);
            activeJobKind = "organize";
            notice = likelyScope
                ? "Likely-usable publication queued. Only the items bound to this scoped plan can receive links; every source and destination will be revalidated first."
                : "Organization queued. Every source and destination will be revalidated before its link is created.";
            schedulePoll(350);
        } catch (value) {
            error = `${likelyScope ? "Likely usable" : "Approved"} videos could not be organized: ${messageFrom(value)}`;
        } finally {
            actionBusy = false;
        }
    }

    function formatBytes(value?: number | null): string {
        const bytes = Number(value) || 0;
        if (!bytes) return "Unknown size";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const index = Math.min(
            Math.floor(Math.log(bytes) / Math.log(1024)),
            units.length - 1,
        );
        return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
    }

    function formatDuration(value?: number | null): string {
        const seconds = Math.max(0, Math.floor(Number(value) || 0));
        if (!seconds) return "Unknown";
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const remainder = seconds % 60;
        return hours
            ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
            : `${minutes}:${String(remainder).padStart(2, "0")}`;
    }

    function streamSummary(item: RecoveredChannelItem): string {
        if (!item.streams?.length) return "Streams not reported";
        return item.streams
            .map((stream) => {
                const kind = text(stream.type || stream.codec_type, "stream");
                return `${kind}: ${text(stream.codec || stream.codec_name, "unknown")}`;
            })
            .join(" · ");
    }

    function conditionLabel(item: RecoveredChannelItem): string {
        if (isLikelyUsable(item)) {
            return item.severity === "minimal"
                ? "Minimal decoder warnings"
                : "No decoder errors found";
        }
        const labels: Record<string, string> = {
            serious: "Serious decode issues",
            inconclusive: "Inspection inconclusive",
            damaged: "Decode damage reported",
            failed: "Inspection failed",
        };
        return labels[item.severity] || labels[item.assessment] || "Needs closer review";
    }

    function planScopeLabel(scope: RecoveredChannelPlanScope): string {
        return scope === "likely_usable"
            ? "Likely usable only"
            : "Approved selections";
    }

    function planHealthTierLabel(item: RecoveredChannelPlanItem): string {
        const labels: Record<string, string> = {
            clear: "Clear",
            minimal: "Minimal",
            serious: "Serious",
            inconclusive: "Inconclusive",
            damaged: "Damaged",
            failed: "Failed",
            unknown: "Unknown tier",
        };
        return labels[item.health_tier] || item.health_tier.replaceAll("_", " ");
    }

    function planDecisionLabel(item: RecoveredChannelPlanItem): string {
        if (item.decision === "approved") return "Prior: approved";
        if (item.decision === "held") return "Prior: held";
        return "Prior: pending";
    }

    function itemFindings(item: RecoveredChannelItem): string[] {
        const values = [
            ...item.findings,
            ...item.warnings,
            ...(item.error ? [item.error] : []),
        ];
        return Array.from(new Set(values)).slice(0, 12);
    }

    function canApproveItem(item: RecoveredChannelItem): boolean {
        return (
            !expectedDurationChanged(item) &&
            item.inspection_status === "completed" &&
            ["clear", "minimal", "serious"].includes(item.severity) &&
            typeof item.sha256 === "string" &&
            /^[0-9a-f]{64}$/i.test(item.sha256)
        );
    }

    function countFromCase(
        counts: Record<string, number> | undefined,
        names: string[],
        fallback: number,
    ): number {
        const values = counts || {};
        for (const name of names) {
            if (Number.isFinite(values[name])) return values[name];
        }
        return fallback;
    }

    function optionalCountFromCase(
        counts: Record<string, number> | undefined,
        names: string[],
    ): number | null {
        const values = counts || {};
        for (const name of names) {
            if (Number.isFinite(values[name])) return values[name];
        }
        return null;
    }

    function jobProgress(): number {
        const current = numeric(activeJob?.current);
        const total = numeric(activeJob?.total);
        const derived = total > 0 ? (current / total) * 100 : 0;
        return Math.max(
            0,
            Math.min(
                100,
                numeric(
                    activeJob?.progress_percent ||
                        activeJob?.progress ||
                        derived,
                ),
            ),
        );
    }

    function leaveWorkflow() {
        if (
            dirtyExpectedDurationItems.length > 0 &&
            !confirm(
                `Leave this investigation and discard ${dirtyExpectedDurationItems.length} unsaved expected-duration ${dirtyExpectedDurationItems.length === 1 ? "edit" : "edits"}?`,
            )
        )
            return;
        dispatch("exit", { changed });
    }

    onMount(() => {
        void discoverExistingCase();
        void tick().then(() => backButton?.focus());
    });

    onDestroy(() => {
        destroyed = true;
        if (pollTimer) clearTimeout(pollTimer);
        if (repairPollTimer) clearTimeout(repairPollTimer);
    });

    $: unresolvedItems = items.filter((item) => !itemIsPublished(item));
    $: publishedItems = items.filter(itemIsPublished);
    $: repairNeededCount = Math.max(
        investigation?.repair_needed || 0,
        publishedItems.filter(canRepairPublishedLink).length,
    );
    $: healthyPublishedCount = publishedItems.filter(
        (item) => item.link_status === "healthy",
    ).length;
    $: unhealthyPublishedCount = publishedItems.filter(
        (item) => item.link_status !== "healthy",
    ).length;
    $: linkHealthAttention =
        repairNeededCount > 0 || unhealthyPublishedCount > 0;
    $: dirtyExpectedDurationItems = unresolvedItems.filter(
        expectedDurationChanged,
    );
    $: if (
        (dirtyExpectedDurationItems.length > 0) !==
        reportedDirtyExpectedDurations
    ) {
        reportedDirtyExpectedDurations = dirtyExpectedDurationItems.length > 0;
        dispatch("dirtychange", { dirty: reportedDirtyExpectedDurations });
    }
    $: likelyItems = unresolvedItems.filter(isLikelyUsable);
    $: reviewItems = unresolvedItems.filter((item) => !isLikelyUsable(item));
    $: authoritativeLikelyCount = optionalCountFromCase(
        investigation?.counts,
        ["remaining_best", "best", "likely_usable", "likely", "clear_or_minimal"],
    );
    $: likelyCount = authoritativeLikelyCount ?? 0;
    $: authoritativeReviewCount = optionalCountFromCase(
        investigation?.counts,
        ["remaining_review", "needs_review", "review", "serious_or_inconclusive"],
    );
    $: reviewCount = authoritativeReviewCount ?? 0;
    $: approvedCount = countFromCase(
        investigation?.counts,
        ["approved", "remaining_approved", "decision_approved"],
        unresolvedItems.filter((item) => item.decision === "approved").length,
    );
    $: heldCount = countFromCase(
        investigation?.counts,
        ["hold", "held", "decision_hold"],
        unresolvedItems.filter((item) => item.decision === "held").length,
    );
    $: publishedCount = countFromCase(
        investigation?.counts,
        ["organized", "published"],
        publishedItems.length,
    );
    $: totalCount = Math.max(
        sourceItems.length,
        itemsTotal,
        investigation?.total || 0,
    );
    $: authoritativeRemainingCount = optionalCountFromCase(
        investigation?.counts,
        ["remaining", "remaining_count", "unresolved", "unpublished"],
    );
    $: remainingCount =
        authoritativeRemainingCount ?? Math.max(0, totalCount - publishedCount);
    $: completedCount = Math.min(
        totalCount,
        (investigation?.completed || 0) + (investigation?.failed || 0),
    );
    $: inspectionScopeTotal = countFromCase(
        investigation?.counts,
        ["remaining"],
        totalCount,
    );
    $: inspectionScopeFinished = countFromCase(
        investigation?.counts,
        ["remaining_finished"],
        completedCount,
    );
    $: hasMoreItems = items.length < itemsTotal;
    $: organizationRunning =
        activeJobKind === "organize" &&
        Boolean(activeJob) &&
        !terminalJobStatuses.has(jobStatus(activeJob));
</script>

<section class="channel-workflow" aria-labelledby="channel-workflow-title">
    <header class="workflow-header">
        <button bind:this={backButton} class="back-button" type="button" on:click={leaveWorkflow}>
            <i class="bi bi-arrow-left" aria-hidden="true"></i>
            Back to Unindexed
        </button>
        <div class="workflow-heading">
            <span>Recovered channel investigation</span>
            <strong id="channel-workflow-title">{sourceChannelId}</strong>
        </div>
        <span class="workflow-count">{totalCount} {totalCount === 1 ? "video" : "videos"}</span>
    </header>

    <nav class="workflow-steps" aria-label="Recovered channel progress">
        <ol>
            {#each [
                ["channel", "1", "Channel"],
                ["inspect", "2", "Inspect"],
                ["review", "3", "Review"],
                ["plan", "4", "Organize"],
            ] as entry}
                <li
                    class:active={step === entry[0] || (step === "complete" && entry[0] === "plan")}
                    class:complete={
                        (["channel", "inspect", "review", "plan", "complete"].indexOf(step) >
                            ["channel", "inspect", "review", "plan", "complete"].indexOf(entry[0]))
                    }
                    aria-current={step === entry[0] ? "step" : undefined}
                >
                    <span>{entry[1]}</span>
                    <strong>{entry[2]}</strong>
                </li>
            {/each}
        </ol>
    </nav>

    {#if error}
        <div class="workflow-notice workflow-notice--error" role="alert">
            <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
            <span>{error}</span>
            <button type="button" on:click={() => (error = null)} aria-label="Dismiss error">
                <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
        </div>
    {/if}

    {#if notice}
        <div class="workflow-notice" role="status">
            <i class="bi bi-info-circle" aria-hidden="true"></i>
            <span>{notice}</span>
            <button type="button" on:click={() => (notice = null)} aria-label="Dismiss notice">
                <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
        </div>
    {/if}

    <div class="workflow-body" aria-busy={loadingInitial || actionBusy}>
        {#if loadingInitial}
            <div class="workflow-empty" aria-live="polite">
                <i class="bi bi-arrow-repeat spin" aria-hidden="true"></i>
                <strong>Checking for a saved investigation…</strong>
                <span>Durable channel work can be resumed after leaving Recovery.</span>
            </div>
        {:else if step === "channel"}
            <section class="step-panel" aria-labelledby="workflow-channel-heading">
                <header>
                    <div>
                        <span class="step-eyebrow">Step 1</span>
                        <h3 bind:this={stepHeading} tabindex="-1" id="workflow-channel-heading">Name the recovered channel</h3>
                        <p>The raw source channel ID remains attached to every investigation record.</p>
                    </div>
                </header>

                <div class="channel-form">
                    <div class="source-identity">
                        <span>Raw source channel ID</span>
                        <code>{sourceChannelId}</code>
                        <small>{sourceItems.length} unindexed source {sourceItems.length === 1 ? "video" : "videos"} in the current scan</small>
                    </div>

                    <label for="recovered-channel-name">Recovered channel name</label>
                    <input
                        id="recovered-channel-name"
                        bind:value={targetChannelName}
                        maxlength="160"
                        autocomplete="off"
                        spellcheck="false"
                        placeholder="The Oversphere"
                        disabled={actionBusy}
                    />
                    <p class="field-help">Enter only the human-readable name. The recovered suffix is added by the server.</p>

                    <div class="target-preview">
                        <span>Target folder preview</span>
                        <code>{requestedTargetComponent()}</code>
                        <small>No folder or link is created until the final organization step.</small>
                    </div>
                </div>

                <section class="legacy-attach" aria-labelledby="legacy-attach-heading">
                    <div>
                        <span class="step-eyebrow">Existing archive edge case</span>
                        <h4 id="legacy-attach-heading">Pair an existing Target channel folder</h4>
                        <p>Use this when human-readable video folders and symlinks already exist, but TubeArchivist metadata does not. Detection uses exact symlink targets—not folder or title resemblance.</p>
                    </div>
                    <label for="existing-target-component">Exact Target folder name <small>(optional for auto-detect)</small></label>
                    <div class="legacy-attach-controls">
                        <input
                            id="existing-target-component"
                            bind:value={existingTargetComponent}
                            placeholder="Western Decline"
                            autocomplete="off"
                            spellcheck="false"
                            disabled={actionBusy}
                            on:input={() => { attachPlan = null; attachConfirmation = ""; }}
                        />
                        <button type="button" on:click={() => buildExistingTargetPlan()} disabled={actionBusy}>
                            <i class="bi bi-diagram-3" aria-hidden="true"></i>
                            {actionBusy ? "Scanning…" : existingTargetComponent.trim() ? "Build attach dry run" : "Auto-detect folder"}
                        </button>
                    </div>

                    {#if attachPlan && text(attachPlan.status) === "selection_required"}
                        <div class="attach-candidates">
                            <strong>Exact-link candidates</strong>
                            {#if Array.isArray(attachPlan.candidates) && attachPlan.candidates.length}
                                {#each attachPlan.candidates as candidate}
                                    <button type="button" on:click={() => { existingTargetComponent = text(candidate.target_component); void buildExistingTargetPlan(existingTargetComponent); }} disabled={actionBusy}>
                                        <span>{text(candidate.target_component)}</span>
                                        <small>{numeric(candidate.exact_unique_links) + numeric(candidate.already_tracked_links)} exact links · {numeric(candidate.source_only)} source-only</small>
                                    </button>
                                {/each}
                            {:else}
                                <p>No Target folder contained an exact symlink target for this raw channel ID. Enter the exact folder name to inspect it directly.</p>
                            {/if}
                        </div>
                    {:else if attachPlan && text(attachPlan.status) === "ready"}
                        {@const counts = asRecord(attachPlan.counts)}
                        <div class="attach-summary" role="status">
                            <dl>
                                <div><dt>Raw source videos</dt><dd>{numeric(counts.source_total)}</dd></div>
                                <div><dt>Existing exact links</dt><dd>{numeric(counts.exact_unique_links)}</dd></div>
                                <div class:attention={numeric(counts.source_only) > 0}><dt>Source-only / unaccounted</dt><dd>{numeric(counts.source_only)}</dd></div>
                                <div><dt>Already tracked</dt><dd>{numeric(counts.already_tracked)}</dd></div>
                                <div><dt>Unrelated real files</dt><dd>{numeric(counts.real_media) + numeric(counts.unrelated_files)}</dd></div>
                                <div class:attention={numeric(counts.ambiguous) + numeric(counts.broken_links) > 0}><dt>Ambiguous or broken</dt><dd>{numeric(counts.ambiguous) + numeric(counts.broken_links)}</dd></div>
                            </dl>
                            {#if Array.isArray(attachPlan.source_only) && attachPlan.source_only.length}
                                <details open>
                                    <summary>Source-only videos ({attachPlan.source_only.length})</summary>
                                    <ul>
                                        {#each attachPlan.source_only as missing}
                                            <li><code>{text(missing.video_id)}</code><span>{text(missing.filename)}</span></li>
                                        {/each}
                                    </ul>
                                </details>
                            {/if}
                            <p><strong>Filesystem changes: none.</strong> Applying records proven ownership of unique exact links; it does not rename, move, delete, replace, or create media.</p>
                            <label for="attach-confirmation">Type <code>{text(attachPlan.confirmation)}</code> to attach</label>
                            <div class="legacy-attach-controls">
                                <input id="attach-confirmation" bind:value={attachConfirmation} autocomplete="off" spellcheck="false" disabled={actionBusy} />
                                <button class="primary-action" type="button" on:click={applyExistingTargetPlan} disabled={actionBusy || attachConfirmation !== text(attachPlan.confirmation)}>
                                    Attach existing folder
                                </button>
                            </div>
                        </div>
                    {/if}
                </section>

                <footer class="step-actions">
                    <button class="primary-action" type="button" on:click={createCase} disabled={!targetChannelName.trim() || actionBusy}>
                        <i class="bi bi-arrow-right-circle" aria-hidden="true"></i>
                        {actionBusy ? "Creating…" : "Continue to inspection"}
                    </button>
                </footer>
            </section>
        {:else if step === "inspect"}
            <section class="step-panel" aria-labelledby="workflow-inspect-heading">
                <header>
                    <div>
                        <span class="step-eyebrow">Step 2</span>
                        <h3 bind:this={stepHeading} tabindex="-1" id="workflow-inspect-heading">Inspect every source video</h3>
                        <p>Strict sequential decoding checks container and codec integrity without modifying the source.</p>
                    </div>
                </header>

                <div class="inspection-summary">
                    <div class="inspection-progress">
                        <div>
                            <strong>{inspectionScopeFinished} of {inspectionScopeTotal}</strong>
                            <span>inspections finished</span>
                        </div>
                        <span>{inspectionScopeTotal ? Math.round((inspectionScopeFinished / inspectionScopeTotal) * 100) : 0}%</span>
                    </div>
                    <progress max={Math.max(1, inspectionScopeTotal)} value={inspectionScopeFinished}>{inspectionScopeFinished} of {inspectionScopeTotal}</progress>

                    {#if activeJob && !terminalJobStatuses.has(jobStatus(activeJob))}
                        <div class="active-work" aria-live="polite">
                            <i class="bi bi-arrow-repeat spin" aria-hidden="true"></i>
                            <div>
                                <strong>{text(activeJob.phase || activeJob.message, "Inspection running")}</strong>
                                <span>{Math.round(jobProgress())}% of the current background job</span>
                            </div>
                        </div>
                    {:else}
                        <div class="inspection-explanation">
                            <i class="bi bi-shield-check" aria-hidden="true"></i>
                            <p>A clean decode checks whether streams can be read. It cannot prove that a picture or soundtrack has no subtle historical distortion, so preview remains the final human check.</p>
                        </div>
                    {/if}

                    <dl class="inspection-counts">
                        <div><dt>To investigate</dt><dd>{inspectionScopeTotal}</dd></div>
                        <div><dt>Finished</dt><dd>{inspectionScopeFinished}</dd></div>
                        <div><dt>Needs review</dt><dd>{countFromCase(investigation?.counts, ["review"], 0)}</dd></div>
                        <div><dt>Remaining</dt><dd>{Math.max(0, inspectionScopeTotal - inspectionScopeFinished)}</dd></div>
                    </dl>
                </div>

                <footer class="step-actions split-actions">
                    {#if activeJob && activeJobKind === "inspect" && !terminalJobStatuses.has(jobStatus(activeJob))}
                        <button type="button" on:click={cancelInspection} disabled={actionBusy}>
                            <i class="bi bi-stop-circle" aria-hidden="true"></i>
                            Cancel remaining
                        </button>
                    {:else}
                        <button class="primary-action" type="button" on:click={() => startInspection(!hasItemsNeedingInspection())} disabled={actionBusy || inspectionScopeTotal === 0 || dirtyExpectedDurationItems.length > 0} title={dirtyExpectedDurationItems.length > 0 ? "Save or discard expected-duration edits first" : undefined}>
                            <i class="bi bi-activity" aria-hidden="true"></i>
                            {hasItemsNeedingInspection() ? inspectionScopeFinished > 0 ? "Resume remaining inspection" : "Inspect unaccounted videos" : "Run full inspection again"}
                        </button>
                        {#if inspectionScopeFinished > 0}
                            <button type="button" on:click={() => moveTo("review")}>
                                Review results
                                <i class="bi bi-arrow-right" aria-hidden="true"></i>
                            </button>
                        {/if}
                    {/if}
                </footer>
            </section>
        {:else if step === "review"}
            <section class="step-panel review-step" aria-labelledby="workflow-review-heading">
                <header>
                    <div>
                        <span class="step-eyebrow">Step 3</span>
                        <h3 bind:this={stepHeading} tabindex="-1" id="workflow-review-heading">Review condition and choose what to publish</h3>
                        <p>Approve videos individually, or explicitly prepare every eligible Likely usable video. Held videos always remain for later work.</p>
                    </div>
                    <div class="decision-summary" aria-live="polite">
                        <strong>{approvedCount} approved</strong>
                        <span>{heldCount} held</span>
                    </div>
                </header>

                <div class="integrity-limit" role="note">
                    <i class="bi bi-eye" aria-hidden="true"></i>
                    <span><strong>Automated evidence is only a triage aid.</strong> “No decoder errors found” does not guarantee visual or audible authenticity. Watch or listen before approving when identity or quality is uncertain.</span>
                </div>

                <div class="mobile-bucket-tabs" role="group" aria-label="Choose condition list">
                    <button
                        id="recovered-bucket-likely"
                        type="button"
                        aria-controls="recovered-likely-list"
                        class:active={mobileReviewBucket === "likely"}
                        aria-pressed={mobileReviewBucket === "likely"}
                        on:click={() => selectMobileReviewBucket("likely")}
                        on:keydown={(event) => handleMobileBucketKeydown(event, "likely")}
                    >
                        Likely usable <span>{likelyCount}</span>
                    </button>
                    <button
                        id="recovered-bucket-review"
                        type="button"
                        aria-controls="recovered-review-list"
                        class:active={mobileReviewBucket === "review"}
                        aria-pressed={mobileReviewBucket === "review"}
                        on:click={() => selectMobileReviewBucket("review")}
                        on:keydown={(event) => handleMobileBucketKeydown(event, "review")}
                    >
                        Needs review <span>{reviewCount}</span>
                    </button>
                </div>

                <div class="review-columns">
                    <section id="recovered-likely-list" class="condition-list condition-list--likely" class:mobile-hidden={mobileReviewBucket !== "likely"} aria-labelledby="likely-list-heading">
                        <header>
                            <div>
                                <h4 id="likely-list-heading"><i class="bi bi-check2-circle" aria-hidden="true"></i> Likely usable</h4>
                                <p>Strict decode completed clearly or with only minimal evidence.</p>
                            </div>
                            <span>{likelyCount}</span>
                        </header>
                        <div class="likely-plan-action">
                            <div>
                                <strong>Publish the strongest candidates first</strong>
                                <span>
                                    {#if authoritativeLikelyCount === null}
                                        The server will count eligible clear/minimal videos across the whole investigation.
                                    {:else}
                                        {authoritativeLikelyCount} clear/minimal {authoritativeLikelyCount === 1 ? "video is" : "videos are"} classified across the whole investigation.
                                    {/if}
                                </span>
                            </div>
                            <button
                                bind:this={likelyPlanTrigger}
                                class="primary-action"
                                type="button"
                                aria-expanded={likelyPlanConfirmationOpen}
                                aria-controls="likely-plan-confirmation"
                                on:click={openLikelyPlanConfirmation}
                                disabled={actionBusy || authoritativeLikelyCount === 0 || dirtyExpectedDurationItems.length > 0}
                                title={dirtyExpectedDurationItems.length > 0 ? "Save or discard expected-duration edits first" : undefined}
                            >
                                <i class="bi bi-shield-check" aria-hidden="true"></i>
                                Prepare likely usable
                            </button>
                        </div>
                        {#if likelyPlanConfirmationOpen}
                            <section
                                id="likely-plan-confirmation"
                                class="likely-plan-confirmation"
                                aria-labelledby="likely-plan-confirmation-heading"
                                aria-describedby="likely-plan-confirmation-description"
                                on:keydown={handleLikelyPlanConfirmationKeydown}
                            >
                                <div>
                                    <span class="step-eyebrow">Scoped dry run</span>
                                    <h5 bind:this={likelyPlanConfirmationHeading} tabindex="-1" id="likely-plan-confirmation-heading">Prepare every eligible Likely usable video?</h5>
                                    <p id="likely-plan-confirmation-description">The server—not the loaded page—will consider all completed clear/minimal candidates in this investigation that are not held.</p>
                                </div>
                                <ul>
                                    <li><strong>Pending and already-approved Likely videos are included.</strong> Load a Likely video and use Hold before continuing when it must be excluded.</li>
                                    <li><strong>Needs closer review stays untouched,</strong> even when you approved one of those videos manually.</li>
                                    <li><strong>Held videos stay held.</strong> Their decision will not be changed.</li>
                                    <li><strong>Planning changes no decisions and creates no links.</strong> After you confirm the plan, only pending videos whose links publish successfully become approved.</li>
                                </ul>
                                <div class="likely-plan-confirmation-actions">
                                    <button type="button" on:click={() => closeLikelyPlanConfirmation()} disabled={actionBusy}>Cancel</button>
                                    <button class="primary-action" type="button" on:click={() => buildPlan("likely_usable")} disabled={actionBusy || dirtyExpectedDurationItems.length > 0}>
                                        {actionBusy ? "Building dry run…" : "Create likely-only dry run"}
                                        <i class="bi bi-arrow-right" aria-hidden="true"></i>
                                    </button>
                                </div>
                            </section>
                        {/if}
                        <div class="media-list">
                            {#each likelyItems as item (item.id)}
                                <article class:approved={item.decision === "approved"} class:held={item.decision === "held"} aria-labelledby={`recovered-item-${item.id}`}>
                                    <div class="media-heading">
                                        <span class="condition-badge condition-badge--likely">{conditionLabel(item)}</span>
                                        <h5 id={`recovered-item-${item.id}`}>{item.filename || item.video_id}</h5>
                                        <code>{item.video_id}</code>
                                    </div>
                                    <dl class="media-facts">
                                        <div><dt>Size</dt><dd>{formatBytes(item.size_bytes)}</dd></div>
                                        <div><dt>Reported</dt><dd>{formatDuration(item.duration_seconds)}</dd></div>
                                        <div><dt>Readable</dt><dd>{formatDuration(item.readable_duration_seconds)}</dd></div>
                                    </dl>
                                    <p class="stream-summary">{streamSummary(item)}</p>
                                    <code class="source-path">{item.path}</code>
                                    {#if itemFindings(item).length}
                                        <details>
                                            <summary>Inspection details ({itemFindings(item).length})</summary>
                                            <ul>{#each itemFindings(item) as finding}<li>{finding}</li>{/each}</ul>
                                        </details>
                                    {/if}
                                    <div class="duration-editor">
                                        <div class="duration-editor-heading">
                                            <label for={`expected-duration-${item.id}`}>Expected full duration <span>optional</span></label>
                                            {#if item.expected_duration_seconds}
                                                <span>Saved: {expectedDurationDisplay(item.expected_duration_seconds)}</span>
                                            {/if}
                                        </div>
                                        <div class="duration-editor-controls">
                                            <input
                                                id={`expected-duration-${item.id}`}
                                                type="text"
                                                inputmode="text"
                                                enterkeyhint="done"
                                                autocomplete="off"
                                                placeholder="10:00 or 600 seconds"
                                                value={expectedDurationInput(item)}
                                                aria-invalid={Boolean(expectedDurationErrors[item.id])}
                                                aria-describedby={`expected-duration-help-${item.id}${expectedDurationErrors[item.id] ? ` expected-duration-error-${item.id}` : ""}`}
                                                on:input={(event) => setExpectedDurationDraft(item.id, event.currentTarget.value)}
                                                on:keydown={(event) => {
                                                    if (event.key === "Enter") {
                                                        event.preventDefault();
                                                        void saveExpectedDuration(item);
                                                    }
                                                }}
                                            />
                                            <button type="button" on:click={() => saveExpectedDuration(item)} disabled={updatingItemIds.has(item.id) || !expectedDurationChanged(item)}>Save</button>
                                            {#if expectedDurationChanged(item)}
                                                <button type="button" on:click={() => discardExpectedDurationDraft(item)} disabled={updatingItemIds.has(item.id)}>Discard</button>
                                            {/if}
                                            <button type="button" on:click={() => saveExpectedDuration(item, true)} disabled={updatingItemIds.has(item.id) || !item.expected_duration_seconds}>Clear</button>
                                        </div>
                                        <p id={`expected-duration-help-${item.id}`}>Saving or clearing this baseline resets the prior diagnosis and approval. Reinspection is required.</p>
                                        {#if expectedDurationErrors[item.id]}
                                            <p id={`expected-duration-error-${item.id}`} class="duration-editor-error" role="alert">{expectedDurationErrors[item.id]}</p>
                                        {/if}
                                    </div>
                                    <div class="media-actions">
                                        <button type="button" on:click={() => openPreview(item, "video")} disabled={!item.path}><i class="bi bi-play-circle" aria-hidden="true"></i> Preview</button>
                                        <button type="button" on:click={() => openPreview(item, "audio")} disabled={!item.path}><i class="bi bi-headphones" aria-hidden="true"></i> Listen</button>
                                        {#if numeric(item.reference_repair_count) > 0}
                                            <button type="button" on:click={() => openReferenceRepair(item)} disabled={referenceRepairBusy}><i class="bi bi-clock-history" aria-hidden="true"></i> Repair history</button>
                                        {/if}
                                    </div>
                                    <div class="decision-actions" aria-label={`Decision for ${item.filename || item.video_id}`}>
                                        <button
                                            type="button"
                                            class:active={item.decision === "approved"}
                                            aria-pressed={item.decision === "approved"}
                                            on:click={() => updateDecision(item, item.decision === "approved" ? "undecided" : "approved")}
                                            disabled={updatingItemIds.has(item.id) || !canApproveItem(item)}
                                            title={expectedDurationChanged(item) ? "Save or discard the expected-duration edit before approval" : canApproveItem(item) ? "Approve this checksum-bound inspection" : "A completed checksum-bound inspection is required before approval"}
                                        ><i class="bi bi-check2-square" aria-hidden="true"></i> {expectedDurationChanged(item) ? "Save duration first" : item.decision === "approved" ? "Approved" : canApproveItem(item) ? "Approve" : "Inspection required"}</button>
                                        <button
                                            type="button"
                                            class:active={item.decision === "held"}
                                            aria-pressed={item.decision === "held"}
                                            on:click={() => updateDecision(item, item.decision === "held" ? "undecided" : "held")}
                                            disabled={updatingItemIds.has(item.id)}
                                        ><i class="bi bi-pause-circle" aria-hidden="true"></i> {item.decision === "held" ? "Held" : "Hold"}</button>
                                    </div>
                                </article>
                            {:else}
                                <div class="list-empty"><i class="bi bi-inbox" aria-hidden="true"></i><span>No loaded videos are in this condition list.</span></div>
                            {/each}
                        </div>
                    </section>

                    <section id="recovered-review-list" class="condition-list condition-list--review" class:mobile-hidden={mobileReviewBucket !== "review"} aria-labelledby="review-list-heading">
                        <header>
                            <div>
                                <h4 id="review-list-heading"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i> Needs closer review</h4>
                                <p>Serious, inconclusive, unreadable, or failed inspection evidence.</p>
                            </div>
                            <span>{reviewCount}</span>
                        </header>
                        {#if repairDamaged}
                            <section class="reference-repair-panel" aria-labelledby="reference-repair-heading">
                                <header>
                                    <div>
                                        <span class="step-eyebrow">UNTRUNC reference repair</span>
                                        <h5 id="reference-repair-heading">{repairDamaged.filename || repairDamaged.video_id}</h5>
                                        <p>A healthy video supplies container and track structure only. It does not donate or reconstruct missing pictures or audio.</p>
                                    </div>
                                    <button type="button" aria-label="Close reference repair" on:click={closeReferenceRepair}><i class="bi bi-x-lg" aria-hidden="true"></i></button>
                                </header>

                                {#if referenceRepairError}
                                    <p class="reference-repair-error" role="alert"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i>{referenceRepairError}</p>
                                {/if}

                                {#if repairHistory.length > 0}
                                    <div class="repair-history-selector">
                                        <label for="reference-repair-history">Repair attempt</label>
                                        <select id="reference-repair-history" value={activeReferenceRepair?.repair_id || ""} on:change={(event) => selectRepairAttempt(event.currentTarget.value)}>
                                            {#each repairHistory as attempt}
                                                <option value={attempt.repair_id}>{attempt.reference_video_id || attempt.reference_item_id} · {attempt.status} · {repairAssessmentLabel(attempt.assessment)}</option>
                                            {/each}
                                        </select>
                                        {#if activeReferenceRepair}
                                            <span>Created {activeReferenceRepair.created_at || "unknown"} · Tool {activeReferenceRepair.tool_version || "not recorded"}</span>
                                        {/if}
                                    </div>
                                {/if}

                                {#if itemEligibleForReferenceRepair(repairDamaged) && (!activeReferenceRepair || terminalJobStatuses.has(activeReferenceRepair.status))}
                                    <div class="reference-selector">
                                        <label for="reference-repair-candidate">Healthy reference</label>
                                        {#if repairCompatibilityUncertain}
                                            <p class="compatibility-warning"><i class="bi bi-question-diamond" aria-hidden="true"></i>The damaged container exposes no streams, so compatibility is reference-led and uncertain.</p>
                                        {/if}
                                        <select id="reference-repair-candidate" bind:value={selectedReferenceId} on:change={() => (acknowledgeWeakReference = false)} disabled={referenceRepairBusy || repairReferences.length === 0}>
                                            <option value="">Choose a healthy reference…</option>
                                            {#each repairReferences as reference}
                                                <option value={reference.item_id}>{reference.filename} · {reference.compatibility} · score {reference.score}</option>
                                            {/each}
                                        </select>
                                        {#if selectedReferenceId}
                                            {@const selectedReference = repairReferences.find((entry) => entry.item_id === selectedReferenceId)}
                                            {#if selectedReference}
                                                <div class="reference-rationale">
                                                    <strong>{selectedReference.video_id}</strong>
                                                    <span>{selectedReference.reasons.join(" · ")}</span>
                                                </div>
                                                <div class="reference-preview">
                                                    <video src={selectedReference.preview_url} controls playsinline preload="metadata"></video>
                                                    <audio src={selectedReference.preview_audio_url} controls preload="metadata"></audio>
                                                </div>
                                                {#if selectedReferenceNeedsAcknowledgement()}
                                                    <label class="compatibility-acknowledgement">
                                                        <input type="checkbox" bind:checked={acknowledgeWeakReference} />
                                                        I understand this reference is not a strong technical match and UNTRUNC may produce a worse result.
                                                    </label>
                                                {/if}
                                            {/if}
                                        {:else if !referenceRepairBusy}
                                            <p>No completed clear/minimal MP4-family reference exists in this recovered channel.</p>
                                        {/if}
                                        <div class="reference-repair-actions">
                                            {#if activeReferenceRepair && ["failed", "cancelled", "interrupted"].includes(activeReferenceRepair.status)}
                                                <button type="button" on:click={retryReferenceRepair} disabled={referenceRepairBusy}><i class="bi bi-arrow-clockwise" aria-hidden="true"></i> Retry same pair</button>
                                            {/if}
                                            <button class="primary-action" type="button" on:click={startReferenceRepair} disabled={referenceRepairBusy || !selectedReferenceId || (selectedReferenceNeedsAcknowledgement() && !acknowledgeWeakReference)}>
                                                <i class="bi bi-tools" aria-hidden="true"></i>{referenceRepairBusy ? "Starting…" : "Run UNTRUNC privately"}
                                            </button>
                                        </div>
                                    </div>
                                {/if}

                                {#if activeReferenceRepair}
                                    <div class="repair-job-state" data-status={activeReferenceRepair.status}>
                                        <div><strong>{activeReferenceRepair.status}</strong><span>{activeReferenceRepair.phase || "queued"}</span></div>
                                        <progress max="100" value={numeric(activeReferenceRepair.progress)}></progress>
                                        <span>{Math.round(numeric(activeReferenceRepair.progress))}%</span>
                                    </div>
                                    {#if !terminalJobStatuses.has(activeReferenceRepair.status)}
                                        <button type="button" on:click={cancelReferenceRepair} disabled={referenceRepairBusy}>Cancel repair</button>
                                    {/if}
                                    {#if activeReferenceRepair.error}
                                        <p class="reference-repair-error" role="alert">{activeReferenceRepair.error}</p>
                                    {/if}
                                {/if}

                                {#if activeReferenceRepair?.status === "completed" || activeReferenceRepair?.decision === "adopted" || activeReferenceRepair?.decision === "restored"}
                                    <div class="repair-assessment" data-assessment={activeReferenceRepair.assessment}>
                                        <span>Nonbinding assessment</span>
                                        <strong>{repairAssessmentLabel(activeReferenceRepair.assessment)}</strong>
                                        <p>A clean technical report cannot prove visual or audible authenticity. Watch, listen, and seek through both copies.</p>
                                    </div>
                                    <div class="repair-comparison">
                                        <article>
                                            <h6>{activeReferenceRepair.decision === "adopted" ? "Verified original backup" : activeReferenceRepair.decision === "restored" ? "Restored original source" : "Original source"}</h6>
                                            <video src={activeReferenceRepair.decision === "adopted" ? activeReferenceRepair.backup_preview_url : `/api/recovery/preview?filepath=${encodeURIComponent(repairDamaged.path)}`} controls playsinline preload="metadata"></video>
                                            <audio src={activeReferenceRepair.decision === "adopted" ? activeReferenceRepair.backup_audio_url : `/api/recovery/preview-audio?filepath=${encodeURIComponent(repairDamaged.path)}`} controls preload="metadata"></audio>
                                            <dl>
                                                <div><dt>Streams</dt><dd>{repairReportStreams("before")}</dd></div>
                                                <div><dt>Duration</dt><dd>{formatDuration(repairReport("before").duration_seconds)}</dd></div>
                                                <div><dt>Decoded to</dt><dd>{formatDuration(repairReport("before").decoded_endpoint_seconds)}</dd></div>
                                                <div><dt>Seekable</dt><dd>{repairReport("before").seekable === true ? "Yes" : repairReport("before").seekable === false ? "No" : "Unknown"}</dd></div>
                                                <div><dt>Decode</dt><dd>{repairReport("before").decode_clean === true ? "Clean" : repairReport("before").probeable === false ? "Not probeable" : "Warnings/errors"}</dd></div>
                                                <div><dt>Size</dt><dd>{formatBytes(repairReport("before").size_bytes)}</dd></div>
                                                <div><dt>Errors</dt><dd>{Array.isArray(repairReport("before").errors) && repairReport("before").errors.length ? repairReport("before").errors.join(" · ") : "None reported"}</dd></div>
                                                <div><dt>SHA-256</dt><dd><code>{text(activeReferenceRepair.source_sha256).slice(0, 16)}…</code></dd></div>
                                            </dl>
                                        </article>
                                        <article>
                                            <h6>UNTRUNC candidate</h6>
                                            <video src={activeReferenceRepair.candidate_preview_url} controls playsinline preload="metadata"></video>
                                            <audio src={activeReferenceRepair.candidate_audio_url} controls preload="metadata"></audio>
                                            <dl>
                                                <div><dt>Streams</dt><dd>{repairReportStreams("after")}</dd></div>
                                                <div><dt>Duration</dt><dd>{formatDuration(repairReport("after").duration_seconds)}</dd></div>
                                                <div><dt>Decoded to</dt><dd>{formatDuration(repairReport("after").decoded_endpoint_seconds)}</dd></div>
                                                <div><dt>Seekable</dt><dd>{repairReport("after").seekable === true ? "Yes" : repairReport("after").seekable === false ? "No" : "Unknown"}</dd></div>
                                                <div><dt>Decode</dt><dd>{repairReport("after").decode_clean ? "Clean" : "Warnings/errors"}</dd></div>
                                                <div><dt>Size</dt><dd>{formatBytes(repairReport("after").size_bytes)}</dd></div>
                                                <div><dt>Errors</dt><dd>{Array.isArray(repairReport("after").errors) && repairReport("after").errors.length ? repairReport("after").errors.join(" · ") : "None reported"}</dd></div>
                                                <div><dt>SHA-256</dt><dd><code>{text(activeReferenceRepair.candidate_sha256).slice(0, 16)}…</code></dd></div>
                                            </dl>
                                        </article>
                                    </div>
                                    {#if activeReferenceRepair.decision === "adopted"}
                                        <button type="button" on:click={() => prepareRepairAction("restore")} disabled={referenceRepairBusy}><i class="bi bi-arrow-counterclockwise" aria-hidden="true"></i> Prepare Restore Original</button>
                                    {:else if activeReferenceRepair.decision !== "restored"}
                                        <button class="primary-action" type="button" on:click={() => prepareRepairAction("adopt")} disabled={referenceRepairBusy}><i class="bi bi-shield-check" aria-hidden="true"></i> Prepare source replacement</button>
                                    {/if}
                                {/if}

                                {#if repairActionPlan}
                                    <section class="repair-action-plan" aria-label="Source replacement dry run">
                                        <h6>{repairActionPlan.action === "restore" ? "Restore dry run" : "Replacement dry run"}</h6>
                                        <dl>
                                            <div><dt>Source</dt><dd><code>{repairActionPlan.source_path}</code></dd></div>
                                            <div><dt>Permanent backup</dt><dd><code>{repairActionPlan.backup_path}</code></dd></div>
                                            <div><dt>Target changes</dt><dd>{repairActionPlan.target_mutations} — existing symlinks remain untouched</dd></div>
                                        </dl>
                                        <p>This token is checksum-bound, single-use, and expires in ten minutes. The server revalidates everything immediately before replacement.</p>
                                        <label class="repair-confirmation">
                                            Type <code>{repairActionPlan.confirmation}</code> to continue
                                            <input type="text" autocomplete="off" bind:value={repairConfirmationInput} />
                                        </label>
                                        <div class="reference-repair-actions">
                                            <button type="button" on:click={() => (repairActionPlan = null)} disabled={referenceRepairBusy}>Cancel</button>
                                            <button class="danger-action" type="button" on:click={applyRepairAction} disabled={referenceRepairBusy || repairConfirmationInput !== repairActionPlan.confirmation}>{referenceRepairBusy ? "Applying…" : repairActionPlan.action === "restore" ? "Restore verified original" : "Replace source with candidate"}</button>
                                        </div>
                                    </section>
                                {/if}
                            </section>
                        {/if}
                        <div class="media-list">
                            {#each reviewItems as item (item.id)}
                                <article class:approved={item.decision === "approved"} class:held={item.decision === "held"} aria-labelledby={`recovered-item-${item.id}`}>
                                    <div class="media-heading">
                                        <span class="condition-badge condition-badge--review">{conditionLabel(item)}</span>
                                        <h5 id={`recovered-item-${item.id}`}>{item.filename || item.video_id}</h5>
                                        <code>{item.video_id}</code>
                                    </div>
                                    <dl class="media-facts">
                                        <div><dt>Size</dt><dd>{formatBytes(item.size_bytes)}</dd></div>
                                        <div><dt>Reported</dt><dd>{formatDuration(item.duration_seconds)}</dd></div>
                                        <div><dt>Readable</dt><dd>{formatDuration(item.readable_duration_seconds)}</dd></div>
                                    </dl>
                                    <p class="stream-summary">{streamSummary(item)}</p>
                                    <code class="source-path">{item.path}</code>
                                    {#if itemFindings(item).length}
                                        <details open>
                                            <summary>Inspection details ({itemFindings(item).length})</summary>
                                            <ul>{#each itemFindings(item) as finding}<li>{finding}</li>{/each}</ul>
                                        </details>
                                    {/if}
                                    <div class="duration-editor">
                                        <div class="duration-editor-heading">
                                            <label for={`expected-duration-${item.id}`}>Expected full duration <span>optional</span></label>
                                            {#if item.expected_duration_seconds}
                                                <span>Saved: {expectedDurationDisplay(item.expected_duration_seconds)}</span>
                                            {/if}
                                        </div>
                                        <div class="duration-editor-controls">
                                            <input
                                                id={`expected-duration-${item.id}`}
                                                type="text"
                                                inputmode="text"
                                                enterkeyhint="done"
                                                autocomplete="off"
                                                placeholder="10:00 or 600 seconds"
                                                value={expectedDurationInput(item)}
                                                aria-invalid={Boolean(expectedDurationErrors[item.id])}
                                                aria-describedby={`expected-duration-help-${item.id}${expectedDurationErrors[item.id] ? ` expected-duration-error-${item.id}` : ""}`}
                                                on:input={(event) => setExpectedDurationDraft(item.id, event.currentTarget.value)}
                                                on:keydown={(event) => {
                                                    if (event.key === "Enter") {
                                                        event.preventDefault();
                                                        void saveExpectedDuration(item);
                                                    }
                                                }}
                                            />
                                            <button type="button" on:click={() => saveExpectedDuration(item)} disabled={updatingItemIds.has(item.id) || !expectedDurationChanged(item)}>Save</button>
                                            {#if expectedDurationChanged(item)}
                                                <button type="button" on:click={() => discardExpectedDurationDraft(item)} disabled={updatingItemIds.has(item.id)}>Discard</button>
                                            {/if}
                                            <button type="button" on:click={() => saveExpectedDuration(item, true)} disabled={updatingItemIds.has(item.id) || !item.expected_duration_seconds}>Clear</button>
                                        </div>
                                        <p id={`expected-duration-help-${item.id}`}>Saving or clearing this baseline resets the prior diagnosis and approval. Reinspection is required.</p>
                                        {#if expectedDurationErrors[item.id]}
                                            <p id={`expected-duration-error-${item.id}`} class="duration-editor-error" role="alert">{expectedDurationErrors[item.id]}</p>
                                        {/if}
                                    </div>
                                    <div class="media-actions">
                                        <button type="button" on:click={() => openPreview(item, "video")} disabled={!item.path}><i class="bi bi-play-circle" aria-hidden="true"></i> Preview</button>
                                        <button type="button" on:click={() => openPreview(item, "audio")} disabled={!item.path}><i class="bi bi-headphones" aria-hidden="true"></i> Listen</button>
                                        {#if itemEligibleForReferenceRepair(item) || numeric(item.reference_repair_count) > 0}
                                            <button type="button" on:click={() => openReferenceRepair(item)} disabled={referenceRepairBusy}><i class="bi bi-tools" aria-hidden="true"></i> {itemEligibleForReferenceRepair(item) ? "Reference repair" : "Repair history"}</button>
                                        {/if}
                                    </div>
                                    <div class="decision-actions" aria-label={`Decision for ${item.filename || item.video_id}`}>
                                        <button
                                            type="button"
                                            class:active={item.decision === "approved"}
                                            aria-pressed={item.decision === "approved"}
                                            on:click={() => updateDecision(item, item.decision === "approved" ? "undecided" : "approved")}
                                            disabled={updatingItemIds.has(item.id) || !canApproveItem(item)}
                                            title={expectedDurationChanged(item) ? "Save or discard the expected-duration edit before approval" : canApproveItem(item) ? "Approve this checksum-bound inspection after manual review" : "A completed checksum-bound inspection is required before approval"}
                                        ><i class="bi bi-check2-square" aria-hidden="true"></i> {expectedDurationChanged(item) ? "Save duration first" : item.decision === "approved" ? "Approved after review" : canApproveItem(item) ? "Approve after review" : "Inspection required"}</button>
                                        <button
                                            type="button"
                                            class:active={item.decision === "held"}
                                            aria-pressed={item.decision === "held"}
                                            on:click={() => updateDecision(item, item.decision === "held" ? "undecided" : "held")}
                                            disabled={updatingItemIds.has(item.id)}
                                        ><i class="bi bi-pause-circle" aria-hidden="true"></i> {item.decision === "held" ? "Held" : "Hold"}</button>
                                    </div>
                                </article>
                            {:else}
                                <div class="list-empty"><i class="bi bi-inbox" aria-hidden="true"></i><span>No loaded videos are in this condition list.</span></div>
                            {/each}
                        </div>
                    </section>
                </div>

                {#if hasMoreItems}
                    <button class="load-more" type="button" on:click={() => loadItems()} disabled={loadingItems}>
                        {loadingItems ? "Loading…" : `Load more videos (${items.length} of ${itemsTotal} loaded)`}
                    </button>
                {/if}

                {#if dirtyExpectedDurationItems.length > 0}
                    <div class="dirty-duration-warning" role="status">
                        <i class="bi bi-pencil-square" aria-hidden="true"></i>
                        <span><strong>{dirtyExpectedDurationItems.length} unsaved duration {dirtyExpectedDurationItems.length === 1 ? "edit" : "edits"}</strong> The edited videos cannot be approved, and all inspection, planning, and organization is blocked until every edit is saved or discarded.</span>
                    </div>
                {/if}

                <footer class="step-actions review-footer">
                    <button type="button" on:click={() => moveTo("inspect")}><i class="bi bi-arrow-left" aria-hidden="true"></i> Inspection</button>
                    {#if publishedCount > 0}
                        <button type="button" on:click={() => moveTo("complete")}><i class="bi bi-link-45deg" aria-hidden="true"></i> Published links ({publishedCount})</button>
                    {/if}
                    <div>
                        <span>{approvedCount} approved · {heldCount} held</span>
                        <button class="primary-action" type="button" on:click={() => buildPlan("approved")} disabled={approvedCount === 0 || actionBusy || dirtyExpectedDurationItems.length > 0} title={dirtyExpectedDurationItems.length > 0 ? "Save or discard expected-duration edits first" : undefined}>
                            Review organization plan
                            <i class="bi bi-arrow-right" aria-hidden="true"></i>
                        </button>
                    </div>
                </footer>
            </section>
        {:else if step === "plan"}
            <section class="step-panel" aria-labelledby="workflow-plan-heading">
                <header>
                    <div>
                        <span class="step-eyebrow">Step 4</span>
                        <h3 bind:this={stepHeading} tabindex="-1" id="workflow-plan-heading">{plan?.scope === "likely_usable" ? "Review the likely-only managed-link plan" : "Review the approved-video managed-link plan"}</h3>
                        <p>{plan?.scope === "likely_usable" ? "This dry run is scoped to eligible clear/minimal, non-held videos across the whole investigation." : "This dry run is scoped to your individually approved videos."} Source files remain where they are.</p>
                    </div>
                </header>

                {#if organizationRunning}
                    <div class="organization-running" aria-live="polite">
                        <i class="bi bi-arrow-repeat spin" aria-hidden="true"></i>
                        <div><strong>Creating validated links…</strong><span>{Math.round(jobProgress())}% complete</span></div>
                    </div>
                {:else if plan}
                    <div class="plan-summary">
                        <div><span>Target channel</span><strong>{plan.target_folder_name || investigation?.target_folder_name}</strong></div>
                        <div><span>Scope</span><strong>{planScopeLabel(plan.scope)}</strong></div>
                        <div><span>Selected</span><strong>{plan.selected_count}</strong></div>
                        <div><span>Ready</span><strong>{plan.ready_count}</strong></div>
                        <div><span>Blocked</span><strong>{plan.blocked_count}</strong></div>
                    </div>

                    <div class="plan-scope-note" role="note">
                        <i class={`bi ${plan.scope === "likely_usable" ? "bi-filter-circle" : "bi-check2-square"}`} aria-hidden="true"></i>
                        <div>
                            <strong>{planScopeLabel(plan.scope)}</strong>
                            {#if plan.scope === "likely_usable"}
                                <span>{plan.summary.excluded_needs_review_count} Needs-review {plan.summary.excluded_needs_review_count === 1 ? "video is" : "videos are"} excluded, including manual approvals · {plan.summary.held_count} held unchanged · {plan.summary.remaining_count} remain for later after this batch. Pending items become approved only when their links publish successfully.</span>
                            {:else}
                                <span>Only individually approved videos are selected · {plan.summary.held_count} held unchanged · {plan.summary.remaining_count} remain for later after this batch.</span>
                            {/if}
                        </div>
                    </div>

                    <div class="source-preserved" role="note">
                        <i class="bi bi-shield-check" aria-hidden="true"></i>
                        <div><strong>Source archive preserved</strong><span>Organization creates links only. It does not move, replace, transcode, or delete source media.</span></div>
                    </div>

                    <div class="plan-list">
                        {#each plan.items as entry (entry.item_id)}
                            <article class:blocked={["blocked", "conflict", "invalid", "error"].includes(entry.status)}>
                                <div class="plan-entry-heading">
                                    <strong>{entry.video_id}</strong>
                                    <div class="plan-entry-badges">
                                        <span class="condition-badge" class:condition-badge--likely={["clear", "minimal"].includes(entry.health_tier)} class:condition-badge--review={!(["clear", "minimal"].includes(entry.health_tier))}>{planHealthTierLabel(entry)}</span>
                                        <span>{planDecisionLabel(entry)}</span>
                                        <span>{entry.status}</span>
                                    </div>
                                </div>
                                <div><span>Source</span><code>{entry.source_path}</code></div>
                                <div><span>Managed link</span><code>{entry.destination_path}</code></div>
                                {#if entry.link_target}
                                    <div><span>True link target</span><code>{entry.link_target}</code></div>
                                {/if}
                                {#if entry.error}<p>{entry.error}</p>{/if}
                            </article>
                        {/each}
                    </div>
                {:else}
                    <div class="workflow-empty"><i class="bi bi-list-check" aria-hidden="true"></i><strong>No current plan</strong><span>Return to review and build a fresh dry run.</span></div>
                {/if}

                <footer class="step-actions split-actions">
                    <button type="button" on:click={() => moveTo("review")} disabled={organizationRunning}><i class="bi bi-arrow-left" aria-hidden="true"></i> Back to review</button>
                    <button class="primary-action" type="button" on:click={organizeApproved} disabled={!plan || plan.ready_count === 0 || actionBusy || organizationRunning || dirtyExpectedDurationItems.length > 0}>
                        <i class="bi bi-link-45deg" aria-hidden="true"></i>
                        {organizationRunning ? "Publishing…" : plan?.scope === "likely_usable" ? `Publish ${plan.ready_count} likely usable videos` : `Organize ${plan?.ready_count || 0} approved videos`}
                    </button>
                </footer>
            </section>
        {:else}
            <section class="step-panel completion-panel" aria-labelledby="workflow-complete-heading">
                <header>
                    <div>
                        <span class="step-eyebrow">Published / link health</span>
                        <h3 bind:this={stepHeading} tabindex="-1" id="workflow-complete-heading">Recovered channel publication health</h3>
                        <p>Published links are checked against their exact immutable source. Only an absent, server-authorized link can be repaired here.</p>
                    </div>
                </header>

                <div class="completion-mark" class:attention={linkHealthAttention}>
                    <i class={`bi ${linkHealthAttention ? "bi-exclamation-triangle" : "bi-check2-circle"}`} aria-hidden="true"></i>
                </div>
                <dl class="completion-counts">
                    <div><dt>Published</dt><dd>{publishedCount}</dd></div>
                    <div><dt>Healthy loaded</dt><dd>{healthyPublishedCount}</dd></div>
                    <div><dt>Other states loaded</dt><dd>{unhealthyPublishedCount}</dd></div>
                    <div><dt>Repair available</dt><dd>{repairNeededCount}</dd></div>
                </dl>
                <div class="completion-target"><span>Target channel</span><code>{investigation?.target_folder_path || investigation?.target_folder_name}</code></div>
                <div class="completion-batch-note" class:complete={remainingCount === 0 && !organizationOutcomeWarning} role="status">
                    <i class={`bi ${organizationOutcomeWarning ? "bi-exclamation-triangle" : remainingCount === 0 ? "bi-check2-circle" : "bi-collection-play"}`} aria-hidden="true"></i>
                    <span>
                        {#if organizationOutcomeWarning}
                            <strong>Publication batch finished with exceptions.</strong> {remainingCount} {remainingCount === 1 ? "video remains" : "videos remain"} unpublished; review the warning and remaining work.
                        {:else if remainingCount > 0}
                            <strong>Publication batch complete; investigation still open.</strong> {remainingCount} {remainingCount === 1 ? "video remains" : "videos remain"} unpublished for later review.
                        {:else}
                            <strong>Recovered channel publication complete.</strong> No unpublished videos remain in this investigation.
                        {/if}
                    </span>
                </div>
                {#if organizeResult?.message}<p class="completion-message">{organizeResult.message}</p>{/if}
                {#if organizationOutcomeWarning}
                    <p class="completion-warning" role="alert">
                        <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
                        <span>{organizationOutcomeWarning}</span>
                    </p>
                {/if}

                <section class="published-health" aria-labelledby="published-health-heading">
                    <header>
                        <div>
                            <h4 id="published-health-heading">Published links</h4>
                            <p>{publishedItems.length} loaded · automatic repair is restricted to exact <code>missing</code> + <code>repairable</code> server states.</p>
                        </div>
                    </header>
                    <div class="published-link-list">
                        {#each publishedItems as item (item.id)}
                            <article
                                class:published-link--healthy={item.link_status === "healthy"}
                                class:published-link--repairable={canRepairPublishedLink(item)}
                                class:published-link--blocked={!["healthy", "missing"].includes(item.link_status) || (item.link_status === "missing" && !item.repairable_link)}
                            >
                                <div class="published-link-heading">
                                    <div>
                                        <strong>{item.filename || item.video_id}</strong>
                                        <code>{item.video_id}</code>
                                    </div>
                                    <span data-status={item.link_status}>{linkStatusLabel(item)}</span>
                                </div>
                                <p>{linkStatusExplanation(item)}</p>
                                <div class="published-link-path"><span>Source</span><code>{item.path}</code></div>
                                <div class="published-link-path"><span>Managed path</span><code>{publishedPath(item)}</code></div>
                                {#if item.link_target}
                                    <div class="published-link-path"><span>Expected target</span><code>{item.link_target}</code></div>
                                {/if}
                                {#if item.actual_link_target || item.symlink_target}
                                    <div class="published-link-path"><span>Actual symlink target</span><code>{item.actual_link_target || item.symlink_target}</code></div>
                                {/if}
                                {#if item.source_integrity_error}
                                    <div class="published-link-path published-link-path--danger"><span>Source integrity</span><code>{item.source_integrity_error}</code></div>
                                {/if}
                                {#if item.database_owner_error}
                                    <div class="published-link-path published-link-path--danger"><span>Database owner</span><code>{item.database_owner_error}</code></div>
                                {/if}
                                <div class="published-link-actions">
                                    <button type="button" on:click={() => openPreview(item, "video")} disabled={!item.path}><i class="bi bi-play-circle" aria-hidden="true"></i> Preview source</button>
                                    {#if numeric(item.reference_repair_count) > 0}
                                        <button type="button" on:click={() => openReferenceRepair(item)} disabled={referenceRepairBusy}><i class="bi bi-clock-history" aria-hidden="true"></i> Repair history / restore</button>
                                    {/if}
                                    {#if canRepairPublishedLink(item)}
                                        <button class="primary-action" type="button" on:click={() => repairPublishedLink(item)} disabled={repairingItemIds.has(item.id)}>
                                            <i class="bi bi-link-45deg" aria-hidden="true"></i>
                                            {repairingItemIds.has(item.id) ? "Repairing…" : "Repair missing link"}
                                        </button>
                                    {:else if item.link_status !== "healthy"}
                                        <span class="automatic-repair-blocked"><i class="bi bi-shield-lock" aria-hidden="true"></i> Automatic repair blocked</span>
                                    {/if}
                                </div>
                            </article>
                        {:else}
                            <div class="workflow-empty"><i class="bi bi-link-45deg" aria-hidden="true"></i><strong>No published links loaded</strong><span>This investigation has not published any managed links yet.</span></div>
                        {/each}
                    </div>
                    {#if hasMoreItems}
                        <button class="load-more" type="button" on:click={() => loadItems()} disabled={loadingItems}>
                            {loadingItems ? "Loading…" : `Load more history (${items.length} of ${itemsTotal} loaded)`}
                        </button>
                    {/if}
                </section>

                <footer class="step-actions split-actions">
                    {#if remainingCount > 0}
                        <button type="button" on:click={() => moveTo("review")}><i class="bi bi-collection-play" aria-hidden="true"></i> Review remaining videos ({remainingCount})</button>
                    {:else}<span></span>{/if}
                    <button class="primary-action" type="button" on:click={leaveWorkflow}>Return to Unindexed</button>
                </footer>
            </section>
        {/if}
    </div>
</section>

<style>
    .channel-workflow {
        display: flex;
        height: 100%;
        min-height: 0;
        flex-direction: column;
        overflow: hidden;
        color: var(--text);
        background: var(--canvas, var(--page));
    }

    .channel-workflow :global(*) { box-sizing: border-box; }

    .channel-workflow button,
    .channel-workflow input { font: inherit; }

    .channel-workflow button {
        display: inline-flex;
        min-height: 30px;
        align-items: center;
        justify-content: center;
        gap: 6px;
        padding: 5px 9px;
        color: var(--text);
        background: var(--row, var(--surface-raised));
        border: 1px solid var(--rule, var(--line));
        border-radius: 0;
        cursor: pointer;
        font-size: 11px;
        font-weight: 700;
    }

    .channel-workflow button:hover:not(:disabled),
    .channel-workflow button.active {
        background: var(--row-hover, var(--surface-hover));
        border-color: var(--text-secondary, var(--text-muted));
    }

    .channel-workflow button:disabled { cursor: not-allowed; opacity: .5; }

    .channel-workflow .primary-action {
        color: var(--page-deep);
        background: var(--accent);
        border-color: var(--accent-strong);
    }

    .channel-workflow .primary-action:hover:not(:disabled) { background: var(--accent-strong); }

    .workflow-header {
        display: grid;
        min-height: 46px;
        grid-template-columns: auto minmax(0, 1fr) auto;
        align-items: center;
        gap: 10px;
        padding: 6px 8px;
        background: var(--frame, var(--page-deep));
        border-bottom: 1px solid var(--rule, var(--line));
    }

    .back-button { white-space: nowrap; }

    .workflow-heading { min-width: 0; }
    .workflow-heading span,
    .workflow-heading strong { display: block; }
    .workflow-heading span { color: var(--muted, var(--text-muted)); font-size: 9px; text-transform: uppercase; }
    .workflow-heading strong { overflow: hidden; margin-top: 1px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .workflow-count { color: var(--muted, var(--text-muted)); font-size: 10px; white-space: nowrap; }

    .workflow-steps { padding: 7px 8px; background: var(--surface, #434b3c); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .workflow-steps ol { display: grid; max-width: 620px; margin: 0 auto; padding: 0; grid-template-columns: repeat(4, minmax(0, 1fr)); list-style: none; }
    .workflow-steps li { position: relative; display: flex; min-width: 0; align-items: center; gap: 6px; color: var(--muted, var(--text-muted)); }
    .workflow-steps li::after { position: absolute; top: 50%; right: 6px; left: 30px; height: 1px; background: var(--rule-dim, var(--line-soft)); content: ""; }
    .workflow-steps li:last-child::after { display: none; }
    .workflow-steps li > span { position: relative; z-index: 1; display: grid; width: 24px; height: 24px; flex: 0 0 24px; place-items: center; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); font-size: 10px; }
    .workflow-steps li strong { position: relative; z-index: 1; padding-right: 8px; background: var(--surface, #434b3c); font-size: 10px; }
    .workflow-steps li.active,
    .workflow-steps li.complete { color: var(--text); }
    .workflow-steps li.active > span { color: var(--page-deep); background: var(--accent); border-color: var(--accent-strong); }
    .workflow-steps li.complete > span { color: var(--page-deep); background: var(--success); }

    .workflow-notice { display: flex; align-items: flex-start; gap: 8px; padding: 7px 9px; color: var(--text-secondary, var(--text-soft)); background: var(--surface, #434b3c); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); font-size: 11px; }
    .workflow-notice > span { min-width: 0; flex: 1; overflow-wrap: anywhere; }
    .workflow-notice > button { width: 28px; min-height: 28px; flex: 0 0 28px; padding: 0; }
    .workflow-notice--error { color: var(--danger-text, #f0d7cf); background: var(--danger-surface, #4a2e28); border-color: var(--danger-rule, #8b5a4e); }

    .workflow-body { min-height: 0; flex: 1; overflow-y: auto; padding: 8px; }
    .step-panel { display: flex; min-height: 100%; flex-direction: column; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .step-panel > header { display: flex; min-height: 55px; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 10px; background: var(--surface, #434b3c); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .step-eyebrow { display: block; color: var(--muted, var(--text-muted)); font-size: 9px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
    .step-panel h3 { margin: 2px 0 0; font-size: 14px; }
    .step-panel > header p { margin: 2px 0 0; color: var(--muted, var(--text-muted)); font-size: 10px; }

    .workflow-empty { display: grid; min-height: 230px; place-items: center; align-content: center; gap: 5px; color: var(--muted, var(--text-muted)); text-align: center; }
    .workflow-empty > i { font-size: 24px; }
    .workflow-empty strong { color: var(--text); }
    .workflow-empty span { max-width: 440px; font-size: 11px; }

    .channel-form { display: grid; width: min(680px, calc(100% - 20px)); margin: 10px auto; gap: 6px; padding: 12px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .channel-form label { color: var(--text-secondary, var(--text-soft)); font-size: 11px; font-weight: 700; }
    .channel-form input { width: 100%; min-height: 36px; padding: 7px 9px; color: var(--text); background: var(--frame, var(--page-deep)); border: 1px solid var(--rule, var(--line)); border-radius: 0; }
    .field-help { margin: 0 0 5px; color: var(--muted, var(--text-muted)); font-size: 10px; }
    .source-identity,
    .target-preview { display: grid; gap: 3px; padding: 8px; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .source-identity { margin-bottom: 5px; }
    .source-identity > span,
    .target-preview > span { color: var(--muted, var(--text-muted)); font-size: 9px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
    .source-identity code,
    .target-preview code { color: var(--text-secondary, var(--text-soft)); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
    .source-identity small,
    .target-preview small { color: var(--muted, var(--text-muted)); font-size: 10px; }

    .legacy-attach { display: grid; width: min(680px, calc(100% - 20px)); margin: 0 auto 10px; gap: 8px; padding: 12px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .legacy-attach h4 { margin: 2px 0 3px; font-size: 13px; }
    .legacy-attach p { margin: 0; color: var(--muted, var(--text-muted)); font-size: 10px; line-height: 1.45; }
    .legacy-attach label { color: var(--text-secondary, var(--text-soft)); font-size: 11px; font-weight: 700; }
    .legacy-attach label small { color: var(--muted, var(--text-muted)); font-weight: 400; }
    .legacy-attach-controls { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px; }
    .legacy-attach-controls input { min-width: 0; padding: 7px 9px; color: var(--text); background: var(--frame, var(--page-deep)); border: 1px solid var(--rule, var(--line)); border-radius: 0; }
    .attach-candidates,
    .attach-summary { display: grid; gap: 8px; padding: 9px; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .attach-candidates > button { justify-content: space-between; text-align: left; }
    .attach-candidates > button span,
    .attach-candidates > button small { min-width: 0; overflow-wrap: anywhere; }
    .attach-candidates > button small { color: var(--muted, var(--text-muted)); }
    .attach-summary dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; border: 1px solid var(--rule-dim, var(--line-soft)); }
    .attach-summary dl div { min-width: 0; padding: 7px; border-right: 1px solid var(--rule-dim, var(--line-soft)); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .attach-summary dl div:nth-child(3n) { border-right: 0; }
    .attach-summary dt { color: var(--muted, var(--text-muted)); font-size: 9px; }
    .attach-summary dd { margin: 2px 0 0; font-size: 15px; font-weight: 700; }
    .attach-summary .attention dd { color: var(--warning, #d5bd74); }
    .attach-summary details { max-height: 180px; overflow: auto; }
    .attach-summary summary { cursor: pointer; font-size: 11px; font-weight: 700; }
    .attach-summary ul { display: grid; gap: 3px; padding: 0; margin: 6px 0 0; list-style: none; }
    .attach-summary li { display: flex; justify-content: space-between; gap: 8px; padding: 4px 6px; background: var(--row, var(--surface-raised)); font-size: 10px; }
    .attach-summary li span { min-width: 0; overflow-wrap: anywhere; }

    .step-actions { display: flex; min-height: 48px; align-items: center; justify-content: flex-end; gap: 7px; margin-top: auto; padding: 8px 10px; background: var(--surface, #434b3c); border-top: 1px solid var(--rule-dim, var(--line-soft)); }
    .split-actions { justify-content: space-between; }

    .inspection-summary { display: grid; width: min(680px, calc(100% - 20px)); margin: 10px auto; gap: 9px; padding: 12px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .inspection-progress { display: flex; align-items: flex-end; justify-content: space-between; gap: 10px; }
    .inspection-progress div strong,
    .inspection-progress div span { display: block; }
    .inspection-progress div strong { font-size: 14px; }
    .inspection-progress div span { color: var(--muted, var(--text-muted)); font-size: 10px; }
    .inspection-progress > span { color: var(--text-secondary, var(--text-soft)); font-size: 12px; font-weight: 700; }
    .inspection-summary progress { width: 100%; height: 12px; accent-color: var(--accent); }
    .active-work,
    .inspection-explanation,
    .source-preserved,
    .organization-running { display: flex; align-items: flex-start; gap: 8px; padding: 8px; color: var(--text-secondary, var(--text-soft)); background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .active-work i,
    .organization-running i { margin-top: 2px; color: var(--accent); }
    .active-work strong,
    .active-work span,
    .source-preserved strong,
    .source-preserved span,
    .organization-running strong,
    .organization-running span { display: block; }
    .active-work span,
    .source-preserved span,
    .organization-running span { color: var(--muted, var(--text-muted)); font-size: 10px; }
    .inspection-explanation p { margin: 0; font-size: 10px; }
    .inspection-counts,
    .completion-counts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; border: 1px solid var(--rule-dim, var(--line-soft)); }
    .inspection-counts div,
    .completion-counts div { padding: 7px; border-right: 1px solid var(--rule-dim, var(--line-soft)); }
    .inspection-counts div:last-child,
    .completion-counts div:last-child { border-right: 0; }
    .inspection-counts dt,
    .completion-counts dt { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .inspection-counts dd,
    .completion-counts dd { margin: 2px 0 0; font-size: 13px; font-weight: 700; }

    .review-step { min-height: auto; }
    .decision-summary { display: grid; justify-items: end; }
    .decision-summary strong { font-size: 12px; }
    .decision-summary span { color: var(--muted, var(--text-muted)); font-size: 10px; }
    .integrity-limit { display: flex; align-items: flex-start; gap: 8px; margin: 8px; padding: 8px; color: var(--text-secondary, var(--text-soft)); background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); font-size: 10px; }
    .integrity-limit i { color: var(--warning); }
    .mobile-bucket-tabs { display: none; }
    .review-columns { display: grid; min-height: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 0 8px 8px; }
    .condition-list { min-width: 0; background: var(--canvas, var(--page)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .condition-list > header { display: flex; min-height: 55px; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 8px; background: var(--surface, #434b3c); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .condition-list h4 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 12px; }
    .condition-list h4 i { color: var(--accent); }
    .condition-list--review h4 i { color: var(--warning); }
    .condition-list header p { margin: 2px 0 0; color: var(--muted, var(--text-muted)); font-size: 9px; }
    .condition-list > header > span { display: grid; min-width: 28px; height: 24px; place-items: center; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); font-size: 10px; font-weight: 700; }
    .likely-plan-action { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 7px 7px 0; padding: 8px; background: var(--surface, #434b3c); border: 1px solid var(--success); }
    .likely-plan-action > div { min-width: 0; }
    .likely-plan-action strong,
    .likely-plan-action span { display: block; }
    .likely-plan-action strong { font-size: 10px; }
    .likely-plan-action span { margin-top: 2px; color: var(--muted, var(--text-muted)); font-size: 9px; }
    .likely-plan-action > button { flex: 0 0 auto; }
    .likely-plan-confirmation { display: grid; gap: 8px; margin: 7px 7px 0; padding: 10px; color: var(--text-secondary, var(--text-soft)); background: var(--frame, var(--page-deep)); border: 1px solid var(--accent); }
    .likely-plan-confirmation h5 { margin: 2px 0 0; color: var(--text); font-size: 12px; }
    .likely-plan-confirmation h5:focus { outline: 1px solid var(--accent); outline-offset: 3px; }
    .likely-plan-confirmation p { margin: 3px 0 0; font-size: 9px; }
    .likely-plan-confirmation ul { display: grid; gap: 5px; margin: 0; padding-left: 18px; font-size: 9px; }
    .likely-plan-confirmation-actions { display: flex; justify-content: flex-end; gap: 6px; padding-top: 7px; border-top: 1px solid var(--rule-dim, var(--line-soft)); }
    .media-list { display: grid; gap: 7px; max-height: 58dvh; overflow-y: auto; padding: 7px; }
    .media-list article { display: grid; gap: 6px; padding: 8px; background: var(--row, var(--surface-raised)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .media-list article.approved { border-color: var(--success); }
    .media-list article.held { opacity: .78; }
    .media-heading { min-width: 0; }
    .media-heading h5 { margin: 4px 0 1px; font-size: 12px; overflow-wrap: anywhere; }
    .media-heading code { color: var(--muted, var(--text-muted)); font-size: 10px; }
    .condition-badge { display: inline-flex; min-height: 19px; align-items: center; padding: 2px 5px; border: 1px solid; font-size: 8px; font-weight: 700; text-transform: uppercase; }
    .condition-badge--likely { color: var(--text); border-color: var(--success); }
    .reference-repair-panel { display: grid; gap: 10px; margin: 8px; padding: 12px; min-width: 0; background: var(--frame, var(--page-deep)); border: 1px solid var(--accent); }
    .reference-repair-panel > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 0 0 9px; border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .reference-repair-panel > header > div { min-width: 0; }
    .reference-repair-panel > header h5 { margin: 3px 0; font-size: 13px; }
    .reference-repair-panel > header h5,
    .reference-repair-panel > header p { overflow-wrap: anywhere; }
    .reference-repair-panel > header p { max-width: 680px; margin: 0; color: var(--muted, var(--text-muted)); font-size: 10px; line-height: 1.45; }
    .reference-selector { display: grid; gap: 7px; }
    .reference-selector label { font-size: 10px; font-weight: 700; }
    .reference-selector select { width: 100%; min-width: 0; }
    .repair-history-selector { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 5px 9px; padding: 8px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .repair-history-selector label { font-size: 10px; font-weight: 700; }
    .repair-history-selector select { width: 100%; min-width: 0; }
    .repair-history-selector span { grid-column: 2; min-width: 0; color: var(--muted, var(--text-muted)); font-size: 9px; overflow-wrap: anywhere; }
    .compatibility-warning, .reference-repair-error { display: flex; gap: 7px; margin: 0; padding: 8px; font-size: 10px; border: 1px solid var(--warning, #b89a56); }
    .reference-repair-error { color: var(--danger-text, #f0c2b7); border-color: var(--danger, #a75f52); }
    .reference-rationale { display: grid; gap: 3px; padding: 8px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .reference-rationale strong { font-size: 10px; }
    .reference-rationale span { color: var(--muted, var(--text-muted)); font-size: 9px; }
    .reference-preview { display: grid; grid-template-columns: minmax(0, 2fr) minmax(180px, 1fr); align-items: center; gap: 8px; min-width: 0; }
    .reference-preview video { width: 100%; max-height: 220px; background: #111; }
    .reference-preview audio { width: 100%; }
    .compatibility-acknowledgement { display: flex; align-items: flex-start; gap: 8px; padding: 8px; font-size: 10px; border: 1px solid var(--warning, #b89a56); }
    .compatibility-acknowledgement input { flex: 0 0 auto; }
    .reference-repair-actions { display: flex; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }
    .repair-job-state { display: grid; grid-template-columns: minmax(110px, auto) minmax(100px, 1fr) auto; align-items: center; gap: 8px; padding: 8px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .repair-job-state > div { display: grid; }
    .repair-job-state strong { text-transform: capitalize; }
    .repair-job-state span { color: var(--muted, var(--text-muted)); font-size: 9px; }
    .repair-job-state progress { width: 100%; }
    .repair-assessment { display: grid; gap: 3px; padding: 10px; border: 1px solid var(--accent); }
    .repair-assessment > span { color: var(--muted, var(--text-muted)); font-size: 9px; text-transform: uppercase; }
    .repair-assessment > strong { font-size: 13px; }
    .repair-assessment p { margin: 2px 0 0; color: var(--muted, var(--text-muted)); font-size: 10px; }
    .repair-assessment[data-assessment="better"] { border-color: var(--success); }
    .repair-assessment[data-assessment="worse"] { border-color: var(--danger, #a75f52); }
    .repair-comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; min-width: 0; }
    .repair-comparison article { display: grid; align-content: start; gap: 7px; min-width: 0; padding: 9px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .repair-comparison h6 { margin: 0; font-size: 11px; }
    .repair-comparison video { display: block; width: 100%; max-height: 260px; background: #111; }
    .repair-comparison audio { width: 100%; height: 34px; }
    .repair-comparison dl, .repair-action-plan dl { display: grid; gap: 5px; margin: 0; }
    .repair-comparison dl > div, .repair-action-plan dl > div { display: grid; grid-template-columns: 78px minmax(0, 1fr); gap: 7px; }
    .repair-comparison dt, .repair-action-plan dt { color: var(--muted, var(--text-muted)); font-size: 9px; }
    .repair-comparison dd, .repair-action-plan dd { min-width: 0; margin: 0; overflow-wrap: anywhere; font-size: 9px; }
    .repair-action-plan { display: grid; gap: 9px; padding: 10px; border: 1px solid var(--danger, #a75f52); }
    .repair-action-plan h6, .repair-action-plan p { margin: 0; }
    .repair-action-plan p { color: var(--muted, var(--text-muted)); font-size: 10px; }
    .repair-confirmation { display: grid; gap: 5px; font-size: 10px; }
    .repair-confirmation input { width: 100%; min-width: 0; }
    .danger-action { color: var(--danger-text, #f0c2b7); border-color: var(--danger, #a75f52); }
    .condition-badge--review { color: var(--danger-text, #f0d7cf); background: var(--danger-surface, #4a2e28); border-color: var(--danger-rule, #8b5a4e); }
    .media-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; border: 1px solid var(--rule-dim, var(--line-soft)); }
    .media-facts div { min-width: 0; padding: 5px; border-right: 1px solid var(--rule-dim, var(--line-soft)); }
    .media-facts div:last-child { border-right: 0; }
    .media-facts dt { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .media-facts dd { margin: 1px 0 0; font-size: 10px; overflow-wrap: anywhere; }
    .stream-summary { margin: 0; color: var(--muted, var(--text-muted)); font-size: 9px; overflow-wrap: anywhere; }
    .source-path { color: var(--text-secondary, var(--text-soft)); font-size: 9px; overflow-wrap: anywhere; }
    .media-list details { color: var(--text-secondary, var(--text-soft)); font-size: 9px; }
    .media-list summary { min-height: 24px; cursor: pointer; }
    .media-list ul { display: grid; gap: 3px; margin: 4px 0 0; padding-left: 17px; }
    .duration-editor { display: grid; gap: 5px; padding: 7px; background: var(--frame, var(--page-deep)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .duration-editor-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
    .duration-editor-heading label { color: var(--text-secondary, var(--text-soft)); font-size: 10px; font-weight: 700; }
    .duration-editor-heading label span { color: var(--muted, var(--text-muted)); font-size: 8px; font-weight: 400; text-transform: uppercase; }
    .duration-editor-heading > span { color: var(--muted, var(--text-muted)); font-size: 9px; white-space: nowrap; }
    .duration-editor-controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; }
    .duration-editor-controls input { grid-column: 1 / -1; min-width: 0; width: 100%; min-height: 32px; padding: 5px 7px; color: var(--text); background: var(--canvas, var(--page)); border: 1px solid var(--rule, var(--line)); border-radius: 0; font: inherit; font-size: 10px; }
    .duration-editor-controls input[aria-invalid="true"] { border-color: var(--danger-rule, #8b5a4e); }
    .duration-editor-controls button { min-width: 52px; padding-inline: 7px; font-size: 9px; }
    .duration-editor p { margin: 0; color: var(--muted, var(--text-muted)); font-size: 8px; line-height: 1.35; }
    .duration-editor .duration-editor-error { color: var(--danger-text, #f0d7cf); font-size: 9px; }
    .dirty-duration-warning { display: flex; align-items: flex-start; gap: 7px; margin: 0 8px 8px; padding: 8px; color: var(--warning-text, var(--text)); background: var(--warning-surface, var(--surface)); border: 1px solid var(--warning); font-size: 10px; }
    .dirty-duration-warning i { flex: 0 0 auto; color: var(--warning); }
    .dirty-duration-warning span { min-width: 0; overflow-wrap: anywhere; }
    .media-actions,
    .decision-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; }
    .media-actions button,
    .decision-actions button { width: 100%; min-width: 0; padding-inline: 5px; font-size: 9px; }
    .decision-actions { padding-top: 6px; border-top: 1px solid var(--rule-dim, var(--line-soft)); }
    .decision-actions button.active { color: var(--page-deep); background: var(--success); border-color: var(--success); }
    .list-empty { display: grid; min-height: 130px; place-items: center; align-content: center; gap: 4px; color: var(--muted, var(--text-muted)); text-align: center; font-size: 10px; }
    .load-more { width: calc(100% - 16px); margin: 0 8px 8px; }
    .review-footer { position: sticky; bottom: -8px; z-index: 2; justify-content: space-between; }
    .review-footer > div { display: flex; align-items: center; gap: 8px; }
    .review-footer > div > span { color: var(--muted, var(--text-muted)); font-size: 10px; }

    .organization-running { width: min(680px, calc(100% - 20px)); margin: 10px auto; }
    .plan-summary { display: grid; grid-template-columns: minmax(0, 2fr) repeat(4, minmax(82px, 1fr)); gap: 1px; margin: 8px; background: var(--rule-dim, var(--line-soft)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .plan-summary > div { padding: 8px; background: var(--surface, #434b3c); }
    .plan-summary span,
    .plan-summary strong { display: block; }
    .plan-summary span { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .plan-summary strong { margin-top: 2px; font-size: 11px; overflow-wrap: anywhere; }
    .source-preserved { margin: 0 8px 8px; }
    .source-preserved i { color: var(--success); }
    .plan-scope-note { display: flex; align-items: flex-start; gap: 8px; margin: 0 8px 8px; padding: 8px; color: var(--text-secondary, var(--text-soft)); background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .plan-scope-note i { margin-top: 1px; color: var(--accent); }
    .plan-scope-note strong,
    .plan-scope-note span { display: block; }
    .plan-scope-note strong { font-size: 10px; }
    .plan-scope-note span { margin-top: 2px; color: var(--muted, var(--text-muted)); font-size: 9px; }
    .plan-list { display: grid; gap: 6px; max-height: 56dvh; overflow-y: auto; padding: 0 8px 8px; }
    .plan-list article { display: grid; gap: 5px; padding: 7px; background: var(--row, var(--surface-raised)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .plan-list article.blocked { color: var(--danger-text, #f0d7cf); background: var(--danger-surface, #4a2e28); border-color: var(--danger-rule, #8b5a4e); }
    .plan-entry-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
    .plan-entry-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }
    .plan-entry-badges > span { min-height: 19px; padding: 2px 5px; border: 1px solid var(--rule-dim, var(--line-soft)); font-size: 8px; font-weight: 700; text-transform: uppercase; }
    .plan-entry-badges > .condition-badge--likely { border-color: var(--success); }
    .plan-entry-badges > .condition-badge--review { color: var(--danger-text, #f0d7cf); background: var(--danger-surface, #4a2e28); border-color: var(--danger-rule, #8b5a4e); }
    .plan-entry-badges > span:last-child { color: var(--text-secondary, var(--text-soft)); }
    .plan-list article > div:not(.plan-entry-heading) { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 6px; }
    .plan-list article > div > span { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .plan-list code { color: var(--text-secondary, var(--text-soft)); font-size: 9px; overflow-wrap: anywhere; }
    .plan-list p { margin: 0; font-size: 10px; }

    .completion-panel { align-items: stretch; }
    .completion-mark { display: grid; margin: 22px auto 10px; width: 64px; height: 64px; place-items: center; color: var(--success); border: 1px solid var(--success); font-size: 30px; }
    .completion-mark.attention { color: var(--warning); border-color: var(--warning); }
    .completion-counts { width: min(760px, calc(100% - 20px)); margin: 0 auto; grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .completion-target { display: grid; width: min(620px, calc(100% - 20px)); margin: 8px auto; gap: 3px; padding: 8px; background: var(--surface, #434b3c); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .completion-target span { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .completion-target code { overflow-wrap: anywhere; }
    .completion-batch-note { display: flex; width: min(620px, calc(100% - 20px)); align-items: flex-start; gap: 8px; margin: 0 auto 8px; padding: 8px; color: var(--text-secondary, var(--text-soft)); background: var(--surface, #434b3c); border: 1px solid var(--warning); }
    .completion-batch-note.complete { border-color: var(--success); }
    .completion-batch-note i { flex: 0 0 auto; color: var(--warning); }
    .completion-batch-note.complete i { color: var(--success); }
    .completion-batch-note span { min-width: 0; font-size: 10px; overflow-wrap: anywhere; }
    .completion-message { width: min(620px, calc(100% - 20px)); margin: 0 auto 10px; color: var(--muted, var(--text-muted)); text-align: center; }
    .completion-warning { display: flex; width: min(620px, calc(100% - 20px)); align-items: flex-start; gap: 7px; margin: 0 auto 10px; padding: 8px; color: var(--danger-text, #f0d7cf); background: var(--danger-surface, #4a2e28); border: 1px solid var(--danger-rule, #8b5a4e); }
    .completion-warning i { flex: 0 0 auto; }
    .completion-warning span { min-width: 0; overflow-wrap: anywhere; }
    .published-health { width: min(920px, calc(100% - 20px)); margin: 2px auto 10px; background: var(--canvas, var(--page)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .published-health > header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px; background: var(--surface, #434b3c); border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
    .published-health h4 { margin: 0; font-size: 12px; }
    .published-health header p { margin: 2px 0 0; color: var(--muted, var(--text-muted)); font-size: 9px; }
    .published-link-list { display: grid; gap: 7px; max-height: 55dvh; overflow-y: auto; padding: 7px; }
    .published-link-list article { display: grid; gap: 6px; padding: 8px; background: var(--row, var(--surface-raised)); border: 1px solid var(--rule-dim, var(--line-soft)); }
    .published-link-list article.published-link--healthy { border-color: var(--success); }
    .published-link-list article.published-link--repairable { border-color: var(--warning); }
    .published-link-list article.published-link--blocked { border-color: var(--danger-rule, #8b5a4e); }
    .published-link-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
    .published-link-heading > div { min-width: 0; }
    .published-link-heading strong,
    .published-link-heading code { display: block; overflow-wrap: anywhere; }
    .published-link-heading strong { font-size: 11px; }
    .published-link-heading code { margin-top: 2px; color: var(--muted, var(--text-muted)); font-size: 9px; }
    .published-link-heading > span { flex: 0 0 auto; padding: 3px 5px; border: 1px solid currentColor; font-size: 8px; font-weight: 700; text-transform: uppercase; }
    .published-link-heading > span[data-status="healthy"] { color: var(--success); }
    .published-link-heading > span[data-status="missing"] { color: var(--warning); }
    .published-link--blocked .published-link-heading > span { color: var(--danger-text, #f0d7cf); }
    .published-link-list article > p { margin: 0; color: var(--text-secondary, var(--text-soft)); font-size: 9px; }
    .published-link-path { display: grid; grid-template-columns: 105px minmax(0, 1fr); gap: 6px; padding-top: 4px; border-top: 1px solid var(--rule-dim, var(--line-soft)); }
    .published-link-path span { color: var(--muted, var(--text-muted)); font-size: 8px; text-transform: uppercase; }
    .published-link-path code { color: var(--text-secondary, var(--text-soft)); font-size: 9px; overflow-wrap: anywhere; }
    .published-link-path--danger span,
    .published-link-path--danger code { color: var(--danger-text, #f0d7cf); }
    .published-link-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; padding-top: 4px; }
    .automatic-repair-blocked { display: inline-flex; min-height: 30px; align-items: center; gap: 5px; color: var(--danger-text, #f0d7cf); font-size: 9px; }

    .spin { animation: workflow-spin .8s linear infinite; }
    @keyframes workflow-spin { to { transform: rotate(360deg); } }

    @media (max-width: 760px) {
        .channel-workflow {
            height: 100%;
            min-height: 0;
            overflow: hidden;
        }
        .channel-workflow button { min-height: 44px; font-size: 12px; }
        .channel-workflow input { min-height: 44px; font-size: 16px; }
        .workflow-header { grid-template-columns: minmax(0, 1fr) auto; min-height: 56px; padding-right: max(8px, env(safe-area-inset-right)); padding-left: max(8px, env(safe-area-inset-left)); }
        .back-button { grid-column: 1; justify-self: start; }
        .workflow-heading { grid-column: 1 / -1; grid-row: 2; }
        .workflow-heading strong { overflow: visible; text-overflow: clip; overflow-wrap: anywhere; white-space: normal; }
        .workflow-count { grid-column: 2; grid-row: 1; }
        .workflow-steps { padding-right: max(6px, env(safe-area-inset-right)); padding-left: max(6px, env(safe-area-inset-left)); }
        .workflow-steps li { justify-content: center; }
        .workflow-steps li strong { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
        .workflow-steps li::after { right: -12px; left: calc(50% + 16px); }
        .workflow-notice > button { width: 44px; min-height: 44px; flex-basis: 44px; }
        .workflow-body {
            min-height: 0;
            flex: 1 1 0;
            overflow-y: auto;
            overscroll-behavior-y: contain;
            -webkit-overflow-scrolling: touch;
            padding-right: max(5px, env(safe-area-inset-right));
            padding-bottom: max(5px, env(safe-area-inset-bottom));
            padding-left: max(5px, env(safe-area-inset-left));
        }
        .step-panel > header { align-items: flex-start; flex-direction: column; }
        .step-panel h3 { font-size: 15px; }
        .channel-form,
        .legacy-attach,
        .inspection-summary,
        .organization-running { width: calc(100% - 12px); margin: 6px auto; padding: 9px; }
        .step-actions,
        .split-actions { align-items: stretch; flex-direction: column-reverse; }
        .step-actions button { width: 100%; }
        .legacy-attach-controls { grid-template-columns: minmax(0, 1fr); }
        .legacy-attach-controls button { width: 100%; }
        .attach-summary dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .attach-summary dl div:nth-child(3n) { border-right: 1px solid var(--rule-dim, var(--line-soft)); }
        .attach-summary dl div:nth-child(2n) { border-right: 0; }
        .attach-summary li { align-items: flex-start; flex-direction: column; }
        .inspection-counts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .inspection-counts div:nth-child(2) { border-right: 0; }
        .inspection-counts div:nth-child(-n + 2) { border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
        .decision-summary { width: 100%; grid-template-columns: repeat(2, minmax(0, 1fr)); justify-items: stretch; }
        .decision-summary span { text-align: right; }
        .mobile-bucket-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0 6px 6px; }
        .mobile-bucket-tabs button { border-right: 0; }
        .mobile-bucket-tabs button:last-child { border-right: 1px solid var(--rule, var(--line)); }
        .mobile-bucket-tabs button.active { color: var(--page-deep); background: var(--accent); }
        .mobile-bucket-tabs button span { display: inline-grid; min-width: 21px; height: 21px; place-items: center; color: inherit; background: rgb(0 0 0 / 12%); }
        .review-columns { grid-template-columns: minmax(0, 1fr); padding: 0 6px 6px; }
        .condition-list.mobile-hidden { display: none; }
        .likely-plan-action { align-items: stretch; flex-direction: column; }
        .likely-plan-action > button { width: 100%; }
        .likely-plan-confirmation { margin-right: 7px; margin-left: 7px; padding: 11px; }
        .likely-plan-confirmation p,
        .likely-plan-confirmation ul { font-size: 10px; line-height: 1.45; }
        .likely-plan-confirmation-actions { align-items: stretch; flex-direction: column-reverse; }
        .likely-plan-confirmation-actions button { width: 100%; }
        .reference-repair-panel { margin: 7px 0; padding: 10px; }
        .repair-history-selector { grid-template-columns: minmax(0, 1fr); }
        .repair-history-selector span { grid-column: 1; }
        .repair-comparison { grid-template-columns: minmax(0, 1fr); }
        .reference-preview { grid-template-columns: minmax(0, 1fr); }
        .reference-repair-actions { align-items: stretch; flex-direction: column-reverse; }
        .reference-repair-actions button { width: 100%; }
        .repair-job-state { grid-template-columns: minmax(0, 1fr) auto; }
        .repair-job-state progress { grid-column: 1 / -1; }
        .media-list { max-height: none; overflow: visible; }
        .media-list article { padding: 9px; }
        .media-list summary { display: flex; min-height: 44px; align-items: center; }
        .duration-editor { padding: 9px; }
        .duration-editor-controls { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        .duration-editor-controls input { min-height: 44px; font-size: 16px; }
        .duration-editor-controls button { width: 100%; font-size: 11px; }
        .duration-editor p { font-size: 10px; }
        .media-actions button,
        .decision-actions button { font-size: 11px; }
        .review-footer {
            position: static;
            bottom: auto;
            padding-bottom: max(8px, env(safe-area-inset-bottom));
        }
        .review-footer > div { align-items: stretch; flex-direction: column; }
        .review-footer > div > span { text-align: center; }
        .plan-summary { grid-template-columns: minmax(0, 1fr); }
        .plan-list { max-height: none; overflow: visible; }
        .plan-entry-heading { align-items: stretch; flex-direction: column; }
        .plan-entry-badges { justify-content: flex-start; }
        .plan-list article > div:not(.plan-entry-heading) { grid-template-columns: minmax(0, 1fr); }
        .published-health { width: calc(100% - 12px); }
        .published-link-list { max-height: none; overflow: visible; }
        .published-link-heading,
        .published-link-actions { align-items: stretch; flex-direction: column; }
        .published-link-heading > span { align-self: flex-start; }
        .published-link-path { grid-template-columns: minmax(0, 1fr); }
        .published-link-actions button { width: 100%; }
        .completion-counts { grid-template-columns: minmax(0, 1fr); }
        .completion-counts div { border-right: 0; border-bottom: 1px solid var(--rule-dim, var(--line-soft)); }
        .completion-counts div:last-child { border-bottom: 0; }
    }

    @media (prefers-reduced-motion: reduce) {
        .spin { animation-duration: 1.8s; }
    }
</style>
