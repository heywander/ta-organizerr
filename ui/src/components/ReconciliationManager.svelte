<script lang="ts">
    import { onDestroy, onMount, tick } from "svelte";

    type Candidate = {
        path: string;
        filename?: string;
        size_bytes?: number;
        duration_seconds?: number | null;
        streams?: unknown;
        valid?: boolean;
        error?: string | null;
        exact_video_id_match?: boolean;
        exact_id_match?: boolean;
        association?: string;
    };

    type ReconciliationEntry = {
        video_id: string;
        title?: string;
        channel?: string;
        link_path?: string;
        stored_target_path?: string;
        status: string;
        status_label?: string;
        detail?: string;
        candidates?: Candidate[];
        allowed_actions?: string[];
    };

    type InventoryResponse = {
        status?: string;
        generated_at?: string;
        summary?: Record<string, any>;
        entries?: ReconciliationEntry[];
    };

    type ScanProgress = {
        job_id?: string;
        status?: string;
        phase?: string;
        current?: number;
        total?: number;
        detail?: string;
        started_at?: string;
    };

    type StatusDefinition = {
        key: string;
        label: string;
        icon: string;
        tone: string;
    };

    const statuses: StatusDefinition[] = [
        { key: "healthy", label: "Healthy", icon: "bi-check-circle", tone: "healthy" },
        {
            key: "repair_available",
            label: "Repair available",
            icon: "bi-link-45deg",
            tone: "repair",
        },
        {
            key: "multiple_matches",
            label: "Manual review",
            icon: "bi-signpost-split",
            tone: "review",
        },
        {
            key: "invalid_media",
            label: "Invalid / corrupt",
            icon: "bi-file-earmark-x",
            tone: "invalid",
        },
        {
            key: "real_file",
            label: "Real file in link path",
            icon: "bi-file-earmark-play",
            tone: "real-file",
        },
        {
            key: "truly_missing",
            label: "Truly missing",
            icon: "bi-question-octagon",
            tone: "missing",
        },
    ];

    let loading = true;
    let loadError: string | null = null;
    let actionError: string | null = null;
    let successMessage: string | null = null;
    let inventoryStatus = "idle";
    let generatedAt = "";
    let scanToken = "";
    let scanTokenField: "scan_token" | "scan_id" = "scan_token";
    let summary: Record<string, any> = {};
    let entries: ReconciliationEntry[] = [];
    let query = "";
    let statusFilter = "all";
    let selectedCandidates: Record<string, string> = {};
    let busyEntryKey: string | null = null;
    let scanProgress: ScanProgress = {};
    let scanElapsedSeconds = 0;
    let inventoryPollTimer: ReturnType<typeof setTimeout> | null = null;
    let scanElapsedTimer: ReturnType<typeof setInterval> | null = null;
    let pendingRescanMessage = false;
    let inventoryAbortController: AbortController | null = null;

    let previewTarget: Candidate | null = null;
    let previewEntry: ReconciliationEntry | null = null;
    let previewMode: "video" | "audio" = "video";
    let previewUrl = "";
    let previewLoading = false;
    let previewError: string | null = null;
    let previewElement: HTMLMediaElement | null = null;
    let previewDialog: HTMLElement | null = null;
    let previewCloseButton: HTMLButtonElement | null = null;
    let previewReturnFocus: HTMLElement | null = null;
    let previewAbortController: AbortController | null = null;
    let previewSequence = 0;
    let previousBodyOverflow = "";

    function normalizeCandidate(raw: any): Candidate {
        return {
            path: raw?.path || raw?.filepath || raw?.source_path || "",
            filename: raw?.filename || raw?.name,
            size_bytes: raw?.size_bytes ?? raw?.size ?? 0,
            duration_seconds:
                raw?.duration_seconds ?? raw?.duration ?? null,
            streams: raw?.streams ?? raw?.media_streams,
            valid: Boolean(raw?.valid ?? raw?.valid_media ?? false),
            error: raw?.error || raw?.validation_error || null,
            exact_video_id_match:
                raw?.exact_video_id_match ?? raw?.exact_id_match,
            association: raw?.association,
        };
    }

    function normalizeEntry(raw: any): ReconciliationEntry {
        const record = raw?.db_record || {};
        const link = raw?.link || {};
        return {
            video_id:
                raw?.video_id ||
                record?.video_id ||
                record?.youtube_id ||
                record?.id ||
                "",
            title: raw?.title || record?.title,
            channel:
                raw?.channel ||
                record?.channel ||
                record?.channel_name,
            link_path:
                raw?.link_path || link?.path || link?.link_path || "",
            stored_target_path:
                raw?.stored_target_path ||
                link?.stored_target_path ||
                link?.target_path ||
                record?.stored_target_path ||
                "",
            status: raw?.status || "unknown",
            status_label: raw?.status_label,
            detail: raw?.detail || raw?.message,
            candidates: Array.isArray(raw?.candidates)
                ? raw.candidates.map(normalizeCandidate)
                : [],
            allowed_actions: Array.isArray(raw?.allowed_actions)
                ? raw.allowed_actions
                : [],
        };
    }

    function inventoryTimestamp(data: any): string {
        if (typeof data?.generated_at === "string") return data.generated_at;
        if (typeof data?.last_run === "string") return data.last_run;
        return (
            data?.last_run?.generated_at ||
            data?.last_run?.completed_at ||
            data?.last_run?.started_at ||
            ""
        );
    }

    function applyInventory(data: InventoryResponse | any) {
        const payload = data?.inventory || data;
        if (!Array.isArray(payload?.entries)) {
            throw new Error("The reconciliation inventory was not a list.");
        }
        inventoryStatus = payload.status || "ready";
        generatedAt = inventoryTimestamp(payload);
        if (data?.scan_token || payload?.scan_token) {
            scanToken = data?.scan_token || payload?.scan_token;
            scanTokenField = "scan_token";
        } else if (data?.scan_id || payload?.scan_id) {
            scanToken = data?.scan_id || payload?.scan_id;
            scanTokenField = "scan_id";
        }
        summary = payload.summary || {};
        entries = payload.entries.map(normalizeEntry);
        seedUnambiguousSelections(entries);
    }

    function messageFrom(error: unknown): string {
        return error instanceof Error ? error.message : String(error);
    }

    async function readResponse(response: Response): Promise<any> {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(
                body.error ||
                    body.message ||
                    `Request failed with status ${response.status}`,
            );
        }
        return body;
    }

    function normalize(value: string | undefined): string {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "_")
            .replace(/^_|_$/g, "");
    }

    function canonicalStatus(value: string | undefined): string {
        const status = normalize(value);
        if (status === "healthy") return "healthy";
        if (status.includes("multiple") || status.includes("ambiguous")) {
            return "multiple_matches";
        }
        if (status.includes("invalid") || status.includes("corrupt")) {
            return "invalid_media";
        }
        if (status.includes("real_file") || status.includes("not_symlink")) {
            return "real_file";
        }
        if (status.includes("truly_missing") || status === "missing") {
            return "truly_missing";
        }
        if (status.includes("repair") || status.includes("recoverable")) {
            return "repair_available";
        }
        return status || "unknown";
    }

    function entryKey(entry: ReconciliationEntry): string {
        return `${entry.video_id}::${entry.link_path || ""}`;
    }

    function candidateName(candidate: Candidate): string {
        if (candidate.filename) return candidate.filename;
        const path = candidate.path.replace(/\\/g, "/");
        return path.split("/").pop() || candidate.path;
    }

    function candidateHasExactId(
        candidate: Candidate,
        _entry: ReconciliationEntry,
    ): boolean {
        return candidate.exact_video_id_match === true;
    }

    function candidateIsUsable(
        candidate: Candidate,
        entry: ReconciliationEntry,
    ): boolean {
        return candidate.valid === true &&
            Number(candidate.size_bytes || 0) > 0 &&
            Boolean(candidate.path) &&
            candidateHasExactId(candidate, entry);
    }

    function candidateCanPreview(
        candidate: Candidate,
        entry: ReconciliationEntry,
    ): boolean {
        return candidate.valid === true &&
            Number(candidate.size_bytes || 0) > 0 &&
            Boolean(candidate.path) &&
            actionAllowed(entry, "preview_candidate");
    }

    function selectedCandidate(entry: ReconciliationEntry): Candidate | null {
        const selectedPath = selectedCandidates[entryKey(entry)];
        return (
            (entry.candidates || []).find(
                (candidate) => candidate.path === selectedPath,
            ) || null
        );
    }

    function chooseCandidate(entry: ReconciliationEntry, candidate: Candidate) {
        if (!candidateIsUsable(candidate, entry)) return;
        selectedCandidates = {
            ...selectedCandidates,
            [entryKey(entry)]: candidate.path,
        };
    }

    function seedUnambiguousSelections(nextEntries: ReconciliationEntry[]) {
        const next = { ...selectedCandidates };
        for (const entry of nextEntries) {
            const exactCandidates = (entry.candidates || []).filter((candidate) =>
                candidateHasExactId(candidate, entry),
            );
            const exactCandidatePathCount = new Set(
                exactCandidates.map((candidate) => candidate.path),
            ).size;
            const validCandidates = exactCandidates.filter((candidate) =>
                candidateIsUsable(candidate, entry),
            );
            if (
                actionAllowed(entry, "repair") &&
                exactCandidatePathCount === 1 &&
                validCandidates.length === 1
            ) {
                next[entryKey(entry)] = validCandidates[0].path;
            } else {
                delete next[entryKey(entry)];
            }
        }
        selectedCandidates = next;
    }

    function clearInventoryPoll() {
        if (inventoryPollTimer !== null) {
            clearTimeout(inventoryPollTimer);
            inventoryPollTimer = null;
        }
    }

    function startElapsedClock() {
        if (scanElapsedTimer !== null) return;
        scanElapsedSeconds = 0;
        scanElapsedTimer = setInterval(() => {
            scanElapsedSeconds += 1;
        }, 1000);
    }

    function stopElapsedClock() {
        if (scanElapsedTimer !== null) {
            clearInterval(scanElapsedTimer);
            scanElapsedTimer = null;
        }
    }

    function formatElapsed(seconds: number): string {
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        return `${minutes}m ${seconds % 60}s`;
    }

    function phaseLabel(phase = scanProgress.phase || scanProgress.status): string {
        const key = normalize(phase);
        const labels: Record<string, string> = {
            queued: "Scan queued",
            waiting: "Waiting for library access",
            waiting_for_library: "Waiting for library access",
            advanced_recovery: "Comparing TubeArchivist metadata",
            metadata: "Comparing TubeArchivist metadata",
            discovery: "Walking archive paths",
            walking: "Walking archive paths",
            probing: "Validating recovery candidates",
            classifying: "Classifying link state",
            finalizing: "Finalizing inventory",
            running: "Building link inventory",
            scanning: "Building link inventory",
        };
        return labels[key] || "Building link inventory";
    }

    function updateScanProgress(data: any) {
        const raw = data?.scan || data?.job || data?.progress || data || {};
        scanProgress = {
            job_id: raw.job_id || raw.id,
            status: raw.status || data?.status || "scanning",
            phase: raw.phase || data?.phase || raw.status || data?.status,
            current:
                raw.current ?? raw.completed ?? data?.current ?? data?.completed,
            total: raw.total ?? data?.total,
            detail: raw.detail || raw.message || data?.detail,
            started_at: raw.started_at || data?.started_at,
        };
    }

    function responseIsScanning(response: Response, data: any): boolean {
        const state = normalize(
            data?.scan?.status ||
                data?.job?.status ||
                data?.status,
        );
        if (!response.ok || state === "error" || state === "complete") return false;
        if (response.status === 202 || data?.scan_complete === false) return true;
        return ["queued", "waiting", "running", "scanning", "in_progress"].includes(
            state,
        );
    }

    function scheduleInventoryPoll() {
        clearInventoryPoll();
        const delay = scanElapsedSeconds < 30 ? 1000 : 2000;
        inventoryPollTimer = setTimeout(() => {
            inventoryPollTimer = null;
            void loadInventory(true, true);
        }, delay);
    }

    function acceptScanEnvelope(response: Response, data: any): boolean {
        if (!responseIsScanning(response, data)) return false;
        if (Array.isArray(data?.inventory?.entries)) {
            applyInventory(data);
        }
        // A displayed cached inventory is intentionally read-only until the
        // new filesystem snapshot completes.
        scanToken = "";
        updateScanProgress(data);
        inventoryStatus = phaseLabel();
        scheduleInventoryPoll();
        return true;
    }

    async function loadInventory(preserveMessage = false, isPoll = false) {
        if (!isPoll) clearInventoryPoll();
        loading = true;
        startElapsedClock();
        if (!isPoll) {
            loadError = null;
            actionError = null;
            if (!preserveMessage) successMessage = null;
        }
        let stillScanning = false;
        const controller = new AbortController();
        inventoryAbortController = controller;
        try {
            const endpoint = isPoll
                ? "/api/reconciliation/inventory?poll=1"
                : "/api/reconciliation/inventory";
            const response = await fetch(endpoint, {
                headers: { Accept: "application/json" },
                signal: controller.signal,
            });
            const data = await response.json().catch(() => ({}));
            stillScanning = acceptScanEnvelope(response, data);
            if (stillScanning) return;
            if (!response.ok) {
                if (Array.isArray(data?.inventory?.entries)) applyInventory(data);
                scanToken = "";
                throw new Error(
                    data.error ||
                        data.message ||
                        `Request failed with status ${response.status}`,
                );
            }
            applyInventory(data as InventoryResponse);
            scanProgress = {};
            loadError = null;
            if (pendingRescanMessage) {
                successMessage = "The filesystem inventory was rebuilt from current paths.";
                pendingRescanMessage = false;
            }
        } catch (error) {
            if ((error as Error)?.name === "AbortError") return;
            loadError = messageFrom(error);
            if (entries.length === 0) entries = [];
            inventoryStatus = "error";
            pendingRescanMessage = false;
        } finally {
            if (inventoryAbortController === controller) {
                inventoryAbortController = null;
            }
            if (!stillScanning) {
                loading = false;
                stopElapsedClock();
            }
        }
    }

    async function rescanInventory() {
        clearInventoryPoll();
        const previousScanToken = scanToken;
        const previousScanTokenField = scanTokenField;
        // The moment a refresh is requested, the displayed snapshot is stale
        // and must stay read-only even if the background job cannot start.
        scanToken = "";
        loading = true;
        startElapsedClock();
        loadError = null;
        actionError = null;
        successMessage = null;
        pendingRescanMessage = true;
        let stillScanning = false;
        const controller = new AbortController();
        inventoryAbortController = controller;
        try {
            const response = await fetch("/api/reconciliation/rescan", {
                method: "POST",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(
                    previousScanToken
                        ? { [previousScanTokenField]: previousScanToken }
                        : {},
                ),
                signal: controller.signal,
            });
            const data = await response.json().catch(() => ({}));
            stillScanning = acceptScanEnvelope(response, data);
            if (stillScanning) return;
            if (!response.ok) {
                throw new Error(
                    data.error ||
                        data.message ||
                        `Request failed with status ${response.status}`,
                );
            }
            applyInventory(data);
            scanProgress = {};
            pendingRescanMessage = false;
            successMessage = "The filesystem inventory was rebuilt from current paths.";
        } catch (error) {
            if ((error as Error)?.name === "AbortError") return;
            loadError = messageFrom(error);
            pendingRescanMessage = false;
        } finally {
            if (inventoryAbortController === controller) {
                inventoryAbortController = null;
            }
            if (!stillScanning) {
                loading = false;
                stopElapsedClock();
            }
        }
    }

    function statusCount(status: string): number {
        const direct = summary?.[status];
        const nested = summary?.statuses?.[status];
        const byStatus = summary?.by_status?.[status];
        const value =
            typeof direct === "number"
                ? direct
                : typeof nested === "number"
                  ? nested
                  : byStatus;
        if (typeof value === "number") return value;
        return entries.filter(
            (entry) => canonicalStatus(entry.status) === status,
        ).length;
    }

    function inventoryMetric(...keys: string[]): number | null {
        for (const key of keys) {
            const direct = summary?.[key];
            const nested = summary?.inventory?.[key];
            const inventoryCounts = summary?.inventory_counts?.[key];
            const value =
                typeof direct === "number"
                    ? direct
                    : typeof nested === "number"
                      ? nested
                      : inventoryCounts;
            if (typeof value === "number") return value;
        }
        return null;
    }

    function formatBytes(value: number | undefined): string {
        const bytes = Number(value || 0);
        if (!Number.isFinite(bytes) || bytes < 1) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const order = Math.min(
            Math.floor(Math.log(bytes) / Math.log(1024)),
            units.length - 1,
        );
        const amount = bytes / 1024 ** order;
        return `${amount.toFixed(order === 0 || amount >= 10 ? 0 : 1)} ${units[order]}`;
    }

    function formatDuration(value: number | null | undefined): string {
        const seconds = Number(value);
        if (!Number.isFinite(seconds) || seconds < 0) return "Duration unavailable";
        const total = Math.round(seconds);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const remainder = total % 60;
        return hours > 0
            ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
            : `${minutes}:${String(remainder).padStart(2, "0")}`;
    }

    function formatGeneratedAt(value: string): string {
        if (!value) return "Not yet scanned";
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    function formatStreams(streams: unknown): string {
        if (!streams) return "No stream details";
        if (Array.isArray(streams)) {
            const labels = streams.map((stream: any) => {
                if (typeof stream === "string") return stream;
                const kind = stream?.codec_type || stream?.type || "stream";
                const codec = stream?.codec_name || stream?.codec || "unknown codec";
                return `${kind}: ${codec}`;
            });
            return labels.length ? labels.join(" · ") : "No streams found";
        }
        if (typeof streams === "object") {
            return Object.entries(streams as Record<string, any>)
                .map(([key, value]) => `${key}: ${String(value)}`)
                .join(" · ");
        }
        return String(streams);
    }

    function actionAllowed(entry: ReconciliationEntry, action: string): boolean {
        const expected = normalize(action);
        return (entry.allowed_actions || []).some((candidate) => {
            const normalized = normalize(candidate);
            if (normalized === expected) return true;
            if (expected === "repair" && normalized === "repair_link") return true;
            if (expected === "adopt" && normalized.includes("adopt")) return true;
            if (
                expected === "preview_candidate" &&
                (normalized === "preview" || normalized === "preview_candidate")
            )
                return true;
            if (
                expected === "remove_broken_link" &&
                normalized.includes("remove_broken")
            )
                return true;
            if (expected === "forget_record" && normalized.includes("forget")) {
                return true;
            }
            return false;
        });
    }

    function confirmAction(
        action: string,
        entry: ReconciliationEntry,
        candidate: Candidate | null,
    ): boolean {
        const name = entry.title || entry.video_id;
        if (action === "repair") {
            return confirm(
                `Repair the broken link for “${name}” using this exact-ID file?\n\n${candidate?.path || "No candidate selected"}\n\nThe source media file will not be deleted.`,
            );
        }
        if (action === "adopt") {
            return confirm(
                `Adopt the existing real file for “${name}”?\n\n${entry.link_path || candidate?.path || "Path unavailable"}\n\nThe file will remain in place and will not be deleted.`,
            );
        }
        if (action === "remove_broken_link") {
            return confirm(
                `Remove only the broken symlink and corresponding stale database row for “${name}”?\n\nThis action must never delete a real video file.`,
            );
        }
        return confirm(
            `Forget the stale database record for “${name}”?\n\nFiles and symlinks will remain untouched.`,
        );
    }

    async function runAction(action: string, entry: ReconciliationEntry) {
        const candidate = selectedCandidate(entry);
        if (action === "repair" && !candidate) {
            actionError = "Select one valid exact-ID candidate before repairing the link.";
            return;
        }
        if (
            action === "repair" &&
            candidate &&
            !candidateIsUsable(candidate, entry)
        ) {
            actionError = "Automatic repair is limited to valid, non-empty exact-ID matches.";
            return;
        }
        if (!confirmAction(action, entry, candidate)) return;

        const endpoints: Record<string, string> = {
            repair: "/api/reconciliation/repair-link",
            adopt: "/api/reconciliation/adopt-real-file",
            remove_broken_link: "/api/reconciliation/remove-broken-link",
            forget_record: "/api/reconciliation/forget-stale-record",
        };
        busyEntryKey = entryKey(entry);
        actionError = null;
        successMessage = null;
        try {
            const response = await fetch(endpoints[action], {
                method: "POST",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    video_id: entry.video_id,
                    link_path: entry.link_path || null,
                    stored_target_path: entry.stored_target_path || null,
                    candidate_path:
                        candidate?.path ||
                        (action === "adopt" ? entry.link_path || null : null),
                    ...(scanToken
                        ? { [scanTokenField]: scanToken }
                        : {}),
                }),
            });
            const data = await readResponse(response);
            if (data.action_applied && data.rescan_error && !data.inventory) {
                // The mutation committed, so never leave a stale token or
                // controls on screen that could encourage an unsafe retry.
                scanToken = "";
                entries = [];
                summary = {};
                selectedCandidates = {};
                inventoryStatus = "refresh failed";
                loadError = `The action completed, but the inventory could not be refreshed: ${data.rescan_error}`;
            } else if (data.inventory || Array.isArray(data.entries)) {
                applyInventory(data);
            } else {
                await loadInventory(true);
                if (loadError) throw new Error(loadError);
            }
            successMessage =
                data.message ||
                "Action completed and was recorded. The inventory has been rescanned.";
        } catch (error) {
            actionError = messageFrom(error);
        } finally {
            busyEntryKey = null;
        }
    }

    function stopPreviewRequest() {
        previewSequence += 1;
        previewAbortController?.abort();
        previewAbortController = null;
    }

    function unloadPreview() {
        if (!previewElement) return;
        previewElement.pause();
        previewElement.removeAttribute("src");
        previewElement.load();
        previewElement = null;
    }

    function closePreview(restoreFocus = true) {
        const returnFocus = previewReturnFocus;
        stopPreviewRequest();
        unloadPreview();
        previewTarget = null;
        previewEntry = null;
        previewUrl = "";
        previewError = null;
        previewLoading = false;
        previewMode = "video";
        previewReturnFocus = null;
        document.body.style.overflow = previousBodyOverflow;
        if (restoreFocus && returnFocus?.isConnected) {
            void tick().then(() => returnFocus.focus());
        }
    }

    function openPreview(entry: ReconciliationEntry, candidate: Candidate) {
        if (!candidateCanPreview(candidate, entry)) return;
        previewReturnFocus =
            document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        previousBodyOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        previewEntry = entry;
        previewTarget = candidate;
        void startPreview("video");
        void tick().then(() => previewCloseButton?.focus());
    }

    async function startPreview(mode: "video" | "audio") {
        if (!previewTarget?.path) return;
        stopPreviewRequest();
        unloadPreview();
        previewMode = mode;
        previewUrl = "";
        previewError = null;
        previewLoading = true;

        const sequence = ++previewSequence;
        const controller = new AbortController();
        previewAbortController = controller;
        const endpoint =
            mode === "audio"
                ? "/api/recovery/preview-audio"
                : "/api/recovery/preview";
        const url = `${endpoint}?filepath=${encodeURIComponent(previewTarget.path)}`;
        try {
            const response = await fetch(url, {
                method: "HEAD",
                signal: controller.signal,
            });
            if (!response.ok) {
                const messages: Record<number, string> = {
                    401: "Your session expired. Sign in again before previewing media.",
                    403: "The server refused access to this candidate file.",
                    404: "The candidate file no longer exists.",
                    415: "This media type is not available for preview.",
                    422: "No readable audio stream was found.",
                    429: "The preview service is busy. Close another preview and try again.",
                    503: "The audio preview service is temporarily unavailable.",
                };
                throw new Error(
                    messages[response.status] ||
                        `Preview request failed with status ${response.status}.`,
                );
            }
            if (sequence !== previewSequence) return;
            previewUrl = url;
        } catch (error) {
            if ((error as Error)?.name === "AbortError") return;
            if (sequence !== previewSequence) return;
            previewLoading = false;
            previewError = messageFrom(error);
        } finally {
            if (sequence === previewSequence) previewAbortController = null;
        }
    }

    function handleMediaReady() {
        previewLoading = false;
        previewError = null;
    }

    function handleMediaError() {
        previewLoading = false;
        previewError =
            previewMode === "audio"
                ? "The audio stream could not be played. The candidate may not contain compatible audio."
                : "The browser could not play this video. Try Listen only to inspect its audio track.";
    }

    function trapPreviewFocus(event: KeyboardEvent) {
        if (!previewDialog) return;
        const focusable = Array.from(
            previewDialog.querySelectorAll<HTMLElement>(
                'button:not(:disabled), video[controls], audio[controls], [href], [tabindex]:not([tabindex="-1"])',
            ),
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (!previewDialog.contains(active)) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function handleWindowKeydown(event: KeyboardEvent) {
        if (!previewTarget) return;
        if (event.key === "Escape") {
            event.preventDefault();
            closePreview();
        } else if (event.key === "Tab") {
            trapPreviewFocus(event);
        }
    }

    onMount(() => {
        void loadInventory();
    });

    onDestroy(() => {
        clearInventoryPoll();
        stopElapsedClock();
        inventoryAbortController?.abort();
        inventoryAbortController = null;
        stopPreviewRequest();
        unloadPreview();
        if (previewTarget) document.body.style.overflow = previousBodyOverflow;
    });

    $: normalizedQuery = query.trim().toLowerCase();
    $: filteredEntries = entries.filter((entry) => {
        if (
            statusFilter !== "all" &&
            canonicalStatus(entry.status) !== statusFilter
        ) {
            return false;
        }
        if (!normalizedQuery) return true;
        return [
            entry.video_id,
            entry.title,
            entry.channel,
            entry.link_path,
            entry.stored_target_path,
            ...(entry.candidates || []).flatMap((candidate) => [
                candidate.filename,
                candidate.path,
            ]),
        ].some((value) => String(value || "").toLowerCase().includes(normalizedQuery));
    });

    $: inventoryMetrics = [
        {
            label: "Database records",
            value: inventoryMetric("tracked_database_entries", "tracked_records", "database_entries"),
        },
        {
            label: "Target / Hidden links",
            value: inventoryMetric("symlinks", "target_hidden_symlinks", "link_entries"),
        },
        {
            label: "Source files",
            value: inventoryMetric("source_files", "real_source_files"),
        },
        {
            label: "Unindexed files",
            value: inventoryMetric("unindexed", "unindexed_files"),
        },
    ].filter((metric) => metric.value !== null);
</script>

<svelte:window on:keydown={handleWindowKeydown} />

<div
    class="reconciliation-page"
    aria-hidden={previewTarget ? "true" : undefined}
    inert={previewTarget !== null}
>
    <header class="page-heading">
        <div>
            <h1><i class="bi bi-link-45deg" aria-hidden="true"></i> Link Reconciliation</h1>
            <p>Compare tracked records, organized links, source media, and exact-ID unindexed files.</p>
        </div>
        <div class="heading-actions">
            <span class="page-heading__status">
                <i class:spin={loading} class="bi {loading ? "bi-arrow-clockwise" : "bi-clock-history"}" aria-hidden="true"></i>
                {loading
                    ? `${phaseLabel()} · ${formatElapsed(scanElapsedSeconds)}`
                    : formatGeneratedAt(generatedAt)}
            </span>
            <button class="button" on:click={rescanInventory} disabled={loading || busyEntryKey !== null}>
                <i class:spin={loading} class="bi bi-arrow-clockwise" aria-hidden="true"></i>
                {loading ? "Scanning…" : "Rescan"}
            </button>
        </div>
    </header>

    <div class="rule-notice" role="note">
        <i class="bi bi-shield-check" aria-hidden="true"></i>
        <div>
            <strong>Exact video IDs are the repair boundary.</strong>
            <span>Filename resemblance is for manual review only. A valid exact-ID unindexed file is recoverable, not missing.</span>
        </div>
    </div>

    {#if loadError}
        <div class="notice notice--error status-message" role="alert">
            <span>
                <strong>Inventory unavailable.</strong> {loadError}
                {#if entries.length > 0} The previous snapshot is read-only until a scan succeeds.{/if}
            </span>
            <button class="button" on:click={rescanInventory}>Try again</button>
        </div>
    {/if}

    {#if actionError}
        <div class="notice notice--error status-message" role="alert">
            <span><strong>Action failed.</strong> {actionError}</span>
            <button class="button-quiet" on:click={() => (actionError = null)} aria-label="Dismiss action error">
                <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
        </div>
    {/if}

    {#if successMessage}
        <div class="notice success-message status-message" role="status">
            <span><strong>Inventory refreshed.</strong> {successMessage}</span>
            <button class="button-quiet" on:click={() => (successMessage = null)} aria-label="Dismiss success message">
                <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
        </div>
    {/if}

    {#if loading && entries.length > 0}
        <div class="notice scan-message status-message" role="status" aria-live="polite">
            <span>
                <strong>{phaseLabel()}.</strong>
                {scanProgress.detail || "The last completed inventory is shown read-only while the refresh runs."}
            </span>
            <span class="scan-message__elapsed">{formatElapsed(scanElapsedSeconds)}</span>
        </div>
    {/if}

    {#if inventoryMetrics.length > 0}
        <section class="inventory-strip" aria-label="Inventory sources">
            {#each inventoryMetrics as metric}
                <div>
                    <strong>{metric.value}</strong>
                    <span>{metric.label}</span>
                </div>
            {/each}
        </section>
    {/if}

    <section class="summary-grid" aria-label="Reconciliation status summary">
        {#each statuses as item}
            <button
                class="summary-card summary-card--{item.tone}"
                class:selected={statusFilter === item.key}
                aria-pressed={statusFilter === item.key}
                on:click={() =>
                    (statusFilter = statusFilter === item.key ? "all" : item.key)}
            >
                <i class="bi {item.icon}" aria-hidden="true"></i>
                <span>
                    <strong>{statusCount(item.key)}</strong>
                    <small>{item.label}</small>
                </span>
            </button>
        {/each}
    </section>

    <section class="panel inventory-panel" aria-labelledby="inventory-title">
        <div class="panel-header inventory-header">
            <div>
                <h2 class="section-title" id="inventory-title">Link inventory</h2>
                <p class="section-description">
                    {filteredEntries.length} of {entries.length} entries · {inventoryStatus}
                </p>
            </div>
            <div class="filters">
                <label class="search-field">
                    <span class="sr-only">Search reconciliation inventory</span>
                    <i class="bi bi-search" aria-hidden="true"></i>
                    <input bind:value={query} type="search" placeholder="ID, title, channel, or path" />
                </label>
                <label>
                    <span class="sr-only">Filter by status</span>
                    <select bind:value={statusFilter} aria-label="Filter inventory by status">
                        <option value="all">All statuses</option>
                        {#each statuses as item}
                            <option value={item.key}>{item.label}</option>
                        {/each}
                    </select>
                </label>
            </div>
        </div>

        {#if loading && entries.length === 0}
            <div class="loading-state" role="status">
                <i class="bi bi-arrow-clockwise spin" aria-hidden="true"></i>
                <strong>{phaseLabel()}…</strong>
                <span>
                    {scanProgress.detail || "Checking records, links, source media, and exact-ID recovery candidates."}
                    · {formatElapsed(scanElapsedSeconds)} elapsed
                </span>
                {#if Number(scanProgress.total || 0) > 0}
                    <div class="scan-progress" aria-label="Scan progress">
                        <progress
                            max={Number(scanProgress.total)}
                            value={Number(scanProgress.current || 0)}
                        ></progress>
                        <small>{Number(scanProgress.current || 0)} of {Number(scanProgress.total)} validated</small>
                    </div>
                {/if}
            </div>
        {:else if !loadError && filteredEntries.length === 0}
            <div class="empty-state">
                <i class="bi bi-folder-check" aria-hidden="true"></i>
                <strong>No matching entries</strong>
                <span>Clear the search or select another status.</span>
                {#if query || statusFilter !== "all"}
                    <button class="button" on:click={() => { query = ""; statusFilter = "all"; }}>Clear filters</button>
                {/if}
            </div>
        {:else}
            <div class="entry-list">
                {#each filteredEntries as entry (entryKey(entry))}
                    {@const statusKey = canonicalStatus(entry.status)}
                    {@const statusDefinition = statuses.find((item) => item.key === statusKey)}
                    {@const candidates = entry.candidates || []}
                    {@const currentCandidate = selectedCandidate(entry)}
                    {@const entryBusy = busyEntryKey === entryKey(entry)}
                    {@const mutationsBusy = busyEntryKey !== null || loading || !scanToken}
                    <article class="entry-card entry-card--{statusDefinition?.tone || "unknown"}">
                        <header class="entry-heading">
                            <div class="entry-title">
                                <span class="status-icon">
                                    <i class="bi {statusDefinition?.icon || "bi-info-circle"}" aria-hidden="true"></i>
                                </span>
                                <div>
                                    <h3>{entry.title || "Untitled archive entry"}</h3>
                                    <div class="entry-meta">
                                        <code>{entry.video_id || "No video ID"}</code>
                                        {#if entry.channel}<span>{entry.channel}</span>{/if}
                                    </div>
                                </div>
                            </div>
                            <span class="status-badge status-badge--{statusDefinition?.tone || "unknown"}">
                                {entry.status_label || statusDefinition?.label || entry.status}
                            </span>
                        </header>

                        {#if entry.detail}
                            <p class="entry-detail">{entry.detail}</p>
                        {/if}

                        <dl class="path-grid">
                            <div>
                                <dt>Organized link</dt>
                                <dd title={entry.link_path || "Unavailable"}>{entry.link_path || "Unavailable"}</dd>
                            </div>
                            <div>
                                <dt>Stored target</dt>
                                <dd title={entry.stored_target_path || "Unavailable"}>{entry.stored_target_path || "Unavailable"}</dd>
                            </div>
                        </dl>

                        {#if candidates.length > 0}
                            <section class="candidate-section" aria-label="Candidates for {entry.title || entry.video_id}">
                                <div class="candidate-heading">
                                    <strong>{candidates.length} media candidate{candidates.length === 1 ? "" : "s"}</strong>
                                    <span>Select only a validated exact-ID match.</span>
                                </div>
                                <div class="candidate-list">
                                    {#each candidates as candidate (candidate.path)}
                                        {@const exactMatch = candidateHasExactId(candidate, entry)}
                                        {@const usable = candidateIsUsable(candidate, entry)}
                                        {@const previewable = candidateCanPreview(candidate, entry)}
                                        <div class:invalid={!candidate.valid} class="candidate-row">
                                            <label class="candidate-select">
                                                <input
                                                    type="radio"
                                                    name="candidate-{entryKey(entry)}"
                                                    value={candidate.path}
                                                    checked={currentCandidate?.path === candidate.path}
                                                    disabled={!usable || !actionAllowed(entry, "repair") || mutationsBusy}
                                                    on:change={() => chooseCandidate(entry, candidate)}
                                                    aria-label="Select {candidateName(candidate)} for repair"
                                                />
                                                <span class="candidate-main">
                                                    <span class="candidate-name" title={candidate.path}>{candidateName(candidate)}</span>
                                                    <span class="candidate-facts">
                                                        <span class:exact-cue={exactMatch} class:manual-cue={!exactMatch}>
                                                            <i class="bi {exactMatch ? "bi-fingerprint" : "bi-eye"}" aria-hidden="true"></i>
                                                            {exactMatch
                                                                ? `Exact ID ${entry.video_id}`
                                                                : candidate.association === "expected_link_path"
                                                                  ? "Existing file at the expected link path"
                                                                  : "Manual review only"}
                                                        </span>
                                                        <span>{formatBytes(candidate.size_bytes)}</span>
                                                        <span>{formatDuration(candidate.duration_seconds)}</span>
                                                        <span>{formatStreams(candidate.streams)}</span>
                                                    </span>
                                                    <span class="candidate-path" title={candidate.path}>{candidate.path}</span>
                                                    {#if candidate.error}
                                                        <span class="candidate-error"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i> {candidate.error}</span>
                                                    {/if}
                                                </span>
                                            </label>
                                            <button
                                                class="button preview-button"
                                                on:click={() => openPreview(entry, candidate)}
                                                disabled={!previewable || mutationsBusy}
                                                title={!previewable ? "Preview is not available for this candidate" : "Preview candidate"}
                                            >
                                                <i class="bi bi-play-fill" aria-hidden="true"></i>
                                                Preview
                                            </button>
                                        </div>
                                    {/each}
                                </div>
                            </section>
                        {/if}

                        {#if (entry.allowed_actions || []).length > 0}
                            <footer class="entry-actions">
                                <span class="action-note">
                                    <i class="bi bi-journal-text" aria-hidden="true"></i>
                                    Every action is path-validated, logged, and followed by a rescan.
                                </span>
                                <div class="action-buttons">
                                    {#if actionAllowed(entry, "repair")}
                                        <button
                                            class="button-primary"
                                            on:click={() => runAction("repair", entry)}
                                            disabled={mutationsBusy || !currentCandidate}
                                        >
                                            <i class="bi bi-link-45deg" aria-hidden="true"></i>
                                            {entryBusy ? "Working…" : "Repair Link"}
                                        </button>
                                    {/if}
                                    {#if actionAllowed(entry, "adopt")}
                                        <button
                                            class="button-primary"
                                            on:click={() => runAction("adopt", entry)}
                                            disabled={mutationsBusy}
                                        >
                                            <i class="bi bi-file-earmark-check" aria-hidden="true"></i>
                                            {entryBusy ? "Working…" : "Adopt Existing Real File"}
                                        </button>
                                    {/if}
                                    {#if actionAllowed(entry, "remove_broken_link")}
                                        <button
                                            class="button-danger"
                                            on:click={() => runAction("remove_broken_link", entry)}
                                            disabled={mutationsBusy}
                                        >
                                            <i class="bi bi-link-45deg" aria-hidden="true"></i>
                                            {entryBusy ? "Working…" : "Remove Broken Link"}
                                        </button>
                                    {/if}
                                    {#if actionAllowed(entry, "forget_record")}
                                        <button
                                            class="button-danger"
                                            on:click={() => runAction("forget_record", entry)}
                                            disabled={mutationsBusy}
                                        >
                                            <i class="bi bi-database-x" aria-hidden="true"></i>
                                            {entryBusy ? "Working…" : "Forget Stale Record"}
                                        </button>
                                    {/if}
                                </div>
                            </footer>
                        {/if}
                    </article>
                {/each}
            </div>
        {/if}
    </section>
</div>

{#if previewTarget && previewEntry}
    <div class="preview-overlay" role="presentation">
        <section
            bind:this={previewDialog}
            class="preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="candidate-preview-title"
        >
            <header class="preview-header">
                <div>
                    <span class="preview-kicker">
                        <i class="bi {candidateHasExactId(previewTarget, previewEntry) ? "bi-fingerprint" : "bi-eye"}" aria-hidden="true"></i>
                        {candidateHasExactId(previewTarget, previewEntry) ? "Exact-ID candidate" : "Manual-review candidate"}
                    </span>
                    <h2 id="candidate-preview-title">{candidateName(previewTarget)}</h2>
                </div>
                <button
                    bind:this={previewCloseButton}
                    class="button-quiet preview-close"
                    on:click={() => closePreview()}
                    aria-label="Close candidate preview"
                >
                    <i class="bi bi-x-lg" aria-hidden="true"></i>
                </button>
            </header>

            <div class="preview-context">
                <span><strong>Video ID</strong> <code>{previewEntry.video_id}</code></span>
                <span><strong>Size</strong> {formatBytes(previewTarget.size_bytes)}</span>
                <span><strong>Duration</strong> {formatDuration(previewTarget.duration_seconds)}</span>
            </div>

            <div class="preview-mode" role="group" aria-label="Preview mode">
                <button
                    class:active={previewMode === "video"}
                    aria-pressed={previewMode === "video"}
                    on:click={() => startPreview("video")}
                >
                    <i class="bi bi-play-btn" aria-hidden="true"></i> Watch
                </button>
                <button
                    class:active={previewMode === "audio"}
                    aria-pressed={previewMode === "audio"}
                    on:click={() => startPreview("audio")}
                >
                    <i class="bi bi-headphones" aria-hidden="true"></i> Listen only
                </button>
            </div>

            <div class:audio-mode={previewMode === "audio"} class="media-frame">
                {#if previewLoading}
                    <div class="media-loading" role="status">
                        <i class="bi bi-arrow-clockwise spin" aria-hidden="true"></i>
                        Preparing {previewMode === "audio" ? "audio" : "video"} preview…
                    </div>
                {/if}
                {#if previewError}
                    <div class="media-error" role="alert">
                        <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
                        <span>{previewError}</span>
                    </div>
                {:else if previewUrl && previewMode === "video"}
                    <video
                        bind:this={previewElement}
                        src={previewUrl}
                        controls
                        playsinline
                        preload="metadata"
                        on:loadedmetadata={handleMediaReady}
                        on:canplay={handleMediaReady}
                        on:error={handleMediaError}
                    >
                        <track kind="captions" />
                    </video>
                {:else if previewUrl}
                    <div class="audio-player">
                        <i class="bi bi-soundwave" aria-hidden="true"></i>
                        <audio
                            bind:this={previewElement}
                            src={previewUrl}
                            controls
                            preload="metadata"
                            on:loadedmetadata={handleMediaReady}
                            on:canplay={handleMediaReady}
                            on:error={handleMediaError}
                        >
                            <track kind="captions" />
                        </audio>
                        <span>Listen from the beginning to identify this recovered channel.</span>
                    </div>
                {/if}
            </div>

            <footer class="preview-footer">
                <span title={previewTarget.path}>{previewTarget.path}</span>
                <button class="button" on:click={() => closePreview()}>Close preview</button>
            </footer>
        </section>
    </div>
{/if}

<style>
    .reconciliation-page {
        display: grid;
        gap: 12px;
    }

    .page-heading {
        display: flex;
        min-height: 62px;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 10px 12px;
        border: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .page-heading h1 {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0;
        color: var(--text);
        font-size: 15px;
        font-weight: 700;
    }

    .page-heading h1 i {
        color: var(--accent);
    }

    .page-heading p {
        margin: 3px 0 0;
        color: var(--text-muted);
        font-size: 11px;
    }

    .heading-actions,
    .page-heading__status {
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .page-heading__status {
        color: var(--text-muted);
        font-size: 10px;
        white-space: nowrap;
    }

    .rule-notice {
        display: grid;
        grid-template-columns: 30px minmax(0, 1fr);
        align-items: center;
        gap: 9px;
        padding: 9px 11px;
        border: 1px solid var(--accent);
        background: color-mix(in srgb, var(--surface-raised) 80%, var(--accent));
    }

    .rule-notice > i {
        color: var(--accent-strong);
        font-size: 18px;
        text-align: center;
    }

    .rule-notice div {
        display: grid;
        gap: 1px;
    }

    .rule-notice strong,
    .rule-notice span {
        font-size: 11px;
    }

    .rule-notice span {
        color: var(--text-muted);
    }

    .status-message {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
    }

    .success-message {
        border-color: var(--success);
    }

    .scan-message {
        border-color: var(--accent);
    }

    .scan-message__elapsed {
        flex: 0 0 auto;
        color: var(--text-muted);
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
    }

    .inventory-strip {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        border: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .inventory-strip div {
        display: flex;
        min-height: 38px;
        align-items: baseline;
        gap: 7px;
        padding: 8px 10px;
        border-right: 1px solid var(--line-soft);
    }

    .inventory-strip div:last-child {
        border-right: 0;
    }

    .inventory-strip strong {
        color: var(--accent-strong);
        font-size: 14px;
    }

    .inventory-strip span {
        color: var(--text-muted);
        font-size: 10px;
    }

    .summary-grid {
        display: grid;
        grid-template-columns: repeat(6, minmax(0, 1fr));
        border: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .summary-card {
        display: grid;
        grid-template-columns: 25px minmax(0, 1fr);
        min-height: 54px;
        align-items: center;
        gap: 7px;
        padding: 7px 8px;
        border: 0;
        border-right: 1px solid var(--line-soft);
        border-bottom: 2px solid transparent;
        background: var(--surface);
        color: var(--text);
        cursor: pointer;
        text-align: left;
    }

    .summary-card:last-child {
        border-right: 0;
    }

    .summary-card:hover,
    .summary-card.selected {
        background: var(--surface-hover);
    }

    .summary-card.selected {
        border-bottom-color: var(--accent);
    }

    .summary-card > i {
        color: var(--text-soft);
        font-size: 16px;
        text-align: center;
    }

    .summary-card--healthy > i,
    .entry-card--healthy .status-icon {
        color: var(--success);
    }

    .summary-card--repair > i,
    .entry-card--repair .status-icon {
        color: var(--accent-strong);
    }

    .summary-card--review > i,
    .summary-card--invalid > i,
    .entry-card--review .status-icon,
    .entry-card--invalid .status-icon {
        color: var(--warning);
    }

    .summary-card--missing > i,
    .entry-card--missing .status-icon {
        color: var(--danger);
    }

    .summary-card span {
        display: grid;
    }

    .summary-card strong {
        font-size: 14px;
        line-height: 1.1;
    }

    .summary-card small {
        overflow: hidden;
        color: var(--text-muted);
        font-size: 9px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .inventory-panel {
        min-width: 0;
    }

    .inventory-header {
        flex-wrap: wrap;
    }

    .filters {
        display: flex;
        align-items: center;
        gap: 7px;
    }

    .filters input {
        width: 230px;
        padding: 5px 8px 5px 29px;
    }

    .filters select {
        min-width: 145px;
        padding: 5px 8px;
    }

    .search-field {
        position: relative;
    }

    .search-field i {
        position: absolute;
        top: 50%;
        left: 9px;
        z-index: 1;
        color: var(--text-muted);
        transform: translateY(-50%);
        pointer-events: none;
    }

    .entry-list {
        display: grid;
    }

    .entry-card {
        min-width: 0;
        border-bottom: 1px solid var(--line);
        background: var(--surface);
    }

    .entry-card:last-child {
        border-bottom: 0;
    }

    .entry-heading {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        padding: 9px 10px;
        background: var(--surface-raised);
    }

    .entry-title {
        display: flex;
        min-width: 0;
        align-items: flex-start;
        gap: 8px;
    }

    .status-icon {
        display: grid;
        width: 27px;
        height: 27px;
        flex: 0 0 auto;
        place-items: center;
        border: 1px solid var(--line-soft);
        background: var(--page-deep);
    }

    .entry-title h3 {
        overflow: hidden;
        margin: 0;
        font-size: 12px;
        font-weight: 700;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .entry-meta {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin-top: 3px;
        color: var(--text-muted);
        font-size: 10px;
    }

    code {
        padding: 1px 4px;
        border: 1px solid var(--line-soft);
        background: var(--page-deep);
        color: var(--accent-strong);
        font-family: ui-monospace, "Cascadia Mono", monospace;
        font-size: 10px;
    }

    .status-badge {
        flex: 0 0 auto;
        padding: 3px 6px;
        border: 1px solid var(--line);
        background: var(--page-deep);
        color: var(--text-soft);
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }

    .status-badge--healthy {
        border-color: var(--success);
    }

    .status-badge--repair {
        border-color: var(--accent-strong);
    }

    .status-badge--review,
    .status-badge--invalid {
        border-color: var(--warning);
    }

    .status-badge--missing {
        border-color: var(--danger);
    }

    .entry-detail {
        margin: 0;
        padding: 7px 10px;
        border-top: 1px solid var(--line-soft);
        color: var(--text-soft);
        font-size: 11px;
    }

    .path-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        margin: 0;
        border-top: 1px solid var(--line-soft);
    }

    .path-grid div {
        min-width: 0;
        padding: 6px 10px;
        border-right: 1px solid var(--line-soft);
    }

    .path-grid div:last-child {
        border-right: 0;
    }

    .path-grid dt {
        color: var(--text-muted);
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    .path-grid dd {
        overflow: hidden;
        margin: 2px 0 0;
        color: var(--text-soft);
        font-family: ui-monospace, "Cascadia Mono", monospace;
        font-size: 10px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .candidate-section {
        border-top: 1px solid var(--line-soft);
        background: color-mix(in srgb, var(--surface) 84%, var(--page-deep));
    }

    .candidate-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 6px 10px;
        border-bottom: 1px solid var(--line-soft);
        color: var(--text-muted);
        font-size: 10px;
    }

    .candidate-heading strong {
        color: var(--text-soft);
    }

    .candidate-list {
        display: grid;
    }

    .candidate-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 9px;
        padding: 7px 10px;
        border-bottom: 1px solid var(--line-soft);
    }

    .candidate-row:last-child {
        border-bottom: 0;
    }

    .candidate-row:hover {
        background: var(--surface-hover);
    }

    .candidate-row.invalid {
        background: color-mix(in srgb, var(--surface) 90%, var(--danger));
    }

    .candidate-select {
        display: flex;
        min-width: 0;
        align-items: flex-start;
        gap: 8px;
    }

    .candidate-select input {
        margin-top: 3px;
        accent-color: var(--accent);
    }

    .candidate-main {
        display: grid;
        min-width: 0;
        gap: 2px;
    }

    .candidate-name {
        overflow: hidden;
        color: var(--text);
        font-size: 11px;
        font-weight: 700;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .candidate-facts {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
        color: var(--text-muted);
        font-size: 9px;
    }

    .candidate-facts > span:not(:last-child)::after {
        margin-left: 7px;
        color: var(--line);
        content: "|";
    }

    .exact-cue {
        color: var(--success);
        font-weight: 700;
    }

    .manual-cue,
    .candidate-error {
        color: #f0d4ce;
    }

    .candidate-path {
        overflow: hidden;
        color: var(--text-muted);
        font-family: ui-monospace, "Cascadia Mono", monospace;
        font-size: 9px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .candidate-error {
        font-size: 10px;
    }

    .preview-button {
        min-width: 82px;
    }

    .entry-actions {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        padding: 7px 10px;
        border-top: 1px solid var(--line-soft);
        background: var(--page-deep);
    }

    .action-note {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--text-muted);
        font-size: 9px;
    }

    .action-buttons {
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 6px;
    }

    .loading-state,
    .empty-state {
        display: grid;
        min-height: 190px;
        place-items: center;
        align-content: center;
        gap: 6px;
        padding: 24px;
        color: var(--text-muted);
        text-align: center;
    }

    .loading-state > i,
    .empty-state > i {
        color: var(--accent);
        font-size: 23px;
    }

    .loading-state strong,
    .empty-state strong {
        color: var(--text);
        font-size: 12px;
    }

    .loading-state span,
    .empty-state span {
        font-size: 10px;
    }

    .scan-progress {
        display: grid;
        width: min(340px, 100%);
        gap: 4px;
        margin-top: 5px;
    }

    .scan-progress progress {
        width: 100%;
        height: 7px;
        border: 0;
        background: var(--page-deep);
        accent-color: var(--accent);
    }

    .scan-progress small {
        color: var(--text-muted);
        font-size: 9px;
        font-variant-numeric: tabular-nums;
    }

    .preview-overlay {
        position: fixed;
        z-index: 100;
        inset: 0;
        display: grid;
        place-items: center;
        padding: 14px;
        background: rgb(28 34 27 / 0.88);
    }

    .preview-dialog {
        width: min(840px, 100%);
        max-height: calc(100dvh - 28px);
        overflow: auto;
        border: 1px solid var(--line);
        background: var(--surface);
    }

    .preview-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        padding: 9px 10px;
        border-bottom: 1px solid var(--line);
        background: var(--surface-raised);
    }

    .preview-header h2 {
        overflow: hidden;
        max-width: 72vw;
        margin: 2px 0 0;
        font-size: 13px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .preview-kicker {
        color: var(--success);
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    .preview-close {
        width: 30px;
        padding: 4px;
    }

    .preview-context {
        display: flex;
        flex-wrap: wrap;
        gap: 14px;
        padding: 7px 10px;
        border-bottom: 1px solid var(--line-soft);
        color: var(--text-muted);
        font-size: 10px;
    }

    .preview-context strong {
        margin-right: 4px;
        color: var(--text-soft);
    }

    .preview-mode {
        display: flex;
        border-bottom: 1px solid var(--line-soft);
        background: var(--page-deep);
    }

    .preview-mode button {
        min-height: 34px;
        padding: 6px 13px;
        border: 0;
        border-right: 1px solid var(--line-soft);
        border-bottom: 2px solid transparent;
        background: transparent;
        color: var(--text-muted);
        cursor: pointer;
        font-size: 11px;
    }

    .preview-mode button:hover,
    .preview-mode button.active {
        background: var(--surface-hover);
        color: var(--text);
    }

    .preview-mode button.active {
        border-bottom-color: var(--accent);
    }

    .media-frame {
        position: relative;
        display: grid;
        min-height: 380px;
        place-items: center;
        background: #20271f;
    }

    .media-frame.audio-mode {
        min-height: 180px;
    }

    .media-frame video {
        display: block;
        width: 100%;
        max-height: min(58dvh, 560px);
        background: #111;
    }

    .media-loading,
    .media-error {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 18px;
        color: var(--text-muted);
        font-size: 11px;
        text-align: center;
    }

    .media-error {
        color: #f0d4ce;
    }

    .audio-player {
        display: grid;
        width: min(600px, calc(100% - 28px));
        place-items: center;
        gap: 10px;
        padding: 18px;
        border: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .audio-player > i {
        color: var(--accent);
        font-size: 24px;
    }

    .audio-player audio {
        width: 100%;
    }

    .audio-player span {
        color: var(--text-muted);
        font-size: 10px;
    }

    .preview-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        padding: 7px 10px;
        border-top: 1px solid var(--line-soft);
        background: var(--surface-raised);
    }

    .preview-footer > span {
        overflow: hidden;
        color: var(--text-muted);
        font-family: ui-monospace, "Cascadia Mono", monospace;
        font-size: 9px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .spin {
        animation: spin 1s linear infinite;
    }

    .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
    }

    @keyframes spin {
        to {
            transform: rotate(360deg);
        }
    }

    @media (max-width: 900px) {
        .summary-grid {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }

        .summary-card:nth-child(3) {
            border-right: 0;
        }

        .summary-card:nth-child(-n + 3) {
            border-bottom: 1px solid var(--line-soft);
        }

        .inventory-strip {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .inventory-strip div:nth-child(2) {
            border-right: 0;
        }

        .inventory-strip div:nth-child(-n + 2) {
            border-bottom: 1px solid var(--line-soft);
        }

        .entry-actions {
            align-items: flex-start;
            flex-direction: column;
        }

        .action-buttons {
            justify-content: flex-start;
        }
    }

    @media (max-width: 640px) {
        .reconciliation-page :global(button),
        .preview-overlay :global(button) {
            min-width: 44px;
            min-height: 44px;
        }

        .reconciliation-page :global(input:not([type="checkbox"]):not([type="radio"])),
        .reconciliation-page :global(select) {
            min-height: 44px;
            font-size: 16px;
        }

        .page-heading {
            align-items: flex-start;
            flex-direction: column;
        }

        .heading-actions {
            width: 100%;
            justify-content: space-between;
        }

        .inventory-header {
            align-items: stretch;
            flex-direction: column;
        }

        .filters {
            display: grid;
            grid-template-columns: minmax(0, 1fr) 130px;
            width: 100%;
        }

        .filters input,
        .filters select,
        .search-field {
            width: 100%;
            min-width: 0;
        }

        .status-message {
            align-items: flex-start;
            flex-wrap: wrap;
        }

        .status-message > span:first-child {
            min-width: 0;
            flex: 1 1 220px;
            overflow-wrap: anywhere;
        }

        .status-message > .button {
            width: 100%;
        }

        .path-grid {
            grid-template-columns: 1fr;
        }

        .path-grid div {
            border-right: 0;
            border-bottom: 1px solid var(--line-soft);
        }

        .path-grid div:last-child {
            border-bottom: 0;
        }

        .candidate-heading {
            align-items: flex-start;
            flex-direction: column;
        }

        .candidate-row {
            grid-template-columns: minmax(0, 1fr);
        }

        .preview-button {
            width: 100%;
            justify-self: stretch;
        }

        .entry-actions,
        .action-buttons,
        .action-buttons > :global(button) {
            width: 100%;
        }

        .entry-heading {
            align-items: flex-start;
            flex-direction: column;
        }

        .entry-title h3,
        .path-grid dd,
        .candidate-name,
        .candidate-path {
            overflow: visible;
            text-overflow: clip;
            white-space: normal;
            overflow-wrap: anywhere;
        }

        .candidate-select input {
            width: 20px;
            height: 20px;
        }

        .status-badge {
            margin-left: 0;
        }

        .preview-overlay {
            align-items: stretch;
            padding:
                max(6px, env(safe-area-inset-top))
                max(6px, env(safe-area-inset-right))
                max(6px, env(safe-area-inset-bottom))
                max(6px, env(safe-area-inset-left));
        }

        .preview-dialog {
            width: 100%;
            max-height: 100%;
            margin: auto;
            overscroll-behavior: contain;
        }

        .preview-close {
            width: 44px;
            flex: 0 0 44px;
        }

        .preview-header h2 {
            max-width: calc(100vw - 88px);
        }

        .preview-mode button {
            min-height: 44px;
            flex: 1;
        }

        .media-frame {
            min-height: min(240px, 38dvh);
        }

        .preview-footer {
            align-items: stretch;
            flex-direction: column;
            padding-bottom: max(7px, env(safe-area-inset-bottom));
        }

        .preview-footer > span {
            overflow: visible;
            text-overflow: clip;
            white-space: normal;
            overflow-wrap: anywhere;
        }
    }

    @media (max-width: 760px) and (max-height: 600px) {
        .preview-overlay {
            place-items: stretch;
        }

        .preview-dialog {
            max-height: 100%;
        }

        .media-frame,
        .media-frame.audio-mode {
            min-height: 140px;
        }

        .audio-player {
            padding: 10px;
        }
    }

    @media (max-width: 420px) {
        .summary-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .summary-card:nth-child(3) {
            border-right: 1px solid var(--line-soft);
        }

        .summary-card:nth-child(2n) {
            border-right: 0;
        }

        .summary-card:nth-child(-n + 4) {
            border-bottom: 1px solid var(--line-soft);
        }

        .inventory-strip {
            grid-template-columns: 1fr;
        }

        .inventory-strip div,
        .inventory-strip div:nth-child(2) {
            border-right: 0;
            border-bottom: 1px solid var(--line-soft);
        }

        .inventory-strip div:last-child {
            border-bottom: 0;
        }

        .filters {
            grid-template-columns: 1fr;
        }

        .page-heading__status {
            white-space: normal;
        }
    }

    @media (prefers-reduced-motion: reduce) {
        .spin {
            animation: none;
        }
    }
</style>
