<script lang="ts">
    import { onDestroy, onMount } from "svelte";
    import {
        boundedEvidenceJson,
        normalizeAvailabilityEvidence,
        preservedObservationCount,
        structuredEvidenceSignals,
    } from "../lib/availabilityEvidence.js";
    import {
        normalizeTerminationEligibility,
        terminationEligibilityMessage,
    } from "../lib/availabilityEligibility.js";

    type EvidenceSignal = { key: string; value: string };

    type Evidence = {
        id: string;
        jobId: string;
        subjectId: string;
        state: string;
        reasonCode: string;
        provider: string;
        rawReason: string;
        detail: string;
        observedAt: string;
        explicit: boolean;
        toolVersion: string;
        structured: Record<string, unknown>;
    };

    type CountingObservation = {
        id: string;
        observedAt: string;
        reasonCode: string;
    };

    type TerminationEligibility = {
        diagnosticsAvailable: boolean;
        eligible: boolean;
        status: string;
        requiredObservations: number;
        thresholdProgress: number;
        matchingObservations: number;
        qualifyingObservationsNeeded: number;
        minimumSeparationHours: number;
        matchingReasonCode: string;
        firstObservedAt: string;
        latestObservedAt: string;
        nextQualifyingAt: string;
        qualifiedAt: string;
        countingObservations: CountingObservation[];
        countingObservationIds: string[];
        lastBlockingObservation: {
            id: string;
            state: string;
            observedAt: string;
        } | null;
        apiCorroborationRequired: boolean;
    };

    type ChannelRecord = {
        channelId: string;
        displayName: string;
        provenance: string;
        state: string;
        designation: string;
        folderPath: string;
        lastCheckedAt: string;
        lastAccessibleAt: string;
        reasonCode: string;
        terminationCandidate: boolean;
        terminationEligibility: TerminationEligibility;
        terminationConfirmed: boolean;
        observationCount: number;
        localVideoCount: number;
        revision: string;
        activeJobId: string;
        evidence: Evidence[];
    };

    type VideoRecord = {
        videoId: string;
        title: string;
        state: string;
        rawReason: string;
        detail: string;
        lastCheckedAt: string;
        localPath: string;
        previewUrl: string;
        audioPreviewUrl: string;
    };

    type AvailabilityJob = {
        jobId: string;
        channelId: string;
        status: string;
        phase: string;
        current: number;
        total: number;
        message: string;
        error: string;
        kind: "channel" | "videos" | "unknown";
    };

    type PlanInventoryItem = {
        kind: string;
        path: string;
        detail: string;
        status: string;
    };

    type DesignationPlan = {
        token: string;
        revision: string;
        sourcePath: string;
        destinationPath: string;
        location: string;
        canApply: boolean;
        blockers: string[];
        warnings: string[];
        counts: Record<string, number | string>;
        inventory: PlanInventoryItem[];
        expiresAt: string;
    };

    const channelStates = [
        ["all", "All states"],
        ["accessible", "Accessible"],
        ["termination_candidate", "Termination candidate"],
        ["explicit_termination", "Explicit termination evidence"],
        ["restricted", "Restricted"],
        ["unavailable_unknown", "Unavailable — unknown"],
        ["transient", "Transient / indeterminate"],
        ["conflicting", "Conflicting evidence"],
    ];

    const videoStates = [
        ["all", "All states"],
        ["unchecked", "Not checked"],
        ["available", "Available"],
        ["private", "Private"],
        ["community_guidelines", "Community guidelines"],
        ["policy_removed", "Other policy / copyright"],
        ["uploader_removed", "Uploader removed"],
        ["account_terminated", "Account terminated"],
        ["restricted", "Restricted"],
        ["live_processing", "Live / processing"],
        ["unavailable_unknown", "Unavailable — unknown"],
        ["transient", "Transient / indeterminate"],
    ];

    let channels: ChannelRecord[] = [];
    let selectedChannelId = "";
    let channelQuery = "";
    let channelStateFilter = "all";
    let loadingChannels = true;
    let channelError = "";
    let loadingEvidence = false;
    let evidenceError = "";

    let videos: VideoRecord[] = [];
    let loadingVideos = false;
    let videoError = "";
    let videoQuery = "";
    let videoStateFilter = "all";
    let videoPage = 1;
    let videoPages = 1;
    let videoTotal = 0;
    const videosPerPage = 100;
    let videoFilterTimer: ReturnType<typeof setTimeout> | null = null;
    let channelRequestController: AbortController | null = null;
    let videoRequestController: AbortController | null = null;
    let evidenceRequestController: AbortController | null = null;

    let apiCorroborationEnabled = false;

    let activeJob: AvailabilityJob | null = null;
    let jobError = "";
    let jobPollTimer: ReturnType<typeof setTimeout> | null = null;

    let designationPlan: DesignationPlan | null = null;
    let planningDesignation = false;
    let applyingDesignation = false;
    let designationError = "";
    let designationSuccess = "";
    let confirmation = "";
    let designationGeneration = 0;

    let destroyed = false;

    $: selectedChannel =
        channels.find((channel) => channel.channelId === selectedChannelId) || null;
    $: selectedEligibilityCopy = selectedChannel
        ? terminationEligibilityMessage(selectedChannel.terminationEligibility)
        : null;

    $: filteredChannels = channels.filter((channel) => {
        const query = normalize(channelQuery);
        const searchable = normalize(
            `${channel.displayName} ${channel.channelId} ${channel.provenance} ${channel.folderPath}`,
        );
        return (
            (!query || searchable.includes(query)) &&
            (channelStateFilter === "all" ||
                canonicalChannelState(channel) === channelStateFilter)
        );
    });

    $: filteredVideos = videos.filter((video) => {
        const query = normalize(videoQuery);
        const searchable = normalize(
            `${video.videoId} ${video.title} ${video.rawReason} ${video.localPath}`,
        );
        return (
            (!query || searchable.includes(query)) &&
            (videoStateFilter === "all" ||
                canonicalVideoState(video.state) === videoStateFilter)
        );
    });

    $: confirmationPhrase = selectedChannel
        ? `TERMINATE ${selectedChannel.channelId}`
        : "";

    $: mayApplyPlan = Boolean(
        designationPlan?.token &&
            designationPlan.canApply &&
            designationPlan.blockers.length === 0 &&
            confirmation.trim() === confirmationPhrase,
    );

    function normalize(value: unknown): string {
        return String(value ?? "").trim().toLowerCase();
    }

    function textValue(...values: unknown[]): string {
        for (const value of values) {
            if (value !== null && value !== undefined && String(value).trim()) {
                return String(value);
            }
        }
        return "";
    }

    function boolValue(...values: unknown[]): boolean {
        for (const value of values) {
            if (typeof value === "boolean") return value;
            if (value === 1 || value === "1" || value === "true") return true;
            if (value === 0 || value === "0" || value === "false") return false;
        }
        return false;
    }

    function numericValue(value: unknown): number {
        const number = Number(value);
        return Number.isFinite(number) ? number : 0;
    }

    function arrayFrom(data: any, ...keys: string[]): any[] {
        if (Array.isArray(data)) return data;
        for (const key of keys) {
            if (Array.isArray(data?.[key])) return data[key];
        }
        return [];
    }

    function normalizeEvidence(raw: any, index: number): Evidence {
        return normalizeAvailabilityEvidence(raw, index) as Evidence;
    }

    function normalizeChannel(raw: any): ChannelRecord {
        const aggregate = raw?.availability || raw?.current || {};
        const evidenceSource =
            raw?.observations || raw?.history || raw?.evidence || aggregate?.history || [];
        let evidence = arrayFrom(evidenceSource, "items", "observations").map(
            normalizeEvidence,
        );
        const currentState = textValue(
            aggregate?.state,
            raw?.current_state,
            raw?.availability_state,
            raw?.state,
            "unchecked",
        );
        const designation = textValue(
            raw?.designation,
            aggregate?.designation,
            raw?.storage_designation,
        );
        const candidate = boolValue(
            raw?.termination_candidate,
            aggregate?.termination_candidate,
            raw?.can_confirm_termination,
        );
        const terminationEligibility = normalizeTerminationEligibility(
            raw?.termination_eligibility ??
                aggregate?.termination_eligibility ??
                raw?.eligibility,
            candidate,
        ) as TerminationEligibility;
        const reasonCode = textValue(
            aggregate?.reason_code,
            raw?.current_reason_code,
            raw?.reason_code,
        );
        const currentStateAt = textValue(
            aggregate?.state_at,
            raw?.current_state_at,
            raw?.last_checked_at,
            raw?.updated_at,
        );
        const normalizedCurrentState = normalize(currentState);
        if (
            evidence.length === 0 &&
            Boolean(currentStateAt) &&
            (Boolean(reasonCode) ||
                !["unchecked", "unknown"].includes(normalizedCurrentState))
        ) {
            evidence = [
                {
                    id: `current-${currentStateAt || currentState}`,
                    jobId: "",
                    subjectId: textValue(raw?.channel_id, raw?.source_channel_id, raw?.id),
                    state: currentState,
                    reasonCode,
                    provider: textValue(raw?.current_provider, "Current aggregate"),
                    rawReason: reasonCode,
                    detail: "Current normalized observation. Historical observations appear here when supplied by the server.",
                    observedAt: currentStateAt,
                    explicit: currentState.includes("explicit"),
                    toolVersion: "",
                    structured: { aggregate: true, reason_code: reasonCode },
                },
            ];
        }
        return {
            channelId: textValue(raw?.channel_id, raw?.source_channel_id, raw?.id),
            displayName: textValue(
                raw?.display_name,
                raw?.channel_name,
                raw?.name,
                "Unknown channel",
            ),
            provenance: textValue(
                raw?.provenance,
                raw?.origin,
                raw?.source_type,
                raw?.recovered ? "recovered" : "tracked",
            ),
            state: currentState,
            designation,
            folderPath: textValue(
                raw?.folder_path,
                raw?.target_path,
                raw?.storage_path,
                raw?.organized_path,
                raw?.storage_component,
            ),
            lastCheckedAt: currentStateAt,
            lastAccessibleAt: textValue(
                aggregate?.last_accessible_at,
                raw?.last_accessible_at,
            ),
            reasonCode,
            terminationCandidate: candidate,
            terminationEligibility,
            terminationConfirmed: boolValue(
                raw?.termination_confirmed,
                aggregate?.termination_confirmed,
                designation === "terminated",
            ),
            observationCount: numericValue(
                raw?.observation_count ?? aggregate?.observation_count ?? evidence.length,
            ),
            localVideoCount: numericValue(raw?.local_video_count),
            revision: textValue(raw?.revision),
            activeJobId: textValue(
                raw?.active_job_id,
                raw?.active_job?.job_id,
                aggregate?.active_job_id,
            ),
            evidence,
        };
    }

    function normalizeVideo(raw: any): VideoRecord {
        const availability = raw?.availability || raw?.current || {};
        return {
            videoId: textValue(raw?.video_id, raw?.youtube_id, raw?.id),
            title: textValue(raw?.title, raw?.filename, "Untitled archive entry"),
            state: textValue(
                availability?.state,
                raw?.current_state,
                raw?.availability_state,
                raw?.state,
                "unchecked",
            ),
            rawReason: textValue(
                availability?.raw_reason,
                raw?.raw_reason,
                raw?.reason,
                raw?.last_reason,
                raw?.reason_code,
            ),
            detail: textValue(availability?.detail, raw?.detail, raw?.message),
            lastCheckedAt: textValue(
                availability?.last_checked_at,
                raw?.last_checked_at,
                raw?.checked_at,
                raw?.state_at,
            ),
            localPath: textValue(
                raw?.local_path,
                raw?.preview_path,
                raw?.filepath,
                raw?.source_path,
                raw?.organized_path,
            ),
            previewUrl: textValue(raw?.preview_url, raw?.video_preview_url),
            audioPreviewUrl: textValue(raw?.audio_preview_url),
        };
    }

    function normalizeJob(raw: any, fallbackKind: AvailabilityJob["kind"]): AvailabilityJob {
        const job = raw?.job || raw;
        return {
            jobId: textValue(job?.job_id, job?.id),
            channelId: textValue(job?.channel_id),
            status: textValue(job?.status, job?.state, "queued"),
            phase: textValue(job?.phase, job?.stage),
            current: numericValue(
                job?.current ?? job?.completed ?? job?.progress?.current,
            ),
            total: numericValue(job?.total ?? job?.progress?.total),
            message: textValue(job?.message, job?.detail, job?.progress?.message),
            error: textValue(job?.error, job?.failure_reason),
            kind: (textValue(job?.kind, job?.job_type, fallbackKind) as AvailabilityJob["kind"]),
        };
    }

    function flattenInventory(raw: any): PlanInventoryItem[] {
        const inventory = raw?.inventory ?? raw?.entries ?? [];
        const rows: PlanInventoryItem[] = [];
        const append = (item: any, fallbackKind = "entry") => {
            if (typeof item === "string") {
                rows.push({ kind: fallbackKind, path: item, detail: "", status: "" });
                return;
            }
            rows.push({
                kind: textValue(item?.kind, item?.type, item?.entry_type, fallbackKind),
                path: textValue(item?.path, item?.source_path, item?.link_path),
                detail: textValue(item?.detail, item?.message, item?.target),
                status: textValue(item?.status, item?.state),
            });
        };
        if (Array.isArray(inventory)) {
            inventory.forEach((item) => append(item));
        } else if (inventory && typeof inventory === "object") {
            for (const [kind, items] of Object.entries(inventory)) {
                if (Array.isArray(items)) items.forEach((item) => append(item, kind));
            }
        }
        return rows;
    }

    function stringList(value: any): string[] {
        if (!value) return [];
        if (Array.isArray(value)) {
            return value.map((item) =>
                typeof item === "string"
                    ? item
                    : textValue(item?.message, item?.detail, item?.code),
            ).filter(Boolean);
        }
        return [String(value)];
    }

    function normalizePlan(raw: any): DesignationPlan {
        const plan = raw?.plan || raw;
        const blockers = stringList(plan?.blockers || plan?.errors);
        const applyFlag = plan?.can_apply ?? plan?.safe_to_apply;
        return {
            token: textValue(plan?.plan_token, plan?.token),
            revision: textValue(plan?.revision, plan?.registry_revision),
            sourcePath: textValue(
                plan?.source_path,
                plan?.old_path,
                plan?.from_path,
                plan?.source_component,
            ),
            destinationPath: textValue(
                plan?.destination_path,
                plan?.new_path,
                plan?.to_path,
                plan?.destination_component,
            ),
            location: textValue(plan?.location, plan?.root, plan?.visibility),
            canApply:
                applyFlag === undefined
                    ? blockers.length === 0
                    : Boolean(applyFlag),
            blockers,
            warnings: stringList(plan?.warnings),
            counts: plan?.counts && typeof plan.counts === "object" ? plan.counts : {},
            inventory: flattenInventory(plan),
            expiresAt: textValue(plan?.expires_at),
        };
    }

    async function readResponse(response: Response): Promise<any> {
        if (response.status === 401) {
            window.location.href = "/login";
            throw new Error("Your session expired.");
        }
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(
                body?.error ||
                    body?.message ||
                    `Request failed with status ${response.status}`,
            );
        }
        return body;
    }

    function errorMessage(error: unknown): string {
        return error instanceof Error ? error.message : String(error);
    }

    function requestWasAborted(error: unknown): boolean {
        return error instanceof Error && error.name === "AbortError";
    }

    function stopJobPolling() {
        if (jobPollTimer) clearTimeout(jobPollTimer);
        jobPollTimer = null;
    }

    function bindJobToSelectedChannel(channel: ChannelRecord | null | undefined) {
        stopJobPolling();
        if (!channel?.activeJobId) {
            activeJob = null;
            jobError = "";
            return;
        }
        if (activeJob?.jobId !== channel.activeJobId) {
            activeJob = normalizeJob(
                { job_id: channel.activeJobId, channel_id: channel.channelId },
                "unknown",
            );
        }
        scheduleJobPoll(0);
    }

    async function loadChannels(keepSelection = true) {
        channelRequestController?.abort();
        const controller = new AbortController();
        channelRequestController = controller;
        loadingChannels = true;
        channelError = "";
        try {
            const response = await fetch("/api/availability/channels", {
                signal: controller.signal,
            });
            const data = await readResponse(response);
            if (
                controller !== channelRequestController ||
                controller.signal.aborted
            ) return;
            apiCorroborationEnabled = Boolean(data?.api_corroboration_enabled);
            const records = arrayFrom(data, "channels", "items", "records");
            channels = records.map(normalizeChannel).filter((channel) => channel.channelId);

            if (!keepSelection || !channels.some((item) => item.channelId === selectedChannelId)) {
                selectedChannelId = channels[0]?.channelId || "";
            }

            const selected = channels.find((item) => item.channelId === selectedChannelId);
            bindJobToSelectedChannel(selected);
        } catch (error) {
            if (requestWasAborted(error) || controller.signal.aborted) return;
            if (controller !== channelRequestController) return;
            channelError = errorMessage(error);
        } finally {
            if (controller === channelRequestController) {
                channelRequestController = null;
                loadingChannels = false;
            }
        }
    }

    async function loadVideos(channelId = selectedChannelId, page = videoPage) {
        videoRequestController?.abort();
        if (!channelId) {
            videoRequestController = null;
            loadingVideos = false;
            videos = [];
            videoPage = 1;
            videoPages = 1;
            videoTotal = 0;
            return;
        }
        const controller = new AbortController();
        videoRequestController = controller;
        loadingVideos = true;
        videoError = "";
        try {
            const parameters = new URLSearchParams({
                page: String(page),
                per_page: String(videosPerPage),
            });
            const query = videoQuery.trim();
            const state = backendVideoState(videoStateFilter);
            if (query) parameters.set("q", query);
            if (state) parameters.set("state", state);
            const response = await fetch(
                `/api/availability/channels/${encodeURIComponent(channelId)}/videos?${parameters.toString()}`,
                { signal: controller.signal },
            );
            const data = await readResponse(response);
            if (
                controller !== videoRequestController ||
                controller.signal.aborted ||
                channelId !== selectedChannelId
            ) return;
            videos = arrayFrom(data, "videos", "items", "records").map(normalizeVideo);
            videoPage = Math.max(1, numericValue(data?.page) || page);
            videoPages = Math.max(1, numericValue(data?.pages) || 1);
            videoTotal = numericValue(data?.total) || videos.length;
        } catch (error) {
            if (requestWasAborted(error) || controller.signal.aborted) return;
            if (controller !== videoRequestController || channelId !== selectedChannelId) return;
            videoError = errorMessage(error);
            videos = [];
        } finally {
            if (controller === videoRequestController) {
                videoRequestController = null;
                loadingVideos = false;
            }
        }
    }

    function backendVideoState(value: string): string {
        const mapping: Record<string, string> = {
            unchecked: "unknown",
            community_guidelines: "community_guidelines_removed",
            policy_removed: "other_policy_removed",
            transient: "transient_failure",
        };
        if (!value || value === "all") return "";
        return mapping[value] || value;
    }

    function scheduleVideoFilterReload() {
        if (videoFilterTimer) clearTimeout(videoFilterTimer);
        // Invalidate the previous page immediately; the replacement request is
        // intentionally debounced so fast typing cannot repaint stale results.
        videoRequestController?.abort();
        videoFilterTimer = setTimeout(() => {
            videoFilterTimer = null;
            videoPage = 1;
            void loadVideos(selectedChannelId, 1);
        }, 300);
    }

    async function loadEvidence(channelId = selectedChannelId) {
        evidenceRequestController?.abort();
        if (!channelId) {
            evidenceRequestController = null;
            loadingEvidence = false;
            return;
        }
        const controller = new AbortController();
        evidenceRequestController = controller;
        loadingEvidence = true;
        evidenceError = "";
        try {
            const response = await fetch(
                `/api/availability/channels/${encodeURIComponent(channelId)}/observations?subject_type=channel&limit=50`,
                { signal: controller.signal },
            );
            const data = await readResponse(response);
            if (
                controller !== evidenceRequestController ||
                controller.signal.aborted ||
                channelId !== selectedChannelId
            ) return;
            const observations = arrayFrom(data, "observations", "items", "history").map(
                normalizeEvidence,
            );
            channels = channels.map((channel) =>
                channel.channelId === channelId
                    ? {
                          ...channel,
                          evidence: observations,
                          observationCount: preservedObservationCount(
                              channel.observationCount,
                              observations.length,
                              data?.total,
                          ),
                      }
                    : channel,
            );
        } catch (error) {
            if (requestWasAborted(error) || controller.signal.aborted) return;
            if (controller !== evidenceRequestController || channelId !== selectedChannelId) return;
            // Older servers do not expose observation history. Keep the current
            // aggregate row visible so the page remains useful during upgrades.
            evidenceError = `Full history unavailable: ${errorMessage(error)}`;
        } finally {
            if (controller === evidenceRequestController) {
                evidenceRequestController = null;
                loadingEvidence = false;
            }
        }
    }

    async function selectChannel(channelId: string) {
        designationGeneration += 1;
        selectedChannelId = channelId;
        designationPlan = null;
        designationError = "";
        designationSuccess = "";
        planningDesignation = false;
        applyingDesignation = false;
        confirmation = "";
        videoPage = 1;
        bindJobToSelectedChannel(
            channels.find((item) => item.channelId === channelId),
        );
        await Promise.all([loadVideos(channelId, 1), loadEvidence(channelId)]);
    }

    async function changeVideoPage(page: number) {
        if (loadingVideos || page < 1 || page > videoPages) return;
        await loadVideos(selectedChannelId, page);
        document.getElementById("video-title")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function startCheck(
        kind: "channel" | "videos",
        videoIds: string[] | null = null,
    ) {
        if (!selectedChannelId || jobRunning(activeJob)) return;
        const channelId = selectedChannelId;
        jobError = "";
        designationSuccess = "";
        try {
            const suffix = kind === "channel" ? "/check" : "/videos/check";
            const response = await fetch(
                `/api/availability/channels/${encodeURIComponent(channelId)}${suffix}`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body:
                        kind === "videos" && videoIds
                            ? JSON.stringify({ video_ids: videoIds })
                            : "{}",
                },
            );
            const data = await readResponse(response);
            const startedJob = normalizeJob(data, kind);
            const boundJob: AvailabilityJob = {
                ...startedJob,
                channelId: startedJob.channelId || channelId,
            };
            if (boundJob.jobId) {
                // Record a job against its originating channel even if the
                // administrator switched channels while the POST was in
                // flight. Returning later must reveal the active job.
                channels = channels.map((channel) =>
                    channel.channelId === channelId
                        ? { ...channel, activeJobId: boundJob.jobId }
                        : channel,
                );
            }
            if (channelId !== selectedChannelId) return;
            activeJob = boundJob;
            if (!boundJob.jobId) {
                await loadChannels();
                await Promise.all([loadVideos(channelId), loadEvidence(channelId)]);
                return;
            }
            scheduleJobPoll(500);
        } catch (error) {
            jobError = errorMessage(error);
        }
    }

    function scheduleJobPoll(delay = 1800) {
        stopJobPolling();
        if (!activeJob?.jobId || destroyed) return;
        jobPollTimer = setTimeout(pollJob, delay);
    }

    async function pollJob() {
        if (!activeJob?.jobId || destroyed) return;
        const jobId = activeJob.jobId;
        const channelId = activeJob.channelId || selectedChannelId;
        const jobKind = activeJob.kind;
        try {
            const response = await fetch(
                `/api/availability/jobs/${encodeURIComponent(jobId)}`,
            );
            const data = await readResponse(response);
            if (
                activeJob?.jobId !== jobId ||
                selectedChannelId !== channelId
            ) return;
            const polledJob = normalizeJob(data, jobKind);
            activeJob = {
                ...polledJob,
                jobId: polledJob.jobId || jobId,
                channelId: polledJob.channelId || channelId,
            };
            if (jobRunning(activeJob)) {
                scheduleJobPoll();
            } else {
                await loadChannels();
                if (selectedChannelId === channelId) {
                    await Promise.all([
                        loadVideos(channelId),
                        loadEvidence(channelId),
                    ]);
                }
            }
        } catch (error) {
            if (activeJob?.jobId !== jobId || selectedChannelId !== channelId) return;
            jobError = errorMessage(error);
            scheduleJobPoll(4000);
        }
    }

    async function cancelJob() {
        if (!activeJob?.jobId || !jobRunning(activeJob)) return;
        jobError = "";
        try {
            const jobId = activeJob.jobId;
            const jobKind = activeJob.kind;
            const channelId = activeJob.channelId || selectedChannelId;
            const response = await fetch(
                `/api/availability/jobs/${encodeURIComponent(jobId)}/cancel`,
                { method: "POST" },
            );
            const data = await readResponse(response);
            if (activeJob?.jobId !== jobId || selectedChannelId !== channelId) return;
            const updated = normalizeJob(data, jobKind);
            activeJob = {
                ...updated,
                jobId: updated.jobId || jobId,
                channelId: updated.channelId || channelId,
            };
            scheduleJobPoll(500);
        } catch (error) {
            jobError = errorMessage(error);
        }
    }

    async function createDesignationPlan() {
        if (!selectedChannelId || planningDesignation || applyingDesignation) return;
        const channelId = selectedChannelId;
        const generation = ++designationGeneration;
        planningDesignation = true;
        designationPlan = null;
        designationError = "";
        designationSuccess = "";
        confirmation = "";
        try {
            const response = await fetch(
                `/api/availability/channels/${encodeURIComponent(channelId)}/designation/plan`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ designation: "terminated" }),
                },
            );
            const planned = normalizePlan(await readResponse(response));
            if (generation === designationGeneration && channelId === selectedChannelId) {
                designationPlan = planned;
            }
        } catch (error) {
            if (generation === designationGeneration && channelId === selectedChannelId) {
                designationError = errorMessage(error);
            }
        } finally {
            if (generation === designationGeneration) planningDesignation = false;
        }
    }

    async function applyDesignation() {
        if (!selectedChannelId || !designationPlan || !mayApplyPlan) return;
        const channelId = selectedChannelId;
        const planToken = designationPlan.token;
        const generation = ++designationGeneration;
        applyingDesignation = true;
        designationError = "";
        designationSuccess = "";
        try {
            const response = await fetch(
                `/api/availability/channels/${encodeURIComponent(channelId)}/designation/apply`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        plan_token: planToken,
                        confirmation,
                    }),
                },
            );
            const data = await readResponse(response);
            if (generation !== designationGeneration || channelId !== selectedChannelId) return;
            designationSuccess = textValue(
                data?.message,
                "The terminated designation was applied. The media contents were not rewritten.",
            );
            designationPlan = null;
            confirmation = "";
            await loadChannels();
            await loadVideos();
        } catch (error) {
            if (generation === designationGeneration && channelId === selectedChannelId) {
                designationError = errorMessage(error);
            }
        } finally {
            if (generation === designationGeneration) applyingDesignation = false;
        }
    }

    function jobRunning(job: AvailabilityJob | null): boolean {
        return Boolean(
            job && ["queued", "running", "cancelling", "cancel_requested"].includes(normalize(job.status)),
        );
    }

    function jobProgress(job: AvailabilityJob): number {
        if (!job.total) return 0;
        return Math.max(0, Math.min(100, Math.round((job.current / job.total) * 100)));
    }

    function canonicalChannelState(channel: ChannelRecord): string {
        if (channel.terminationCandidate && !channel.terminationConfirmed) {
            return "termination_candidate";
        }
        const state = normalize(channel.state).replace(/[^a-z0-9]+/g, "_");
        if (state.includes("conflict")) return "conflicting";
        if (state.includes("explicit") && (state.includes("termin") || state.includes("suspend"))) {
            return "explicit_termination";
        }
        if (state.includes("access") || state === "available" || state === "public") return "accessible";
        if (state.includes("restrict") || state.includes("auth") || state.includes("geo")) return "restricted";
        if (state.includes("transient") || state.includes("rate") || state.includes("indeterminate")) return "transient";
        if (state.includes("unknown") || state.includes("unavailable") || state.includes("not_found")) return "unavailable_unknown";
        return state || "unchecked";
    }

    function canonicalVideoState(value: string): string {
        const state = normalize(value).replace(/[^a-z0-9]+/g, "_");
        if (!state || state === "unknown" || state === "not_checked") return "unchecked";
        if (state.includes("community")) return "community_guidelines";
        if (state.includes("copyright") || (state.includes("policy") && state.includes("remove"))) return "policy_removed";
        if (state.includes("uploader") || state.includes("deleted")) return "uploader_removed";
        if (state.includes("account") && (state.includes("termin") || state.includes("suspend"))) return "account_terminated";
        if (state.includes("private")) return "private";
        if (state.includes("restrict") || state.includes("auth") || state.includes("geo") || state.includes("member")) return "restricted";
        if (state.includes("live") || state.includes("processing") || state.includes("upcoming")) return "live_processing";
        if (state.includes("transient") || state.includes("rate") || state.includes("indeterminate")) return "transient";
        if (state.includes("available") || state.includes("public") || state.includes("unlisted")) return "available";
        if (state.includes("unknown") || state.includes("unavailable") || state.includes("not_found")) return "unavailable_unknown";
        return state;
    }

    function stateLabel(value: string): string {
        return (value || "unchecked")
            .replace(/[_-]+/g, " ")
            .replace(/\b\w/g, (letter) => letter.toUpperCase());
    }

    function stateTone(value: string): string {
        const state = normalize(value);
        if (state.includes("accessible") || state.includes("available") || state.includes("healthy")) return "good";
        if (state.includes("termin") || state.includes("community") || state.includes("policy") || state.includes("private")) return "danger";
        if (state.includes("candidate") || state.includes("restrict") || state.includes("conflict")) return "warning";
        return "muted";
    }

    function formatTime(value: string): string {
        if (!value) return "Not checked";
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    function thresholdPercent(eligibility: TerminationEligibility): number {
        if (!eligibility.requiredObservations) return 0;
        return Math.max(
            0,
            Math.min(
                100,
                Math.round(
                    (eligibility.thresholdProgress /
                        eligibility.requiredObservations) *
                        100,
                ),
            ),
        );
    }

    function evidenceCountsTowardThreshold(evidence: Evidence): boolean {
        return Boolean(
            selectedChannel?.terminationEligibility.countingObservationIds.includes(
                evidence.id,
            ),
        );
    }

    function nextQualifyingLabel(eligibility: TerminationEligibility): string {
        if (!eligibility.nextQualifyingAt) {
            if (eligibility.eligible) return "Evidence threshold satisfied";
            return eligibility.thresholdProgress > 0
                ? "A later matching explicit result is still required"
                : "Requires a first matching explicit response";
        }
        const nextTimestamp = Date.parse(eligibility.nextQualifyingAt);
        if (Number.isFinite(nextTimestamp) && nextTimestamp <= Date.now()) {
            return `Check now · threshold time passed ${formatTime(eligibility.nextQualifyingAt)}`;
        }
        return formatTime(eligibility.nextQualifyingAt);
    }

    function basename(path: string): string {
        return path.replace(/\\/g, "/").split("/").pop() || path;
    }

    function previewLink(video: VideoRecord, mode: "video" | "audio"): string {
        if (!video.localPath) return "";
        const endpoint = mode === "audio" ? "/api/recovery/preview-audio" : "/api/recovery/preview";
        return `${endpoint}?filepath=${encodeURIComponent(video.localPath)}`;
    }

    function evidenceSignals(evidence: Evidence): EvidenceSignal[] {
        return structuredEvidenceSignals(evidence.structured) as EvidenceSignal[];
    }

    function hasStructuredEvidence(evidence: Evidence): boolean {
        return Object.keys(evidence.structured || {}).length > 0;
    }

    function evidenceJson(evidence: Evidence): string {
        return boundedEvidenceJson(evidence.structured);
    }

    onMount(async () => {
        await loadChannels(false);
        if (selectedChannelId) {
            await Promise.all([loadVideos(), loadEvidence()]);
        }
    });

    onDestroy(() => {
        destroyed = true;
        stopJobPolling();
        if (videoFilterTimer) clearTimeout(videoFilterTimer);
        channelRequestController?.abort();
        videoRequestController?.abort();
        evidenceRequestController?.abort();
    });
</script>

<div class="availability-page">
    <header class="page-heading">
        <div>
            <h1><i class="bi bi-broadcast-pin" aria-hidden="true"></i> Availability</h1>
            <p>Gather online evidence without changing archived media. Exact channel and video IDs are the identity boundary.</p>
        </div>
        <span class="page-heading__status">
            <span class="status-dot"></span>
            {channels.length} {channels.length === 1 ? "channel" : "channels"} mapped
        </span>
    </header>

    <div class="safety-note" role="note">
        <i class="bi bi-shield-check" aria-hidden="true"></i>
        <span><strong>Observation is not designation.</strong> Generic unavailability remains unknown. A folder can be relabeled only after repeated explicit evidence, review, a dry run, and typed confirmation.</span>
    </div>

    {#if channelError}
        <div class="notice notice--error" role="alert">{channelError}</div>
    {/if}

    <div class="availability-layout">
        <aside class="panel channel-browser" aria-labelledby="channel-browser-title">
            <div class="panel-header channel-browser__heading">
                <div>
                    <h2 class="section-title" id="channel-browser-title">Channel identity</h2>
                    <p class="section-description">Choose a canonical UCID.</p>
                </div>
                <button class="button-quiet icon-button" on:click={() => loadChannels()} disabled={loadingChannels} aria-label="Refresh channels" title="Refresh channels">
                    <i class:spin={loadingChannels} class="bi bi-arrow-clockwise" aria-hidden="true"></i>
                </button>
            </div>
            <div class="browser-controls">
                <label>
                    <span class="sr-only">Search channels</span>
                    <i class="bi bi-search" aria-hidden="true"></i>
                    <input bind:value={channelQuery} placeholder="Name or exact UCID" />
                </label>
                <label>
                    <span class="sr-only">Filter channel state</span>
                    <select bind:value={channelStateFilter} aria-label="Filter channel state">
                        {#each channelStates as state}
                            <option value={state[0]}>{state[1]}</option>
                        {/each}
                    </select>
                </label>
            </div>
            <div class="channel-list" role="list" aria-busy={loadingChannels}>
                {#if loadingChannels && channels.length === 0}
                    <div class="empty-state"><i class="bi bi-arrow-repeat spin"></i><span>Loading channel identities…</span></div>
                {:else if filteredChannels.length === 0}
                    <div class="empty-state"><i class="bi bi-search"></i><span>No mapped channels match this filter.</span></div>
                {:else}
                    {#each filteredChannels as channel (channel.channelId)}
                        {@const displayState = canonicalChannelState(channel)}
                        <button
                            class="channel-row"
                            class:selected={selectedChannelId === channel.channelId}
                            type="button"
                            on:click={() => selectChannel(channel.channelId)}
                            aria-current={selectedChannelId === channel.channelId ? "true" : undefined}
                            role="listitem"
                        >
                            <span class="channel-row__top">
                                <strong>{channel.displayName}</strong>
                                <span class="state-badge state-badge--{stateTone(displayState)}">{stateLabel(displayState)}</span>
                            </span>
                            <code>{channel.channelId}</code>
                            <span class="channel-row__meta">
                                <span>{stateLabel(channel.provenance)}</span>
                                <span>{channel.observationCount} observations</span>
                            </span>
                        </button>
                    {/each}
                {/if}
            </div>
        </aside>

        <main class="channel-workspace">
            {#if !selectedChannel}
                <section class="panel empty-workspace">
                    <i class="bi bi-broadcast"></i>
                    <h2>No channel selected</h2>
                    <p>Select a canonical channel identity to inspect its evidence and local videos.</p>
                </section>
            {:else}
                <section class="panel identity-panel">
                    <div class="identity-heading">
                        <div class="identity-heading__copy">
                            <span class="eyebrow">Exact channel identity</span>
                            <h2>{selectedChannel.displayName}</h2>
                            <a class="ucid-link" href={`https://www.youtube.com/channel/${selectedChannel.channelId}`} target="_blank" rel="noreferrer">
                                <code>{selectedChannel.channelId}</code>
                                <i class="bi bi-box-arrow-up-right" aria-hidden="true"></i>
                            </a>
                        </div>
                        <span class="state-badge state-badge--large state-badge--{stateTone(canonicalChannelState(selectedChannel))}">
                            {stateLabel(canonicalChannelState(selectedChannel))}
                        </span>
                    </div>
                    <dl class="identity-facts">
                        <div><dt>Provenance</dt><dd>{stateLabel(selectedChannel.provenance)}</dd></div>
                        <div><dt>Last checked</dt><dd>{formatTime(selectedChannel.lastCheckedAt)}</dd></div>
                        <div><dt>Last accessible</dt><dd>{formatTime(selectedChannel.lastAccessibleAt)}</dd></div>
                        <div><dt>Designation</dt><dd>{selectedChannel.designation ? stateLabel(selectedChannel.designation) : "None"}</dd></div>
                        <div>
                            <dt>API corroboration (optional)</dt>
                            <dd>{apiCorroborationEnabled ? "Configured — not required" : "Not configured — checks still work"}</dd>
                        </div>
                        <div><dt>Current reason</dt><dd><code>{selectedChannel.reasonCode || "No reason recorded"}</code></dd></div>
                        <div class="identity-facts__path"><dt>Current folder</dt><dd><code title={selectedChannel.folderPath}>{selectedChannel.folderPath || "Not published in Target"}</code></dd></div>
                    </dl>
                    <div class="identity-actions">
                        <button class="button-primary" on:click={() => startCheck("channel")} disabled={jobRunning(activeJob)}>
                            <i class="bi bi-broadcast" aria-hidden="true"></i>
                            Check channel now
                        </button>
                        <button class="button" on:click={() => startCheck("videos")} disabled={jobRunning(activeJob) || (selectedChannel.localVideoCount === 0 && videoTotal === 0)}>
                            <i class="bi bi-collection-play" aria-hidden="true"></i>
                            Check {selectedChannel.localVideoCount || videoTotal} local video IDs
                        </button>
                        <button
                            class="button button-quiet"
                            on:click={() => startCheck("videos", videos.map((video) => video.videoId))}
                            disabled={jobRunning(activeJob) || loadingVideos || videos.length === 0}
                            title="Check only the exact IDs on the current filtered page"
                        >
                            <i class="bi bi-list-check" aria-hidden="true"></i>
                            Check {videos.length} shown IDs
                        </button>
                        <span>Checks do not download, move, or rename media.</span>
                    </div>
                </section>

                {#if activeJob || jobError}
                    <section class="panel job-panel" aria-live="polite">
                        {#if activeJob}
                            <div class="job-panel__copy">
                                <span class="eyebrow">Persistent server job</span>
                                <strong>{stateLabel(activeJob.kind)} check · {stateLabel(activeJob.status)}</strong>
                                <span>{activeJob.message || activeJob.phase || "Waiting for progress…"}</span>
                            </div>
                            <div class="job-progress" aria-label={`${jobProgress(activeJob)} percent complete`}>
                                <span style={`width: ${jobProgress(activeJob)}%`}></span>
                            </div>
                            <div class="job-panel__numbers">
                                <span>{activeJob.total ? `${activeJob.current} of ${activeJob.total}` : stateLabel(activeJob.phase || activeJob.status)}</span>
                                {#if jobRunning(activeJob)}
                                    <button class="button-danger" on:click={cancelJob}>Cancel check</button>
                                {/if}
                            </div>
                            {#if activeJob.error}<div class="notice notice--error">{activeJob.error}</div>{/if}
                        {/if}
                        {#if jobError}<div class="notice notice--error">{jobError}</div>{/if}
                    </section>
                {/if}

                <div class="workspace-grid">
                    <section class="panel evidence-panel" aria-labelledby="evidence-title">
                        <div class="panel-header">
                            <div>
                                <h2 class="section-title" id="evidence-title"><i class="bi bi-journal-text"></i> Channel evidence</h2>
                                <p class="section-description">Raw provider reasons remain visible for human review.</p>
                            </div>
                            <span class="count-label">
                                {selectedChannel.evidence.length} shown
                                {#if selectedChannel.observationCount > selectedChannel.evidence.length}
                                    · {selectedChannel.observationCount} total
                                {/if}
                            </span>
                        </div>
                        <div class="evidence-list">
                            {#if loadingEvidence}
                                <div class="evidence-loading"><i class="bi bi-arrow-repeat spin"></i> Loading complete observation history…</div>
                            {/if}
                            {#if evidenceError}
                                <div class="evidence-fallback"><i class="bi bi-info-circle"></i> {evidenceError}</div>
                            {/if}
                            {#if selectedChannel.evidence.length === 0 && !loadingEvidence}
                                <div class="empty-state"><i class="bi bi-journal-x"></i><span>No channel observations have been recorded.</span></div>
                            {:else}
                                {#each selectedChannel.evidence as evidence (evidence.id)}
                                    {@const signals = evidenceSignals(evidence)}
                                    <article class="evidence-row">
                                        <div class="evidence-row__heading">
                                            <span class="state-badge state-badge--{stateTone(evidence.state)}">{stateLabel(evidence.state)}</span>
                                            <time>{formatTime(evidence.observedAt)}</time>
                                        </div>
                                        <div class="evidence-row__source">
                                            <strong>
                                                {evidence.provider}
                                                {#if evidence.toolVersion}<small> · {evidence.toolVersion}</small>{/if}
                                            </strong>
                                            <div class="evidence-row__flags">
                                                {#if evidenceCountsTowardThreshold(evidence)}
                                                    <span class="threshold-counting"><i class="bi bi-shield-check"></i> Counts toward threshold</span>
                                                {/if}
                                                {#if evidence.explicit}<span><i class="bi bi-check2-circle"></i> Explicit response</span>{/if}
                                            </div>
                                        </div>
                                        <div class="evidence-identifiers">
                                            <span>Observation <code>#{evidence.id}</code></span>
                                            {#if evidence.jobId}<span>Job <code>{evidence.jobId}</code></span>{/if}
                                            {#if evidence.subjectId}<span>Subject <code>{evidence.subjectId}</code></span>{/if}
                                            {#if evidence.reasonCode}<span>Reason <code>{evidence.reasonCode}</code></span>{/if}
                                        </div>
                                        {#if evidence.rawReason}<pre class="raw-reason">{evidence.rawReason}</pre>{/if}
                                        {#if evidence.detail}<p>{evidence.detail}</p>{/if}
                                        {#if hasStructuredEvidence(evidence)}
                                            <details class="structured-evidence">
                                                <summary>
                                                    <span><i class="bi bi-braces"></i> Structured provider evidence</span>
                                                    <small>{signals.length} key {signals.length === 1 ? "signal" : "signals"}</small>
                                                </summary>
                                                {#if signals.length > 0}
                                                    <dl class="evidence-signals">
                                                        {#each signals as signal}
                                                            <div>
                                                                <dt>{signal.key}</dt>
                                                                <dd>{signal.value}</dd>
                                                            </div>
                                                        {/each}
                                                    </dl>
                                                {/if}
                                                <pre>{evidenceJson(evidence)}</pre>
                                            </details>
                                        {/if}
                                    </article>
                                {/each}
                            {/if}
                        </div>
                    </section>

                    <section class="panel designation-panel" aria-labelledby="designation-title">
                        <div class="panel-header">
                            <div>
                                <h2 class="section-title" id="designation-title"><i class="bi bi-folder-symlink"></i> Terminated designation</h2>
                                <p class="section-description">A separate, guarded filesystem action.</p>
                            </div>
                        </div>
                        <div class="designation-body">
                            <div
                                class="eligibility-threshold eligibility-threshold--{selectedEligibilityCopy?.tone || 'muted'}"
                                role="status"
                                aria-live="polite"
                            >
                                {#if selectedChannel.terminationEligibility.diagnosticsAvailable}
                                <div class="eligibility-threshold__heading">
                                    <div>
                                        <span class="eyebrow">Evidence threshold</span>
                                        <strong>
                                            {selectedChannel.terminationEligibility.thresholdProgress}
                                            of {selectedChannel.terminationEligibility.requiredObservations}
                                            qualifying observations
                                        </strong>
                                    </div>
                                    <span>{selectedChannel.terminationEligibility.minimumSeparationHours}h minimum</span>
                                </div>
                                <div
                                    class="eligibility-threshold__bar"
                                    role="progressbar"
                                    aria-label="Termination evidence threshold"
                                    aria-valuemin="0"
                                    aria-valuemax={selectedChannel.terminationEligibility.requiredObservations}
                                    aria-valuenow={selectedChannel.terminationEligibility.thresholdProgress}
                                >
                                    <span style={`width: ${thresholdPercent(selectedChannel.terminationEligibility)}%`}></span>
                                </div>
                                <div class="eligibility-threshold__status">
                                    <i class="bi bi-info-circle" aria-hidden="true"></i>
                                    <span>
                                        <strong>{selectedEligibilityCopy?.title}</strong>
                                        {selectedEligibilityCopy?.detail}
                                    </span>
                                </div>
                                <dl class="eligibility-threshold__facts">
                                    <div>
                                        <dt>Matching explicit reason</dt>
                                        <dd><code>{selectedChannel.terminationEligibility.matchingReasonCode || "None recorded"}</code></dd>
                                    </div>
                                    <div>
                                        <dt>Matching responses</dt>
                                        <dd>{selectedChannel.terminationEligibility.matchingObservations} observed · {selectedChannel.terminationEligibility.qualifyingObservationsNeeded} still needed</dd>
                                    </div>
                                    <div>
                                        <dt>First matching response</dt>
                                        <dd>{formatTime(selectedChannel.terminationEligibility.firstObservedAt)}</dd>
                                    </div>
                                    <div>
                                        <dt>Next useful follow-up</dt>
                                        <dd>{nextQualifyingLabel(selectedChannel.terminationEligibility)}</dd>
                                    </div>
                                </dl>
                                {#if selectedChannel.terminationEligibility.status === "evidence_reset" && selectedChannel.terminationEligibility.lastBlockingObservation}
                                    <p class="eligibility-threshold__blocker">
                                        <i class="bi bi-arrow-counterclockwise" aria-hidden="true"></i>
                                        Evidence reset by {stateLabel(selectedChannel.terminationEligibility.lastBlockingObservation.state)} at {formatTime(selectedChannel.terminationEligibility.lastBlockingObservation.observedAt)}.
                                    </p>
                                {/if}
                                {:else}
                                    <div class="eligibility-threshold__status">
                                        <i class="bi bi-info-circle" aria-hidden="true"></i>
                                        <span>
                                            <strong>Detailed threshold unavailable</strong>
                                            This server returned only its legacy candidate decision. No observation counts or timing details are inferred in the browser.
                                        </span>
                                    </div>
                                {/if}
                                <p class="eligibility-threshold__api">
                                    <i class="bi bi-key" aria-hidden="true"></i>
                                    YouTube API corroboration is optional{apiCorroborationEnabled ? " and configured" : " and not configured"}. It is not required and never substitutes for explicit matching evidence.
                                </p>
                            </div>

                            {#if selectedChannel.terminationConfirmed || selectedChannel.designation === "terminated"}
                                <div class="designation-state designation-state--confirmed">
                                    <i class="bi bi-check-circle"></i>
                                    <span><strong>Designation confirmed</strong>The channel folder is registered with the terminated marker.</span>
                                </div>
                            {:else if selectedChannel.terminationCandidate}
                                <div class="designation-state designation-state--candidate">
                                    <i class="bi bi-exclamation-diamond"></i>
                                    <span><strong>Eligible for administrator review</strong>Repeated explicit observations satisfy the evidence threshold.</span>
                                </div>
                            {:else}
                                <div class="designation-state">
                                    <i class="bi bi-shield-lock"></i>
                                    <span><strong>Rename action locked</strong>The server has not marked this channel as a termination candidate.</span>
                                </div>
                            {/if}

                            <p class="designation-explainer">The dry run inventories both symlinks and real files. Applying it changes the top-level channel folder name while preserving media contents and link targets.</p>

                            <button class="button" on:click={createDesignationPlan} disabled={!selectedChannel.terminationCandidate || planningDesignation || applyingDesignation || jobRunning(activeJob)}>
                                <i class:spin={planningDesignation} class="bi bi-clipboard-check"></i>
                                {planningDesignation ? "Building dry run…" : "Review folder rename"}
                            </button>
                        </div>
                    </section>
                </div>

                <section class="panel video-panel" aria-labelledby="video-title">
                    <div class="panel-header video-panel__heading">
                        <div>
                            <h2 class="section-title" id="video-title"><i class="bi bi-film"></i> Local archived videos</h2>
                            <p class="section-description">Online availability is independent from local media-integrity health.</p>
                        </div>
                        <span class="count-label">{filteredVideos.length} shown · {videoTotal} total</span>
                    </div>
                    <div class="video-controls">
                        <label>
                            <span class="sr-only">Search local videos</span>
                            <i class="bi bi-search" aria-hidden="true"></i>
                            <input bind:value={videoQuery} on:input={scheduleVideoFilterReload} placeholder="Video ID, title, reason, or path" />
                        </label>
                        <label>
                            <span class="sr-only">Filter video state</span>
                            <select bind:value={videoStateFilter} on:change={scheduleVideoFilterReload} aria-label="Filter video availability state">
                                {#each videoStates as state}
                                    <option value={state[0]}>{state[1]}</option>
                                {/each}
                            </select>
                        </label>
                    </div>
                    {#if videoError}<div class="notice notice--error">{videoError}</div>{/if}
                    <div class="video-list" aria-busy={loadingVideos}>
                        {#if loadingVideos}
                            <div class="empty-state"><i class="bi bi-arrow-repeat spin"></i><span>Loading local video identities…</span></div>
                        {:else if filteredVideos.length === 0}
                            <div class="empty-state"><i class="bi bi-camera-video-off"></i><span>No local videos match this filter.</span></div>
                        {:else}
                            {#each filteredVideos as video (video.videoId)}
                                {@const videoState = canonicalVideoState(video.state)}
                                <article class="video-row">
                                    <div class="video-row__identity">
                                        <div class="video-row__title">
                                            <strong>{video.title}</strong>
                                            <span class="state-badge state-badge--{stateTone(videoState)}">{stateLabel(videoState)}</span>
                                        </div>
                                        <a href={`https://www.youtube.com/watch?v=${video.videoId}`} target="_blank" rel="noreferrer"><code>{video.videoId}</code><i class="bi bi-box-arrow-up-right"></i></a>
                                        {#if video.rawReason}<p>{video.rawReason}</p>{/if}
                                        {#if video.detail}<small>{video.detail}</small>{/if}
                                        <span class="video-row__path" title={video.localPath}>{video.localPath ? basename(video.localPath) : "Local path unavailable"}</span>
                                    </div>
                                    <div class="video-row__actions">
                                        <time>{formatTime(video.lastCheckedAt)}</time>
                                        {#if previewLink(video, "video")}
                                            <a class="button" href={previewLink(video, "video")} target="_blank" rel="noreferrer"><i class="bi bi-play-fill"></i> Video</a>
                                            <a class="button-quiet" href={previewLink(video, "audio")} target="_blank" rel="noreferrer"><i class="bi bi-headphones"></i> Audio</a>
                                        {/if}
                                    </div>
                                </article>
                            {/each}
                        {/if}
                    </div>
                    {#if videoPages > 1}
                        <nav class="video-pager" aria-label="Local videos pages">
                            <button class="button" on:click={() => changeVideoPage(videoPage - 1)} disabled={loadingVideos || videoPage <= 1}>
                                <i class="bi bi-chevron-left"></i> Previous
                            </button>
                            <span>Page <strong>{videoPage}</strong> of <strong>{videoPages}</strong></span>
                            <button class="button" on:click={() => changeVideoPage(videoPage + 1)} disabled={loadingVideos || videoPage >= videoPages}>
                                Next <i class="bi bi-chevron-right"></i>
                            </button>
                        </nav>
                    {/if}
                </section>

                {#if designationError || designationSuccess}
                    <div class:notice--error={Boolean(designationError)} class="notice" role="status">{designationError || designationSuccess}</div>
                {/if}

                {#if designationPlan}
                    <section class="panel dry-run-panel" aria-labelledby="dry-run-title">
                        <div class="panel-header">
                            <div>
                                <span class="eyebrow">No changes made</span>
                                <h2 class="section-title" id="dry-run-title"><i class="bi bi-clipboard-data"></i> Folder designation dry run</h2>
                            </div>
                            <span class="state-badge state-badge--{designationPlan.canApply && designationPlan.blockers.length === 0 ? "good" : "danger"}">
                                {designationPlan.canApply && designationPlan.blockers.length === 0 ? "Ready for confirmation" : "Blocked"}
                            </span>
                        </div>
                        <div class="rename-paths">
                            <div><span>Current folder</span><code>{designationPlan.sourcePath || selectedChannel.folderPath || "Not provided"}</code></div>
                            <i class="bi bi-arrow-right" aria-hidden="true"></i>
                            <div><span>Proposed folder</span><code>{designationPlan.destinationPath || "Not provided"}</code></div>
                        </div>

                        {#if Object.keys(designationPlan.counts).length > 0}
                            <dl class="plan-counts">
                                {#each Object.entries(designationPlan.counts) as count}
                                    <div><dt>{stateLabel(count[0])}</dt><dd>{count[1]}</dd></div>
                                {/each}
                            </dl>
                        {/if}

                        {#if designationPlan.blockers.length > 0}
                            <div class="plan-message plan-message--blocker">
                                <strong><i class="bi bi-slash-octagon"></i> Blockers</strong>
                                <ul>{#each designationPlan.blockers as blocker}<li>{blocker}</li>{/each}</ul>
                            </div>
                        {/if}
                        {#if designationPlan.warnings.length > 0}
                            <div class="plan-message plan-message--warning">
                                <strong><i class="bi bi-exclamation-triangle"></i> Warnings</strong>
                                <ul>{#each designationPlan.warnings as warning}<li>{warning}</li>{/each}</ul>
                            </div>
                        {/if}

                        <div class="plan-inventory">
                            {#if designationPlan.inventory.length === 0}
                                <div class="empty-state"><i class="bi bi-inboxes"></i><span>The server did not return item-level inventory rows.</span></div>
                            {:else}
                                {#each designationPlan.inventory as item}
                                    <div class="plan-inventory__row">
                                        <span class="entry-kind">{stateLabel(item.kind)}</span>
                                        <code title={item.path}>{item.path || "Path unavailable"}</code>
                                        <span>{item.detail || item.status}</span>
                                    </div>
                                {/each}
                            {/if}
                        </div>

                        <div class="apply-dock">
                            <div class="apply-dock__copy">
                                <strong>Administrator confirmation</strong>
                                <span>Type <code>{confirmationPhrase}</code> exactly. The plan token and current registry revision are revalidated by the server.</span>
                            </div>
                            <input bind:value={confirmation} autocomplete="off" spellcheck="false" aria-label="Typed termination confirmation" placeholder={confirmationPhrase} disabled={!designationPlan.canApply || designationPlan.blockers.length > 0} />
                            <button class="button-danger" on:click={applyDesignation} disabled={!mayApplyPlan || applyingDesignation}>
                                <i class:spin={applyingDesignation} class="bi bi-folder-symlink"></i>
                                {applyingDesignation ? "Applying designation…" : "Apply terminated designation"}
                            </button>
                        </div>
                    </section>
                {/if}
            {/if}
        </main>
    </div>
</div>

<style>
    .availability-page { display: grid; gap: 12px; min-width: 0; }
    .page-heading { display: flex; min-height: 64px; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 12px; border: 1px solid var(--line-soft); background: var(--surface); }
    .page-heading h1 { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 15px; font-weight: 700; }
    .page-heading h1 i { color: var(--accent); }
    .page-heading p { max-width: 720px; margin: 3px 0 0; color: var(--text-muted); font-size: 11px; }
    .page-heading__status { display: inline-flex; align-items: center; gap: 7px; color: var(--text-muted); font-size: 11px; white-space: nowrap; }
    .safety-note { display: flex; align-items: flex-start; gap: 9px; padding: 9px 11px; border: 1px solid var(--line-soft); border-left: 3px solid var(--accent); background: var(--page-deep); color: var(--text-soft); font-size: 11px; }
    .safety-note i { margin-top: 1px; color: var(--accent); }
    .availability-layout { display: grid; grid-template-columns: minmax(250px, 310px) minmax(0, 1fr); gap: 12px; align-items: start; min-width: 0; }
    .channel-browser { position: sticky; top: 57px; display: flex; max-height: calc(100dvh - 70px); min-width: 0; flex-direction: column; overflow: hidden; }
    .channel-browser__heading { flex: none; }
    .icon-button { width: 32px; min-width: 32px; padding: 4px; }
    .browser-controls, .video-controls { display: grid; gap: 7px; padding: 8px; border-bottom: 1px solid var(--line-soft); background: var(--surface); }
    .browser-controls label, .video-controls label { position: relative; min-width: 0; }
    .browser-controls label > i, .video-controls label > i { position: absolute; top: 50%; left: 9px; color: var(--text-muted); transform: translateY(-50%); pointer-events: none; }
    .browser-controls input, .video-controls input { width: 100%; padding: 6px 8px 6px 29px; }
    .browser-controls select, .video-controls select { width: 100%; padding: 5px 7px; }
    .channel-list { min-height: 180px; flex: 1; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .channel-row { display: grid; width: 100%; gap: 5px; padding: 9px 10px; border: 0; border-bottom: 1px solid var(--line-soft); background: var(--surface); color: var(--text); text-align: left; cursor: pointer; }
    .channel-row:hover, .channel-row.selected { background: var(--surface-hover); }
    .channel-row.selected { box-shadow: inset 3px 0 var(--accent); }
    .channel-row__top { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 8px; }
    .channel-row__top strong { min-width: 0; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .channel-row code, .ucid-link code { color: var(--accent-strong); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; overflow-wrap: anywhere; }
    .channel-row__meta { display: flex; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 9px; }
    .channel-workspace { display: grid; min-width: 0; gap: 12px; }
    .empty-workspace { display: grid; min-height: 280px; place-items: center; align-content: center; gap: 6px; color: var(--text-muted); text-align: center; }
    .empty-workspace > i { color: var(--accent); font-size: 28px; }
    .empty-workspace h2, .empty-workspace p { margin: 0; }
    .identity-panel { overflow: hidden; }
    .identity-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; padding: 12px; border-bottom: 1px solid var(--line-soft); background: var(--surface-raised); }
    .identity-heading__copy { min-width: 0; }
    .eyebrow { display: block; margin-bottom: 3px; color: var(--text-muted); font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .identity-heading h2 { margin: 0 0 4px; font-size: 17px; line-height: 1.2; }
    .ucid-link { display: inline-flex; max-width: 100%; align-items: center; gap: 6px; color: var(--accent); text-decoration: none; }
    .ucid-link:hover { color: var(--text); }
    .identity-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; }
    .identity-facts > div { min-width: 0; padding: 8px 10px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
    .identity-facts > div:nth-child(3), .identity-facts > div:nth-child(6) { border-right: 0; }
    .identity-facts__path { grid-column: 1 / -1; border-right: 0 !important; }
    .identity-facts dt { color: var(--text-muted); font-size: 9px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
    .identity-facts dd { margin: 2px 0 0; color: var(--text-soft); font-size: 11px; }
    .identity-facts code { display: block; overflow: hidden; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .identity-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 9px 10px; }
    .identity-actions > span { margin-left: auto; color: var(--text-muted); font-size: 10px; }
    .state-badge { display: inline-flex; min-height: 20px; flex: none; align-items: center; padding: 2px 6px; border: 1px solid var(--line); background: var(--page-deep); color: var(--text-soft); font-size: 9px; font-weight: 700; letter-spacing: .03em; line-height: 1.2; text-transform: uppercase; }
    .state-badge--large { min-height: 25px; padding: 4px 8px; }
    .state-badge--good { border-color: var(--success); color: #dcebd4; }
    .state-badge--warning { border-color: var(--warning); color: #ebdfb7; }
    .state-badge--danger { border-color: var(--danger); color: #f1d3cc; }
    .state-badge--muted { color: var(--text-muted); }
    .job-panel { display: grid; grid-template-columns: minmax(190px, 1fr) minmax(160px, 1.5fr) auto; align-items: center; gap: 12px; padding: 9px 10px; }
    .job-panel__copy { display: grid; min-width: 0; gap: 2px; }
    .job-panel__copy strong { font-size: 11px; }
    .job-panel__copy > span:last-child { overflow: hidden; color: var(--text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .job-progress { height: 8px; overflow: hidden; border: 1px solid var(--line-soft); background: var(--page-deep); }
    .job-progress span { display: block; height: 100%; background: var(--accent); transition: width 180ms ease-out; }
    .job-panel__numbers { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 10px; white-space: nowrap; }
    .job-panel .notice { grid-column: 1 / -1; }
    .workspace-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(245px, .65fr); gap: 12px; align-items: stretch; min-width: 0; }
    .evidence-panel, .designation-panel { min-width: 0; overflow: hidden; }
    .count-label { color: var(--text-muted); font-size: 10px; white-space: nowrap; }
    .evidence-list { max-height: 370px; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .evidence-row { display: grid; gap: 5px; padding: 9px 10px; border-bottom: 1px solid var(--line-soft); }
    .evidence-row:last-child { border-bottom: 0; }
    .evidence-row__heading, .evidence-row__source { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .evidence-row time { color: var(--text-muted); font-size: 9px; }
    .evidence-row__source { color: var(--text-soft); font-size: 10px; }
    .evidence-row__source span { color: var(--warning); }
    .evidence-row__source small { color: var(--text-muted); font-size: 9px; font-weight: 400; }
    .evidence-row__flags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px 9px; text-align: right; }
    .evidence-row__flags span { display: inline-flex; align-items: center; gap: 4px; }
    .evidence-row__flags .threshold-counting { color: var(--success); font-weight: 700; }
    .evidence-identifiers { display: flex; flex-wrap: wrap; gap: 4px 10px; color: var(--text-muted); font-size: 9px; }
    .evidence-identifiers code { color: var(--accent-strong); overflow-wrap: anywhere; }
    .evidence-row pre { max-width: 100%; margin: 0; padding: 7px; overflow: auto; border: 1px solid var(--line-soft); background: var(--page-deep); color: var(--text-soft); font: 10px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .evidence-row .raw-reason { max-height: 120px; }
    .evidence-row p { margin: 0; color: var(--text-muted); font-size: 10px; }
    .structured-evidence { min-width: 0; border: 1px solid var(--line-soft); background: color-mix(in srgb, var(--page-deep) 72%, var(--surface)); }
    .structured-evidence > summary { display: flex; min-height: 32px; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; color: var(--text-soft); font-size: 10px; cursor: pointer; }
    .structured-evidence > summary:hover { background: var(--surface-hover); }
    .structured-evidence > summary span { display: inline-flex; align-items: center; gap: 6px; }
    .structured-evidence > summary i { color: var(--accent); }
    .structured-evidence > summary small { color: var(--text-muted); font-size: 9px; }
    .evidence-signals { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); margin: 0; border-top: 1px solid var(--line-soft); }
    .evidence-signals > div { min-width: 0; padding: 6px 8px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
    .evidence-signals dt { color: var(--text-muted); font-size: 8px; overflow-wrap: anywhere; text-transform: uppercase; }
    .evidence-signals dd { margin: 2px 0 0; color: var(--accent-strong); font: 9px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
    .structured-evidence > pre { max-height: 220px; margin: 0; border: 0; border-top: 1px solid var(--line-soft); white-space: pre; overscroll-behavior: contain; }
    .evidence-loading, .evidence-fallback { display: flex; align-items: center; gap: 7px; padding: 7px 10px; border-bottom: 1px solid var(--line-soft); background: var(--page-deep); color: var(--text-muted); font-size: 10px; }
    .evidence-loading i { color: var(--accent); }
    .designation-body { display: grid; gap: 10px; padding: 10px; }
    .eligibility-threshold { display: grid; min-width: 0; gap: 8px; padding: 9px; border: 1px solid var(--line-soft); background: var(--page-deep); }
    .eligibility-threshold--good { border-color: var(--success); }
    .eligibility-threshold--warning { border-color: var(--warning); }
    .eligibility-threshold__heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .eligibility-threshold__heading > div { min-width: 0; }
    .eligibility-threshold__heading strong { display: block; font-size: 11px; }
    .eligibility-threshold__heading > span { flex: none; color: var(--text-muted); font-size: 9px; font-weight: 700; text-transform: uppercase; }
    .eligibility-threshold__bar { height: 7px; overflow: hidden; border: 1px solid var(--line-soft); background: var(--surface); }
    .eligibility-threshold__bar span { display: block; height: 100%; background: var(--accent); transition: width 180ms ease-out; }
    .eligibility-threshold--good .eligibility-threshold__bar span { background: var(--success); }
    .eligibility-threshold--warning .eligibility-threshold__bar span { background: var(--warning); }
    .eligibility-threshold__status { display: flex; align-items: flex-start; gap: 7px; padding: 7px; border: 1px solid var(--line-soft); background: var(--surface); color: var(--text-muted); font-size: 9px; }
    .eligibility-threshold__status > i { margin-top: 1px; color: var(--accent); }
    .eligibility-threshold__status span { display: grid; gap: 2px; }
    .eligibility-threshold__status strong { color: var(--text); font-size: 10px; }
    .eligibility-threshold__facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; border: 1px solid var(--line-soft); }
    .eligibility-threshold__facts > div { min-width: 0; padding: 6px 7px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
    .eligibility-threshold__facts > div:nth-child(2n) { border-right: 0; }
    .eligibility-threshold__facts > div:nth-last-child(-n + 2) { border-bottom: 0; }
    .eligibility-threshold__facts dt { color: var(--text-muted); font-size: 8px; font-weight: 700; text-transform: uppercase; }
    .eligibility-threshold__facts dd { margin: 2px 0 0; color: var(--text-soft); font-size: 9px; overflow-wrap: anywhere; }
    .eligibility-threshold__facts code { color: var(--accent-strong); font-size: 9px; }
    .eligibility-threshold__blocker, .eligibility-threshold__api { display: flex; align-items: flex-start; gap: 6px; margin: 0; color: var(--text-muted); font-size: 9px; line-height: 1.4; }
    .eligibility-threshold__blocker { color: var(--warning); }
    .eligibility-threshold__api i { color: var(--accent); }
    .designation-state { display: flex; align-items: flex-start; gap: 8px; padding: 9px; border: 1px solid var(--line-soft); background: var(--page-deep); }
    .designation-state > i { margin-top: 2px; color: var(--text-muted); }
    .designation-state--candidate { border-color: var(--warning); }
    .designation-state--candidate > i { color: var(--warning); }
    .designation-state--confirmed { border-color: var(--success); }
    .designation-state--confirmed > i { color: var(--success); }
    .designation-state span { display: grid; gap: 2px; color: var(--text-muted); font-size: 10px; }
    .designation-state strong { color: var(--text); font-size: 11px; }
    .designation-explainer { margin: 0; color: var(--text-muted); font-size: 10px; }
    .designation-body > button { width: 100%; }
    .video-panel { min-width: 0; overflow: hidden; }
    .video-panel__heading { flex-wrap: wrap; }
    .video-controls { grid-template-columns: minmax(180px, 1fr) minmax(170px, 260px); }
    .video-list { max-height: min(700px, 68dvh); min-height: 120px; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .video-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 9px 10px; border-bottom: 1px solid var(--line-soft); }
    .video-row:last-child { border-bottom: 0; }
    .video-row:hover { background: var(--surface-hover); }
    .video-row__identity { display: grid; min-width: 0; gap: 3px; }
    .video-row__title { display: flex; min-width: 0; align-items: flex-start; gap: 8px; }
    .video-row__title strong { min-width: 0; flex: 1; font-size: 11px; overflow-wrap: anywhere; }
    .video-row__identity > a { display: inline-flex; width: fit-content; align-items: center; gap: 5px; color: var(--accent-strong); text-decoration: none; }
    .video-row__identity code { font-size: 10px; }
    .video-row__identity p, .video-row__identity small { margin: 0; color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
    .video-row__path { overflow: hidden; color: var(--text-muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
    .video-row__actions { display: grid; grid-template-columns: auto auto; align-content: start; justify-content: end; gap: 6px; }
    .video-row__actions time { grid-column: 1 / -1; color: var(--text-muted); font-size: 9px; text-align: right; }
    .video-row__actions a { min-height: 28px; padding: 4px 8px; }
    .video-pager { position: sticky; bottom: 0; display: flex; min-height: 45px; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 10px; border-top: 1px solid var(--line); background: var(--surface-raised); color: var(--text-muted); font-size: 10px; }
    .empty-state { display: grid; min-height: 120px; place-items: center; align-content: center; gap: 6px; padding: 16px; color: var(--text-muted); font-size: 10px; text-align: center; }
    .empty-state i { color: var(--accent); font-size: 18px; }
    .dry-run-panel { min-width: 0; overflow: visible; }
    .rename-paths { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 9px; padding: 10px; border-bottom: 1px solid var(--line-soft); }
    .rename-paths > div { display: grid; min-width: 0; gap: 3px; padding: 8px; border: 1px solid var(--line-soft); background: var(--page-deep); }
    .rename-paths span { color: var(--text-muted); font-size: 9px; font-weight: 700; text-transform: uppercase; }
    .rename-paths code { overflow-wrap: anywhere; font-size: 10px; }
    .plan-counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); margin: 0; border-bottom: 1px solid var(--line-soft); }
    .plan-counts > div { padding: 8px 10px; border-right: 1px solid var(--line-soft); }
    .plan-counts dt { color: var(--text-muted); font-size: 9px; text-transform: uppercase; }
    .plan-counts dd { margin: 2px 0 0; font-size: 14px; font-weight: 700; }
    .plan-message { margin: 10px 10px 0; padding: 9px 10px; border: 1px solid var(--line); background: var(--page-deep); font-size: 10px; }
    .plan-message--blocker { border-color: var(--danger); }
    .plan-message--warning { border-color: var(--warning); }
    .plan-message strong { display: flex; align-items: center; gap: 6px; }
    .plan-message ul { margin: 6px 0 0; padding-left: 18px; color: var(--text-soft); }
    .plan-inventory { max-height: min(430px, 55dvh); margin-top: 10px; overflow: auto; border-top: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); overscroll-behavior: contain; scrollbar-gutter: stable; }
    .plan-inventory__row { display: grid; grid-template-columns: minmax(95px, .25fr) minmax(180px, 1fr) minmax(100px, .4fr); gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--line-soft); align-items: start; }
    .plan-inventory__row:last-child { border-bottom: 0; }
    .plan-inventory__row code, .plan-inventory__row span { min-width: 0; overflow-wrap: anywhere; font-size: 9px; }
    .entry-kind { color: var(--accent-strong); font-weight: 700; text-transform: uppercase; }
    .apply-dock { position: sticky; bottom: 0; z-index: 4; display: grid; grid-template-columns: minmax(200px, 1fr) minmax(260px, .8fr) auto; align-items: center; gap: 10px; padding: 10px; border-top: 1px solid var(--line); background: var(--surface-raised); box-shadow: 0 -8px 18px rgb(25 30 24 / .25); }
    .apply-dock__copy { display: grid; gap: 2px; color: var(--text-muted); font-size: 10px; }
    .apply-dock__copy strong { color: var(--text); font-size: 11px; }
    .apply-dock__copy code { color: var(--accent-strong); overflow-wrap: anywhere; }
    .apply-dock input { width: 100%; padding: 6px 8px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .spin { animation: spin .9s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    @media (max-width: 1023px) {
        .channel-browser { top: calc(49px + env(safe-area-inset-top)); }
        .workspace-grid { grid-template-columns: minmax(0, 1fr); }
        .job-panel { grid-template-columns: minmax(170px, 1fr) minmax(150px, 1fr); }
        .job-panel__numbers { grid-column: 1 / -1; justify-content: space-between; }
        .apply-dock { grid-template-columns: minmax(190px, 1fr) minmax(220px, 1fr); }
        .apply-dock button { grid-column: 1 / -1; }
    }

    @media (max-width: 800px) {
        .availability-layout { grid-template-columns: minmax(0, 1fr); }
        .channel-browser { position: static; max-height: none; }
        .channel-list { max-height: min(320px, 46dvh); }
        .identity-actions { align-items: stretch; flex-direction: column; }
        .identity-actions > span { margin-left: 0; }
        .video-list { max-height: min(620px, 62dvh); }
    }

    @media (max-width: 600px) {
        .page-heading { align-items: stretch; flex-direction: column; gap: 8px; }
        .page-heading__status { white-space: normal; }
        .identity-heading { align-items: stretch; flex-direction: column; }
        .identity-heading .state-badge { width: fit-content; }
        .identity-facts { grid-template-columns: minmax(0, 1fr); }
        .identity-facts > div, .identity-facts > div:nth-child(3), .identity-facts > div:nth-child(6) { border-right: 0; }
        .identity-facts__path { grid-column: auto; }
        .eligibility-threshold__heading { align-items: flex-start; flex-direction: column; gap: 4px; }
        .eligibility-threshold__facts { grid-template-columns: minmax(0, 1fr); }
        .eligibility-threshold__facts > div, .eligibility-threshold__facts > div:nth-child(2n), .eligibility-threshold__facts > div:nth-last-child(-n + 2) { border-right: 0; border-bottom: 1px solid var(--line-soft); }
        .eligibility-threshold__facts > div:last-child { border-bottom: 0; }
        .job-panel { grid-template-columns: minmax(0, 1fr); }
        .job-panel__numbers { grid-column: auto; }
        .job-panel__numbers button { margin-left: auto; }
        .video-controls { grid-template-columns: minmax(0, 1fr); }
        .video-row { grid-template-columns: minmax(0, 1fr); padding: 10px; }
        .video-row__actions { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
        .video-row__actions time { text-align: left; }
        .video-row__actions a { width: 100%; min-height: 44px; }
        .video-pager { bottom: env(safe-area-inset-bottom); }
        .video-pager button { min-width: 44px; }
        .rename-paths { grid-template-columns: minmax(0, 1fr); }
        .rename-paths > i { transform: rotate(90deg); justify-self: center; }
        .plan-inventory { max-height: 52dvh; }
        .plan-inventory__row { grid-template-columns: minmax(0, 1fr); gap: 3px; padding: 9px 10px; }
        .apply-dock { grid-template-columns: minmax(0, 1fr); padding-bottom: max(10px, env(safe-area-inset-bottom)); }
        .apply-dock button { grid-column: auto; min-height: 48px; }
        .apply-dock input { min-height: 46px; }
    }

    @media (max-width: 390px) {
        .channel-row__top, .evidence-row__heading { align-items: flex-start; flex-direction: column; }
        .channel-row__meta { flex-direction: column; gap: 2px; }
        .identity-actions button { width: 100%; }
    }

    @media (prefers-reduced-motion: reduce) {
        .job-progress span { transition: none; }
    }
</style>
