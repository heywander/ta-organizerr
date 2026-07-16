<script lang="ts">
    import { onDestroy, onMount, tick } from "svelte";

    type MediaStream = {
        type?: string;
        codec?: string;
        codec_name?: string;
        duration_seconds?: number | null;
    };

    type MediaReport = {
        path?: string;
        resolved_path?: string;
        enumerated_resolved_path?: string;
        filename?: string;
        size_bytes?: number;
        duration_seconds?: number | null;
        readable_duration_seconds?: number | null;
        format?: string;
        format_name?: string;
        streams?: MediaStream[];
        valid?: boolean;
        exact_video_id_match?: boolean;
        video_id?: string;
        sha256?: string;
        errors?: string[];
        warnings?: string[];
        message?: string;
        full_decode_checked?: boolean;
    };

    type Diagnosis = {
        source?: MediaReport;
        candidates?: MediaReport[];
        candidate_scan_complete?: boolean;
        candidate_scan_message?: string;
        findings?: string[];
        recommendation?: string;
        recommended_action?: string;
        decode_errors?: number;
        readable_percent?: number | null;
        expected_duration_seconds?: number | null;
        assessment?: string;
        input_sha256?: string;
        salvage_supported?: boolean;
        salvage_blocker?: string;
        salvage_blockers?: string[];
    };

    type SalvageJob = {
        id: string;
        status: string;
        operation: string;
        source_path?: string;
        candidate_path?: string;
        output_path?: string;
        output_duration_seconds?: number;
        expected_duration_seconds?: number;
        progress_percent?: number;
        processed_seconds?: number;
        duration_seconds?: number;
        phase?: string;
        message?: string;
        error?: string;
        started_at?: string;
        completed_at?: string;
        diagnosis?: Diagnosis;
        warnings?: string[];
    };

    type PreviewTarget = {
        path: string;
        label: string;
    };

    const terminalStatuses = new Set([
        "complete",
        "completed",
        "failed",
        "cancelled",
        "canceled",
    ]);

    let sourcePath = "";
    let candidatePath = "";
    let expectedDuration = "";
    let loadedCandidateKey = "";
    let candidateRequestSequence = 0;
    let diagnosis: Diagnosis | null = null;
    let deepCandidateSnapshot: MediaReport | null = null;
    let diagnosedSourcePath = "";
    let diagnosedCandidatePath = "";
    let diagnosedExpectedSeconds: number | null = null;
    let selectedCandidate = "";
    let candidateScanning = false;
    let candidateScanError: string | null = null;
    let jobs: SalvageJob[] = [];
    let loadingJobs = true;
    let submitting = false;
    let cancellingJob = "";
    let error: string | null = null;
    let notice: string | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let destroyed = false;
    let jobsRequest: AbortController | null = null;

    let previewTarget: PreviewTarget | null = null;
    let previewMode: "video" | "audio" = "video";
    let previewUrl = "";
    let previewLoading = false;
    let previewError: string | null = null;
    let previewElement: HTMLMediaElement | null = null;
    let previewRequest: AbortController | null = null;
    let previewSequence = 0;
    let previewCloseButton: HTMLButtonElement | null = null;
    let previewReturnFocus: HTMLElement | null = null;
    let previousBodyOverflow = "";

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

    function normalizeReport(raw: any): MediaReport {
        const decodeMessages = Array.isArray(raw?.decode?.streams)
            ? raw.decode.streams.flatMap((stream: any) =>
                  Array.isArray(stream?.messages) ? stream.messages : [],
              )
            : [];
        const errors = raw?.errors ?? raw?.decode_errors ?? decodeMessages;
        const warnings = raw?.warnings ?? [];
        const decodeStreams = Array.isArray(raw?.decode?.streams)
            ? raw.decode.streams
            : [];
        const inferredValid =
            decodeStreams.length > 0 &&
            decodeStreams.every((stream: any) => stream?.clean === true);
        return {
            ...raw,
            path: raw?.path || raw?.filepath || raw?.source_path || "",
            filename: raw?.filename || raw?.name,
            size_bytes: Number(raw?.size_bytes ?? raw?.size ?? 0),
            duration_seconds: numberOrNull(
                raw?.duration_seconds ??
                    raw?.declared_duration_seconds ??
                    raw?.duration,
            ),
            readable_duration_seconds: numberOrNull(
                raw?.readable_duration_seconds ??
                    raw?.readable_through_seconds ??
                    raw?.decode?.readable_through_seconds ??
                    raw?.decoded_seconds,
            ),
            format: raw?.format || raw?.format_name,
            streams: Array.isArray(raw?.streams) ? raw.streams : [],
            valid:
                raw?.valid !== undefined || raw?.valid_media !== undefined
                    ? Boolean(raw?.valid ?? raw?.valid_media)
                    : inferredValid,
            exact_video_id_match: Boolean(
                raw?.exact_video_id_match ?? raw?.exact_id_match ?? raw?.exact_id,
            ),
            errors: Array.isArray(errors)
                ? errors.map(String)
                : errors
                  ? [String(errors)]
                  : raw?.probe_error
                    ? [String(raw.probe_error)]
                    : [],
            warnings: Array.isArray(warnings)
                ? warnings.map(String)
                : warnings
                  ? [String(warnings)]
                  : [],
            full_decode_checked:
                raw?.full_decode_checked === true || decodeStreams.length > 0,
        };
    }

    function normalizeDiagnosis(raw: any): Diagnosis | null {
        if (!raw || typeof raw !== "object") return null;
        const sourceRaw = raw.source || raw.input || raw.media;
        const candidatesRaw =
            raw.candidates || raw.matches || (raw.candidate ? [raw.candidate] : []);
        const findingsRaw = [
            ...(Array.isArray(raw.findings) ? raw.findings : []),
            ...(Array.isArray(raw.issues) ? raw.issues : []),
            ...(Array.isArray(raw.warnings) ? raw.warnings : []),
            ...(Array.isArray(raw.limitations) ? raw.limitations : []),
            ...(Array.isArray(raw.salvage_blockers)
                ? raw.salvage_blockers
                : []),
            ...(raw.salvage_blocker ? [raw.salvage_blocker] : []),
        ];
        const assessmentCopy: Record<string, string> = {
            healthy:
                "The full decode check completed cleanly. Keep the original unless preview reveals a problem the scanner could not measure.",
            warning:
                "Review the reported warnings and compare the known duration before choosing a salvage operation.",
            damaged:
                "Preserve the original. Try remuxing surviving packets first, then recover decodable media if the remux remains incomplete.",
            inconclusive:
                "The tools could not establish completeness. Preserve the source and use preview plus a known expected duration for a human decision.",
        };
        return {
            ...raw,
            source: sourceRaw ? normalizeReport(sourceRaw) : undefined,
            candidates: Array.isArray(candidatesRaw)
                ? candidatesRaw.map((candidate: any) => ({
                      ...normalizeReport(candidate),
                      exact_video_id_match: true,
                  }))
                : [],
            findings: Array.isArray(findingsRaw)
                ? findingsRaw.map(String)
                : findingsRaw
                  ? [String(findingsRaw)]
                  : [],
            recommendation:
                raw.recommendation ||
                raw.recommended_action ||
                raw.message ||
                assessmentCopy[raw.assessment],
            readable_percent: numberOrNull(
                raw.readable_percent ?? raw.recoverable_percent,
            ),
            expected_duration_seconds: numberOrNull(
                raw.expected_duration_seconds ?? raw.expected_duration,
            ),
            assessment: raw.assessment,
        };
    }

    function normalizeJob(raw: any): SalvageJob {
        const diagnosis = normalizeDiagnosis(
            raw?.diagnosis ||
                raw?.report ||
                raw?.result?.diagnosis ||
                (raw?.kind === "diagnose" || raw?.operation === "diagnose"
                    ? raw?.result
                    : null),
        );
        if (diagnosis) {
            diagnosis.expected_duration_seconds = numberOrNull(
                raw?.expected_duration_seconds ?? raw?.expected_duration,
            );
        }
        return {
            id: String(raw?.job_id ?? raw?.id ?? ""),
            status: String(raw?.status || "unknown").toLowerCase(),
            operation: String(
                raw?.operation || raw?.mode || raw?.kind || "salvage",
            ).toLowerCase(),
            source_path:
                raw?.source_path || raw?.input_path || raw?.filepath || "",
            candidate_path: raw?.candidate_path || "",
            output_path:
                raw?.output_path ||
                raw?.result_path ||
                raw?.result?.output?.path ||
                "",
            output_duration_seconds: numberOrZero(
                raw?.output_duration_seconds ??
                    raw?.result_duration_seconds ??
                    raw?.result?.output_duration_seconds ??
                    raw?.result?.output?.duration_seconds ??
                    raw?.result?.output?.declared_duration_seconds,
            ),
            expected_duration_seconds: numberOrZero(
                raw?.expected_duration_seconds ?? raw?.expected_duration,
            ),
            progress_percent: clampPercent(
                raw?.progress_percent ?? raw?.progress ?? raw?.percent,
            ),
            processed_seconds: numberOrZero(
                raw?.processed_seconds ?? raw?.current_seconds,
            ),
            duration_seconds: numberOrZero(
                raw?.duration_seconds ??
                    raw?.total_seconds ??
                    raw?.result?.source?.declared_duration_seconds,
            ),
            phase: raw?.phase || raw?.stage || "",
            message: raw?.message || raw?.detail || "",
            error: raw?.error || "",
            started_at: raw?.started_at,
            completed_at: raw?.completed_at,
            diagnosis: diagnosis || undefined,
            warnings: [
                ...(Array.isArray(raw?.warnings) ? raw.warnings : []),
                ...(Array.isArray(raw?.result?.warnings)
                    ? raw.result.warnings
                    : []),
            ].map(String),
        };
    }

    function decodedCandidateSnapshot(report?: Diagnosis): MediaReport | null {
        return (
            report?.candidates?.find(
                (candidate) => candidate.full_decode_checked === true,
            ) || null
        );
    }

    function numberOrZero(value: unknown): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function numberOrNull(value: unknown): number | null {
        if (value === null || value === undefined || value === "") return null;
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function clampPercent(value: unknown): number {
        const parsed = Number(value);
        return Number.isFinite(parsed)
            ? Math.max(0, Math.min(100, parsed))
            : 0;
    }

    function basename(path: string): string {
        const bits = path.replace(/\\/g, "/").split("/");
        return bits[bits.length - 1] || path;
    }

    function invalidateDiagnosis() {
        diagnosis = null;
        diagnosedSourcePath = "";
        diagnosedCandidatePath = "";
        diagnosedExpectedSeconds = null;
        selectedCandidate = "";
        loadedCandidateKey = "";
        candidateRequestSequence += 1;
        candidateScanning = false;
        candidateScanError = null;
        deepCandidateSnapshot = null;
    }

    function updateSourcePath(value: string) {
        sourcePath = value;
        if (diagnosis && value.trim() !== diagnosedSourcePath) {
            invalidateDiagnosis();
        }
    }

    function updateCandidatePath(value: string) {
        candidatePath = value;
        if (diagnosis && value.trim() !== diagnosedCandidatePath) {
            invalidateDiagnosis();
        }
    }

    function updateExpectedDuration(value: string) {
        expectedDuration = value;
        if (
            diagnosis &&
            parseExpectedDuration(value) !== diagnosedExpectedSeconds
        ) {
            invalidateDiagnosis();
        }
    }

    function parseExpectedDuration(value: string): number | null {
        const input = value.trim();
        if (!input) return null;
        if (/^\d+(?:\.\d+)?$/.test(input)) {
            const seconds = Number(input);
            return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
        }
        const parts = input.split(":");
        if (parts.length < 2 || parts.length > 3 || parts.some((part) => !/^\d+$/.test(part))) {
            return null;
        }
        const numbers = parts.map(Number);
        const seconds =
            parts.length === 3
                ? numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
                : numbers[0] * 60 + numbers[1];
        return seconds > 0 && numbers.slice(1).every((part) => part < 60)
            ? seconds
            : null;
    }

    function formatBytes(value?: number): string {
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
        if (!seconds) return "Unknown duration";
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const remainder = seconds % 60;
        return hours
            ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
            : `${minutes}:${String(remainder).padStart(2, "0")}`;
    }

    function shortChecksum(value?: string): string {
        return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "";
    }

    function streamSummary(report?: MediaReport): string {
        if (!report?.streams?.length) return "Streams not reported";
        return report.streams
            .map((stream) => {
                const kind = stream.type || (stream as any).codec_type || "stream";
                return `${kind}: ${stream.codec || stream.codec_name || "unknown"}`;
            })
            .join(" · ");
    }

    function diagnosisFindings(report: Diagnosis): string[] {
        return [
            ...(report.findings || []),
            ...(report.source?.errors || []),
            ...(report.source?.warnings || []),
        ].filter((value, index, all) => Boolean(value) && all.indexOf(value) === index)
            .slice(0, 20);
    }

    function operationLabel(operation: string): string {
        if (operation.includes("diagn")) return "Deep inspection";
        if (operation.includes("remux")) return "Remux surviving packets";
        if (operation.includes("transcode") || operation.includes("reencode"))
            return "Recover decodable media";
        return "Media salvage";
    }

    function assessmentIcon(assessment?: string): string {
        if (assessment === "healthy") return "bi-file-earmark-check";
        if (assessment === "damaged") return "bi-file-earmark-excel";
        return "bi-file-earmark-medical";
    }

    function statusLabel(status: string): string {
        const labels: Record<string, string> = {
            queued: "Queued",
            running: "Running",
            complete: "Complete",
            completed: "Complete",
            failed: "Failed",
            cancelling: "Cancelling",
            cancelled: "Cancelled",
            canceled: "Cancelled",
        };
        return labels[status] || status;
    }

    function phaseLabel(phase?: string): string {
        if (!phase) return "Working…";
        const clean = phase.replaceAll("_", " ");
        return clean.charAt(0).toUpperCase() + clean.slice(1);
    }

    function isActive(job: SalvageJob): boolean {
        return !terminalStatuses.has(job.status);
    }

    function jobsFrom(data: any): SalvageJob[] {
        const raw = Array.isArray(data) ? data : data?.jobs || data?.items || [];
        return Array.isArray(raw)
            ? raw.map(normalizeJob).filter((job) => job.id)
            : [];
    }

    async function loadExactCandidates(videoId: string) {
        if (!videoId) return;
        const excludePath =
            diagnosis?.source?.path || diagnosedSourcePath || sourcePath.trim();
        const diagnosedHash =
            diagnosis?.input_sha256 || diagnosis?.source?.sha256 || "";
        const requestKey = `${videoId}\u0000${excludePath}\u0000${diagnosedHash}`;
        if (requestKey === loadedCandidateKey) return;
        const requestSequence = ++candidateRequestSequence;
        loadedCandidateKey = requestKey;
        candidateScanning = true;
        candidateScanError = null;
        try {
            const query = new URLSearchParams({ video_id: videoId });
            if (excludePath) query.set("exclude_path", excludePath);
            const response = await fetch(
                `/api/salvage/candidates?${query.toString()}`,
            );
            const data = await readResponse(response);
            const currentExcludePath =
                diagnosis?.source?.path || diagnosedSourcePath || sourcePath.trim();
            const currentDiagnosedHash =
                diagnosis?.input_sha256 || diagnosis?.source?.sha256 || "";
            if (
                destroyed ||
                requestSequence !== candidateRequestSequence ||
                loadedCandidateKey !== requestKey ||
                diagnosis?.source?.video_id !== videoId ||
                currentExcludePath !== excludePath ||
                currentDiagnosedHash !== diagnosedHash
            )
                return;
            diagnosis = {
                ...diagnosis,
                candidates: Array.isArray(data?.candidates)
                    ? data.candidates.map((candidate: any) =>
                          normalizeReport(candidate),
                      )
                    : [],
                candidate_scan_complete:
                    data?.scan_complete !== false && data?.truncated !== true,
                candidate_scan_message:
                    data?.scan_message || data?.message || "",
            };
        } catch (value) {
            // Candidate discovery is optional. Keep the completed source report
            // useful when candidate search is unavailable.
            console.warn("Exact-ID candidate search failed", value);
            if (
                requestSequence === candidateRequestSequence &&
                loadedCandidateKey === requestKey
            ) {
                loadedCandidateKey = "";
                candidateScanError = messageFrom(value);
            }
        } finally {
            if (
                requestSequence === candidateRequestSequence &&
                (loadedCandidateKey === requestKey || loadedCandidateKey === "")
            ) {
                candidateScanning = false;
            }
        }
    }

    function retryCandidateSearch() {
        const videoId = diagnosis?.source?.video_id;
        if (!videoId || candidateScanning) return;
        candidateRequestSequence += 1;
        loadedCandidateKey = "";
        candidateScanError = null;
        void loadExactCandidates(videoId);
    }

    function schedulePoll(delay?: number) {
        if (destroyed) return;
        if (pollTimer) clearTimeout(pollTimer);
        const hasActiveJob = jobs.some(isActive);
        pollTimer = setTimeout(
            () => void loadJobs(false),
            delay ?? (hasActiveJob ? 1200 : 6000),
        );
    }

    async function loadJobs(showLoading = true) {
        jobsRequest?.abort();
        const controller = new AbortController();
        jobsRequest = controller;
        if (showLoading) loadingJobs = true;
        try {
            const response = await fetch("/api/salvage/jobs", {
                signal: controller.signal,
            });
            const data = await readResponse(response);
            if (destroyed || controller.signal.aborted) return;
            jobs = jobsFrom(data);

            const newestDiagnosis = jobs.find(
                (job) => job.diagnosis && terminalStatuses.has(job.status),
            );
            if (newestDiagnosis?.diagnosis) {
                const previousDiagnosis = diagnosis;
                const previousHash =
                    previousDiagnosis?.input_sha256 ||
                    previousDiagnosis?.source?.sha256;
                const newestHash =
                    newestDiagnosis.diagnosis.input_sha256 ||
                    newestDiagnosis.diagnosis.source?.sha256;
                const sameDiagnosisSnapshot = Boolean(
                    previousDiagnosis?.source?.path &&
                        previousDiagnosis.source.path ===
                            newestDiagnosis.diagnosis.source?.path &&
                        previousHash &&
                        previousHash === newestHash,
                );
                const polledDiagnosis: Diagnosis = sameDiagnosisSnapshot
                    ? {
                          ...newestDiagnosis.diagnosis,
                          candidates:
                              previousDiagnosis?.candidates?.length
                                  ? previousDiagnosis.candidates
                                  :
                              newestDiagnosis.diagnosis.candidates,
                          candidate_scan_complete:
                              previousDiagnosis?.candidate_scan_complete ??
                              newestDiagnosis.diagnosis.candidate_scan_complete,
                          candidate_scan_message:
                              previousDiagnosis?.candidate_scan_message ||
                              newestDiagnosis.diagnosis.candidate_scan_message,
                      }
                    : newestDiagnosis.diagnosis;
                const reportPath =
                    newestDiagnosis.source_path ||
                    polledDiagnosis.source?.path ||
                    "";
                const reportCandidate = newestDiagnosis.candidate_path || "";
                const reportExpected =
                    newestDiagnosis.diagnosis.expected_duration_seconds ??
                    null;
                const adoptingPreviousReport = !sourcePath && Boolean(reportPath);
                if (adoptingPreviousReport) {
                    sourcePath = reportPath;
                    candidatePath = reportCandidate;
                    expectedDuration = reportExpected
                        ? String(reportExpected)
                        : "";
                }
                if (
                    reportPath &&
                    sourcePath.trim() === reportPath &&
                    candidatePath.trim() === reportCandidate &&
                    parseExpectedDuration(expectedDuration) === reportExpected
                ) {
                    diagnosis = polledDiagnosis;
                    deepCandidateSnapshot = decodedCandidateSnapshot(
                        newestDiagnosis.diagnosis,
                    );
                    diagnosedSourcePath = reportPath;
                    diagnosedCandidatePath = reportCandidate;
                    diagnosedExpectedSeconds = reportExpected;
                }
                if (diagnosis?.source?.video_id && diagnosedSourcePath === sourcePath.trim()) {
                    void loadExactCandidates(diagnosis.source.video_id);
                }
            }
        } catch (value) {
            if ((value as Error)?.name !== "AbortError") {
                error = `Could not load salvage jobs: ${messageFrom(value)}`;
            }
        } finally {
            if (jobsRequest === controller) jobsRequest = null;
            if (!destroyed) {
                loadingJobs = false;
                schedulePoll();
            }
        }
    }

    async function inspectSource() {
        const path = sourcePath.trim();
        if (!path || submitting) return;
        const expectedSeconds = parseExpectedDuration(expectedDuration);
        if (expectedDuration.trim() && expectedSeconds === null) {
            error = "Expected duration must be seconds, MM:SS, or HH:MM:SS.";
            return;
        }
        submitting = true;
        error = null;
        notice = null;
        diagnosis = null;
        diagnosedSourcePath = "";
        diagnosedCandidatePath = "";
        diagnosedExpectedSeconds = null;
        selectedCandidate = "";
        loadedCandidateKey = "";
        candidateRequestSequence += 1;
        candidateScanning = false;
        candidateScanError = null;
        deepCandidateSnapshot = null;
        try {
            const response = await fetch("/api/salvage/diagnose", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    source_path: path,
                    candidate_path: candidatePath.trim() || null,
                    expected_duration_seconds: expectedSeconds,
                }),
            });
            const data = await readResponse(response);
            const immediateDiagnosis = normalizeDiagnosis(
                data?.diagnosis || data?.report || data?.result,
            );
            if (immediateDiagnosis?.source) {
                diagnosis = immediateDiagnosis;
                deepCandidateSnapshot = decodedCandidateSnapshot(
                    immediateDiagnosis,
                );
                diagnosedSourcePath = path;
                diagnosedCandidatePath = candidatePath.trim();
                diagnosedExpectedSeconds = expectedSeconds;
            }
            if (immediateDiagnosis?.source?.video_id) {
                void loadExactCandidates(immediateDiagnosis.source.video_id);
            }
            const returnedJob = normalizeJob(data?.job || data);
            if (returnedJob.id) {
                jobs = [returnedJob, ...jobs.filter((job) => job.id !== returnedJob.id)];
            }
            notice =
                data.message ||
                (response.status === 202
                    ? "Deep inspection queued. The file is read sequentially; large or damaged media can take a while."
                    : "Inspection complete.");
            schedulePoll(400);
        } catch (value) {
            error = `Inspection could not start: ${messageFrom(value)}`;
        } finally {
            submitting = false;
        }
    }

    async function startSalvage(operation: "remux" | "reencode") {
        const path = sourcePath.trim();
        if (!path || submitting) return;
        const explanation =
            operation === "remux"
                ? "Create a new file from readable packets? This does not alter the original."
                : "Create a best-effort re-encode? Corrupt sections may be skipped, frozen, or silent. The original will not be altered.";
        if (!confirm(explanation)) return;

        submitting = true;
        error = null;
        notice = null;
        try {
            const response = await fetch("/api/salvage/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    source_path: path,
                    operation,
                    expected_duration_seconds:
                        diagnosis?.expected_duration_seconds ??
                        parseExpectedDuration(expectedDuration),
                    expected_source_sha256:
                        diagnosis?.input_sha256 || diagnosis?.source?.sha256,
                }),
            });
            const data = await readResponse(response);
            const job = normalizeJob(data?.job || data);
            if (job.id) {
                jobs = [job, ...jobs.filter((item) => item.id !== job.id)];
            }
            notice =
                data.message ||
                `${operationLabel(operation)} started. Output will be written separately from the archive source.`;
            schedulePoll(400);
        } catch (value) {
            error = `Salvage could not start: ${messageFrom(value)}`;
        } finally {
            submitting = false;
        }
    }

    async function cancelJob(job: SalvageJob) {
        if (!isActive(job) || cancellingJob) return;
        if (!confirm("Stop this job? Any incomplete output will not replace the source file."))
            return;
        cancellingJob = job.id;
        error = null;
        try {
            const response = await fetch(
                `/api/salvage/jobs/${encodeURIComponent(job.id)}/cancel`,
                { method: "POST" },
            );
            const data = await readResponse(response);
            const updated = normalizeJob(data?.job || data);
            jobs = jobs.map((item) =>
                item.id === job.id
                    ? updated.id
                        ? updated
                        : { ...item, status: "cancelling" }
                    : item,
            );
            notice = data.message || "Cancellation requested.";
            schedulePoll(300);
        } catch (value) {
            error = `Job could not be cancelled: ${messageFrom(value)}`;
        } finally {
            cancellingJob = "";
        }
    }

    function selectJob(job: SalvageJob) {
        invalidateDiagnosis();
        sourcePath = job.source_path || "";
        candidatePath = job.candidate_path || "";
        expectedDuration = job.expected_duration_seconds
            ? String(job.expected_duration_seconds)
            : "";
        if (job.diagnosis) {
            diagnosis = job.diagnosis;
            deepCandidateSnapshot = decodedCandidateSnapshot(job.diagnosis);
            diagnosedSourcePath = job.source_path || job.diagnosis.source?.path || "";
            diagnosedCandidatePath = job.candidate_path || "";
            diagnosedExpectedSeconds =
                job.diagnosis.expected_duration_seconds ?? null;
            expectedDuration = diagnosedExpectedSeconds
                ? String(diagnosedExpectedSeconds)
                : "";
            if (job.diagnosis.source?.video_id) {
                void loadExactCandidates(job.diagnosis.source.video_id);
            }
        }
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function closePreview(restoreFocus = true) {
        const returnFocus = previewReturnFocus;
        previewSequence += 1;
        previewRequest?.abort();
        previewRequest = null;
        if (previewElement) {
            previewElement.pause();
            previewElement.removeAttribute("src");
            previewElement.load();
            previewElement = null;
        }
        previewTarget = null;
        previewUrl = "";
        previewLoading = false;
        previewError = null;
        previewMode = "video";
        previewReturnFocus = null;
        document.body.style.overflow = previousBodyOverflow;
        if (restoreFocus && returnFocus?.isConnected) {
            void tick().then(() => returnFocus.focus());
        }
    }

    function openPreview(path: string, label?: string) {
        if (!path) return;
        closePreview(false);
        previewReturnFocus =
            document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        previousBodyOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        previewTarget = { path, label: label || basename(path) };
        void startPreview("video");
        void tick().then(() => previewCloseButton?.focus());
    }

    async function startPreview(mode: "video" | "audio") {
        if (!previewTarget) return;
        previewSequence += 1;
        const sequence = previewSequence;
        previewRequest?.abort();
        if (previewElement) {
            previewElement.pause();
            previewElement.removeAttribute("src");
            previewElement.load();
            previewElement = null;
        }
        previewMode = mode;
        previewUrl = "";
        previewError = null;
        previewLoading = true;
        const controller = new AbortController();
        previewRequest = controller;
        const endpoint =
            mode === "audio"
                ? "/api/salvage/preview/audio"
                : "/api/salvage/preview";
        const url = `${endpoint}?filepath=${encodeURIComponent(previewTarget.path)}`;
        try {
            const response = await fetch(url, {
                method: "HEAD",
                signal: controller.signal,
            });
            if (!response.ok) {
                const messages: Record<number, string> = {
                    403: "The server refused this path.",
                    404: "The file no longer exists.",
                    415: "This stream cannot be previewed in the browser.",
                    422: "No readable audio stream was found.",
                    429: "Another preview is already being prepared.",
                    503: "The preview helper is temporarily unavailable.",
                };
                throw new Error(
                    messages[response.status] ||
                        `Preview failed with status ${response.status}.`,
                );
            }
            if (sequence !== previewSequence) return;
            previewUrl = url;
        } catch (value) {
            if ((value as Error)?.name === "AbortError") return;
            if (sequence !== previewSequence) return;
            previewError = messageFrom(value);
            previewLoading = false;
        } finally {
            if (sequence === previewSequence) previewRequest = null;
        }
    }

    function handlePreviewReady() {
        previewLoading = false;
    }

    function handleKeydown(event: KeyboardEvent) {
        if (event.key === "Escape" && previewTarget) closePreview();
    }

    onMount(() => {
        window.addEventListener("keydown", handleKeydown);
        const params = new URLSearchParams(window.location.search);
        sourcePath = params.get("path") || sourcePath;
        candidatePath = params.get("candidate") || candidatePath;
        expectedDuration = params.get("expected") || expectedDuration;
        void loadJobs();
    });

    onDestroy(() => {
        destroyed = true;
        candidateRequestSequence += 1;
        if (pollTimer) clearTimeout(pollTimer);
        jobsRequest?.abort();
        window.removeEventListener("keydown", handleKeydown);
        closePreview(false);
    });

    $: exactCandidates = (diagnosis?.candidates || []).filter(
        (candidate) =>
            candidate.exact_video_id_match &&
            candidate.path &&
            !candidate.full_decode_checked,
    );
    $: selectedCandidateReport = exactCandidates.find(
        (candidate) => candidate.path === selectedCandidate,
    );
    $: activeJobs = jobs.filter(isActive);
    $: expectedSeconds =
        diagnosis?.expected_duration_seconds ??
        parseExpectedDuration(expectedDuration);
    $: diagnosisMatchesSource = Boolean(
        diagnosis?.source &&
            (diagnosis.input_sha256 || diagnosis.source.sha256) &&
            diagnosedSourcePath &&
            sourcePath.trim() === diagnosedSourcePath &&
            candidatePath.trim() === diagnosedCandidatePath &&
            parseExpectedDuration(expectedDuration) === diagnosedExpectedSeconds,
    );
    $: canStartSalvage = Boolean(
        diagnosisMatchesSource && diagnosis?.salvage_supported !== false,
    );
</script>

<div class="salvage-page">
    <section class="panel salvage-brief" aria-labelledby="salvage-heading">
        <div class="salvage-brief__heading">
            <span class="salvage-brief__mark" aria-hidden="true">
                <i class="bi bi-life-preserver"></i>
            </span>
            <div>
                <h1 id="salvage-heading" class="section-title">Media salvage</h1>
                <p class="section-description">
                    Inspect damaged media and write the readable portions to a separate recovery file.
                </p>
            </div>
        </div>
        <div class="salvage-brief__rules" aria-label="Safety rules">
            <span><i class="bi bi-shield-check"></i> Original preserved</span>
            <span><i class="bi bi-folder2-open"></i> Separate output</span>
            <span><i class="bi bi-fingerprint"></i> Candidates compared only</span>
        </div>
    </section>

    <div class="notice salvage-reality" role="note">
        <i class="bi bi-info-circle" aria-hidden="true"></i>
        <div>
            <strong>Salvage is not reconstruction.</strong>
            Remuxing can rebuild a damaged container or index when packets remain readable. Recovery can retain frames and audio that still decode, but bytes lost to corruption cannot be recreated. Exact-ID candidates are compared as alternate copies; they are never mixed into the damaged source automatically.
        </div>
    </div>

    {#if error}
        <div class="notice notice--error" role="alert">
            <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
            <span>{error}</span>
            <button class="button-quiet notice-dismiss" on:click={() => (error = null)} aria-label="Dismiss error">
                <i class="bi bi-x-lg"></i>
            </button>
        </div>
    {/if}

    {#if notice}
        <div class="notice salvage-notice" role="status">
            <i class="bi bi-check2-circle" aria-hidden="true"></i>
            <span>{notice}</span>
            <button class="button-quiet notice-dismiss" on:click={() => (notice = null)} aria-label="Dismiss notice">
                <i class="bi bi-x-lg"></i>
            </button>
        </div>
    {/if}

    <div class="salvage-layout">
        <main class="salvage-workspace">
            <section class="panel" aria-labelledby="source-heading">
                <header class="section-header">
                    <div>
                        <h2 id="source-heading" class="section-title">
                            <span class="step-number">1</span>
                            Select damaged source
                        </h2>
                        <p class="section-description">Paths must be inside a configured source, organized, import, or salvage media root.</p>
                    </div>
                    {#if sourcePath.trim()}
                        <button class="button" on:click={() => openPreview(sourcePath.trim(), "Source preview")}>
                            <i class="bi bi-play-btn"></i> Preview source
                        </button>
                    {/if}
                </header>
                <div class="source-form">
                    <label for="salvage-source">Source media path</label>
                    <div class="path-row">
                        <input
                            id="salvage-source"
                            value={sourcePath}
                            on:input={(event) => updateSourcePath(event.currentTarget.value)}
                            placeholder="/app/source/CHANNEL_ID/VIDEO_ID.mp4"
                            autocomplete="off"
                            spellcheck="false"
                            disabled={submitting}
                        />
                        <button class="button-primary" on:click={inspectSource} disabled={!sourcePath.trim() || submitting}>
                            <i class={`bi ${submitting ? "bi-arrow-repeat spin" : "bi-activity"}`}></i>
                            {submitting ? "Submitting…" : "Inspect file"}
                        </button>
                    </div>
                    <details class="candidate-entry">
                        <summary>Specify a known exact-ID candidate</summary>
                        <label for="salvage-candidate">Candidate media path <span>optional</span></label>
                        <input
                            id="salvage-candidate"
                            value={candidatePath}
                            on:input={(event) => updateCandidatePath(event.currentTarget.value)}
                            placeholder="/app/source/CHANNEL_ID/VIDEO_ID.ext"
                            autocomplete="off"
                            spellcheck="false"
                            disabled={submitting}
                        />
                        <p>Filename resemblance is not enough. The server verifies the exact video ID before comparing a candidate.</p>
                    </details>
                    <div class="expected-entry">
                        <label for="salvage-expected">Expected full duration <span>optional</span></label>
                        <input
                            id="salvage-expected"
                            value={expectedDuration}
                            on:input={(event) => updateExpectedDuration(event.currentTarget.value)}
                            placeholder="10:00 or 600"
                            autocomplete="off"
                            disabled={submitting}
                        />
                        <p>Use this when a ten-minute file only plays for four minutes. It gives the report a known completeness baseline.</p>
                    </div>
                </div>
            </section>

            <section class="panel diagnosis-panel" aria-labelledby="diagnosis-heading">
                <header class="section-header">
                    <div>
                        <h2 id="diagnosis-heading" class="section-title">
                            <span class="step-number">2</span>
                            Diagnosis
                        </h2>
                        <p class="section-description">Container metadata plus a sequential decode check.</p>
                    </div>
                </header>

                {#if diagnosis?.source && diagnosisMatchesSource}
                    <div class="diagnosis-body">
                        <article class="media-report">
                            <div class="media-report__title">
                                <i class={`bi ${assessmentIcon(diagnosis.assessment)}`}></i>
                                <div>
                                    <strong>{diagnosis.source.filename || basename(diagnosis.source.path || sourcePath)}</strong>
                                    <span>{diagnosis.source.video_id || "Video ID unavailable"}{diagnosis.assessment ? ` · ${diagnosis.assessment}` : ""}</span>
                                </div>
                            </div>
                            <dl class="report-facts">
                                <div><dt>File size</dt><dd>{formatBytes(diagnosis.source.size_bytes)}</dd></div>
                                <div><dt>Reported length</dt><dd>{formatDuration(diagnosis.source.duration_seconds)}</dd></div>
                                <div><dt>Readable</dt><dd>{formatDuration(diagnosis.source.readable_duration_seconds)}</dd></div>
                                <div><dt>Expected full length</dt><dd>{formatDuration(expectedSeconds)}</dd></div>
                                <div><dt>Streams</dt><dd>{streamSummary(diagnosis.source)}</dd></div>
                                <div><dt>SHA-256 baseline</dt><dd title={diagnosis.input_sha256 || diagnosis.source.sha256}>{shortChecksum(diagnosis.input_sha256 || diagnosis.source.sha256)}</dd></div>
                            </dl>

                            {#if diagnosisFindings(diagnosis).length}
                                <div class="finding-list">
                                    {#each diagnosisFindings(diagnosis) as finding}
                                        <div><i class="bi bi-dash-square"></i><span>{finding}</span></div>
                                    {/each}
                                </div>
                            {/if}

                            {#if diagnosis.recommendation}
                                <div class="recommendation">
                                    <span>Recommendation</span>
                                    <strong>{diagnosis.recommendation}</strong>
                                </div>
                            {/if}
                        </article>

                        <div class="candidate-section">
                            <div class="candidate-section__heading">
                                <div>
                                    <strong>Exact-ID comparison</strong>
                                    <span>{exactCandidates.length} currently probed exact-ID {exactCandidates.length === 1 ? "copy" : "copies"}</span>
                                </div>
                                <div class="candidate-section__actions">
                                    <button class="button-quiet" on:click={retryCandidateSearch} disabled={candidateScanning || !diagnosis.source.video_id}>
                                        <i class={`bi bi-arrow-clockwise ${candidateScanning ? "spin" : ""}`}></i> Rescan copies
                                    </button>
                                    {#if selectedCandidateReport?.path}
                                        <button class="button" on:click={() => openPreview(selectedCandidateReport.path || "", "Candidate preview")}>
                                            <i class="bi bi-play-btn"></i> Preview selected
                                        </button>
                                    {/if}
                                </div>
                            </div>
                            {#if deepCandidateSnapshot}
                                <article class="candidate-snapshot">
                                    <div class="candidate-snapshot__heading">
                                        <div>
                                            <i class="bi bi-shield-check"></i>
                                            <span>
                                                <strong>{deepCandidateSnapshot.filename || basename(deepCandidateSnapshot.path || "Candidate")}</strong>
                                                <small>Full-decode diagnosis snapshot · exact video ID</small>
                                            </span>
                                        </div>
                                        {#if deepCandidateSnapshot.path}
                                            <button class="button" on:click={() => openPreview(deepCandidateSnapshot?.path || "", "Current candidate path") }>
                                                <i class="bi bi-play-btn"></i> Preview current path
                                            </button>
                                        {/if}
                                    </div>
                                    <dl class="candidate-snapshot__facts">
                                        <div><dt>Reported</dt><dd>{formatDuration(deepCandidateSnapshot.duration_seconds)}</dd></div>
                                        <div><dt>Readable</dt><dd>{formatDuration(deepCandidateSnapshot.readable_duration_seconds)}</dd></div>
                                        <div><dt>Size</dt><dd>{formatBytes(deepCandidateSnapshot.size_bytes)}</dd></div>
                                        <div><dt>SHA-256</dt><dd>{shortChecksum(deepCandidateSnapshot.sha256)}</dd></div>
                                    </dl>
                                    <code>{deepCandidateSnapshot.path}</code>
                                    {#if deepCandidateSnapshot.warnings?.length || deepCandidateSnapshot.errors?.length}
                                        <p class="candidate-snapshot__warning">{[...(deepCandidateSnapshot.warnings || []), ...(deepCandidateSnapshot.errors || [])].slice(0, 3).join(" · ")}</p>
                                    {/if}
                                    <p>This evidence describes the file descriptor read during diagnosis. Preview reopens the current path; inspect again before relying on it if that path may have changed.</p>
                                </article>
                            {/if}
                            {#if candidateScanning}
                                <div class="candidate-guidance candidate-guidance--neutral" role="status">
                                    <i class="bi bi-arrow-repeat spin"></i>
                                    <span>Searching the bounded media inventory for alternate exact-ID files…</span>
                                </div>
                            {:else if candidateScanError}
                                <div class="candidate-guidance" role="alert">
                                    <i class="bi bi-exclamation-triangle"></i>
                                    <span>Exact-ID candidate search failed: {candidateScanError}. Source-only diagnosis and salvage remain available; retry the inventory search before concluding that no alternate exists.</span>
                                </div>
                            {:else if diagnosis.candidate_scan_complete === false && exactCandidates.length}
                                <div class="candidate-guidance">
                                    <i class="bi bi-hourglass-split"></i>
                                    <span>{diagnosis.candidate_scan_message || "The bounded search ended before every location was checked, so more exact-ID copies may exist."}</span>
                                </div>
                            {/if}
                            {#if exactCandidates.length}
                                <div class="candidate-list">
                                    <div class="candidate-guidance">
                                        <i class="bi bi-signpost-split"></i>
                                        <span>A fully decoded exact-ID copy is usually better evidence than a salvage output. Selection here only changes the comparison preview; it does not patch or mix streams.</span>
                                    </div>
                                    <label class:selected={!selectedCandidate}>
                                        <input type="radio" name="salvage-candidate-choice" bind:group={selectedCandidate} value="" />
                                        <span class="candidate-radio"></span>
                                        <span class="candidate-copy"><strong>Damaged source</strong><small>Compare against the source alone</small></span>
                                    </label>
                                    {#each exactCandidates as candidate}
                                        <label class:selected={selectedCandidate === candidate.path}>
                                            <input type="radio" name="salvage-candidate-choice" bind:group={selectedCandidate} value={candidate.path} />
                                            <span class="candidate-radio"></span>
                                            <span class="candidate-copy">
                                                <strong>{candidate.filename || basename(candidate.path || "Candidate")}</strong>
                                                <small>{candidate.full_decode_checked ? "Full decode checked" : candidate.valid ? "Probe valid" : "Probe warning"} · {formatBytes(candidate.size_bytes)} · reported {formatDuration(candidate.duration_seconds)}{candidate.full_decode_checked ? ` · readable ${formatDuration(candidate.readable_duration_seconds)}` : ""}</small>
                                                <small>{streamSummary(candidate)}</small>
                                                {#if candidate.full_decode_checked && candidate.sha256}
                                                    <small class="candidate-checksum">SHA-256 {shortChecksum(candidate.sha256)}</small>
                                                {/if}
                                                {#if candidate.full_decode_checked && (candidate.warnings?.length || candidate.errors?.length)}
                                                    <small class="candidate-warning">{[...(candidate.warnings || []), ...(candidate.errors || [])].slice(0, 2).join(" · ")}</small>
                                                {/if}
                                                <code>{candidate.path}</code>
                                            </span>
                                            <span class="candidate-badge"><i class={`bi ${candidate.full_decode_checked ? "bi-shield-check" : "bi-fingerprint"}`}></i> {candidate.full_decode_checked ? "Decode checked" : "Exact ID"}</span>
                                        </label>
                                    {/each}
                                </div>
                            {:else if !candidateScanning && !candidateScanError}
                                <div class="empty-state compact">
                                    <i class="bi bi-files"></i>
                                    {#if diagnosis.candidate_scan_complete === false}
                                        <span>The bounded candidate search ended before every location was checked. Provide a known exact-ID path above or retry; source-only salvage remains available.</span>
                                    {:else}
                                        <span>No alternate exact-ID file was found in the scanned locations. Source-only salvage is still available.</span>
                                    {/if}
                                </div>
                            {/if}
                        </div>
                    </div>
                {:else}
                    <div class="empty-state diagnosis-empty">
                        <i class="bi bi-activity"></i>
                        <strong>No diagnosis yet</strong>
                        <span>Inspecting reads the complete source without modifying it. Progress appears in the job queue.</span>
                    </div>
                {/if}
            </section>

            <section class="panel" aria-labelledby="operation-heading">
                <header class="section-header">
                    <div>
                        <h2 id="operation-heading" class="section-title">
                            <span class="step-number">3</span>
                            Create salvage output
                        </h2>
                        <p class="section-description">Both operations preserve the source and write a new result.</p>
                    </div>
                </header>
                {#if diagnosisMatchesSource && diagnosis?.salvage_supported === false}
                    <div class="candidate-guidance operation-blocker" role="note">
                        <i class="bi bi-slash-circle"></i>
                        <span>{diagnosis.salvage_blocker || diagnosis.salvage_blockers?.join(" · ") || "This stream layout can be diagnosed and previewed, but automatic salvage is disabled to avoid silently dropping media."}</span>
                    </div>
                {/if}
                <div class="operation-grid">
                    <article>
                        <i class="bi bi-box-arrow-in-down"></i>
                        <div>
                            <h3>Remux surviving packets</h3>
                            <p>Copy readable packets into a new container. This can help with index or timestamp damage, but cannot recover unreadable packets or a missing normal MP4 <code>moov</code> structure.</p>
                        </div>
                        <button class="button" on:click={() => startSalvage("remux")} disabled={!canStartSalvage || submitting}>
                            Remux packets
                        </button>
                    </article>
                    <article>
                        <i class="bi bi-tools"></i>
                        <div>
                            <h3>Recover decodable media</h3>
                            <p>Decode and re-encode what remains readable. Damaged spans may be skipped, frozen, silent, or shortened. Specialized reference recovery is not guaranteed.</p>
                        </div>
                        <button class="button-primary" on:click={() => startSalvage("reencode")} disabled={!canStartSalvage || submitting}>
                            Recover media
                        </button>
                    </article>
                </div>
            </section>
        </main>

        <aside class="panel jobs-panel" aria-labelledby="jobs-heading">
            <header class="section-header">
                <div>
                    <h2 id="jobs-heading" class="section-title">
                        <i class="bi bi-list-task"></i> Salvage jobs
                    </h2>
                    <p class="section-description">
                        {activeJobs.length ? `${activeJobs.length} active` : "No active work"}
                    </p>
                </div>
                <button class="button-quiet" on:click={() => loadJobs()} disabled={loadingJobs} aria-label="Refresh salvage jobs">
                    <i class={`bi bi-arrow-clockwise ${loadingJobs ? "spin" : ""}`}></i>
                </button>
            </header>

            <div class="jobs-list">
                {#if loadingJobs && !jobs.length}
                    <div class="empty-state compact"><i class="bi bi-arrow-repeat spin"></i><span>Loading jobs…</span></div>
                {:else if !jobs.length}
                    <div class="empty-state compact"><i class="bi bi-inbox"></i><span>No salvage jobs have been recorded.</span></div>
                {:else}
                    {#each jobs as job (job.id)}
                        <article class:job-failed={job.status === "failed"} class:job-complete={terminalStatuses.has(job.status) && job.status !== "failed"}>
                            <button class="job-main" on:click={() => selectJob(job)} aria-label={`Open ${operationLabel(job.operation)} job`}>
                                <span class="job-heading">
                                    <strong>{operationLabel(job.operation)}</strong>
                                    <small data-status={job.status}>{statusLabel(job.status)}</small>
                                </span>
                                <span class="job-file" title={job.source_path}>{basename(job.source_path || "Unknown source")}</span>
                                {#if isActive(job)}
                                    <span class="job-progress" aria-label={`${Math.round(job.progress_percent || 0)} percent complete`}>
                                        <span style={`width: ${job.progress_percent || 0}%`}></span>
                                    </span>
                                    <span class="job-detail">
                                        <span>{phaseLabel(job.phase || job.message)}</span>
                                        <span>{Math.round(job.progress_percent || 0)}%</span>
                                    </span>
                                {:else if job.error}
                                    <span class="job-error">{job.error}</span>
                                {:else if job.message}
                                    <span class="job-detail"><span>{job.message}</span></span>
                                {/if}
                                {#if job.warnings?.length}
                                    <span class="job-error">{job.warnings.join(" · ")}</span>
                                {/if}
                            </button>
                            <div class="job-actions">
                                {#if isActive(job)}
                                    <button class="button-danger" on:click={() => cancelJob(job)} disabled={cancellingJob === job.id}>
                                        <i class="bi bi-stop-fill"></i> {cancellingJob === job.id ? "Stopping…" : "Cancel"}
                                    </button>
                                {/if}
                                {#if job.output_path && terminalStatuses.has(job.status) && job.status !== "failed"}
                                    <button class="button" on:click={() => openPreview(job.output_path || "", "Salvage output")}>
                                        <i class="bi bi-play-btn"></i> Preview output
                                    </button>
                                {/if}
                            </div>
                            {#if job.output_path}
                                <code class="job-output" title={job.output_path}>{job.output_path}</code>
                            {/if}
                            {#if job.output_duration_seconds}
                                <div class:duration-warning={Boolean((job.expected_duration_seconds || job.duration_seconds) && job.output_duration_seconds + 1 < (job.expected_duration_seconds || job.duration_seconds || 0))} class="job-duration">
                                    <span>Output {formatDuration(job.output_duration_seconds)}</span>
                                    {#if job.expected_duration_seconds}
                                        <span>Expected {formatDuration(job.expected_duration_seconds)}</span>
                                    {:else if job.duration_seconds}
                                        <span>Input {formatDuration(job.duration_seconds)}</span>
                                    {/if}
                                </div>
                            {/if}
                        </article>
                    {/each}
                {/if}
            </div>
        </aside>
    </div>
</div>

{#if previewTarget}
    <div class="preview-backdrop" role="presentation" on:click={() => closePreview()}>
        <section class="preview-dialog" role="dialog" aria-modal="true" aria-labelledby="salvage-preview-title" on:click|stopPropagation>
            <header>
                <div>
                    <h2 id="salvage-preview-title"><i class="bi bi-play-btn"></i> {previewTarget.label}</h2>
                    <code>{previewTarget.path}</code>
                </div>
                <button bind:this={previewCloseButton} class="button-quiet" on:click={() => closePreview()} aria-label="Close preview">
                    <i class="bi bi-x-lg"></i>
                </button>
            </header>
            <div class="preview-tabs" role="tablist" aria-label="Preview mode">
                <button class:active={previewMode === "video"} role="tab" aria-selected={previewMode === "video"} on:click={() => startPreview("video")}>
                    <i class="bi bi-film"></i> Watch
                </button>
                <button class:active={previewMode === "audio"} role="tab" aria-selected={previewMode === "audio"} on:click={() => startPreview("audio")}>
                    <i class="bi bi-headphones"></i> Listen only
                </button>
            </div>
            <div class="preview-stage">
                {#if previewLoading}
                    <div class="preview-message"><i class="bi bi-arrow-repeat spin"></i><span>Preparing preview…</span></div>
                {/if}
                {#if previewError}
                    <div class="preview-message preview-message--error"><i class="bi bi-exclamation-triangle"></i><span>{previewError}</span></div>
                {:else if previewUrl && previewMode === "video"}
                    <video bind:this={previewElement} src={previewUrl} controls playsinline preload="metadata" on:loadedmetadata={handlePreviewReady} on:canplay={handlePreviewReady} on:error={() => { previewLoading = false; previewError = "The browser could not decode this video stream. Try Listen only or inspect the job report."; }}></video>
                {:else if previewUrl}
                    <div class="audio-preview">
                        <i class="bi bi-soundwave"></i>
                        <audio bind:this={previewElement} src={previewUrl} controls preload="metadata" on:loadedmetadata={handlePreviewReady} on:canplay={handlePreviewReady} on:error={() => { previewLoading = false; previewError = "The browser could not decode the audio preview."; }}></audio>
                    </div>
                {/if}
            </div>
            <footer>Preview reads the selected file. It does not modify or promote it into the archive.</footer>
        </section>
    </div>
{/if}

<style>
    .salvage-page { display: grid; gap: 12px; }
    .salvage-brief { display: flex; min-height: 70px; align-items: center; justify-content: space-between; gap: 18px; padding: 10px 12px; background: var(--surface-raised) !important; }
    .salvage-brief__heading { display: flex; align-items: center; gap: 10px; }
    .salvage-brief__mark { display: grid; width: 38px; height: 38px; place-items: center; border: 1px solid var(--line); background: var(--page-deep); color: var(--accent); font-size: 18px; }
    .salvage-brief__rules { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
    .salvage-brief__rules span { display: inline-flex; align-items: center; gap: 5px; padding: 5px 7px; border: 1px solid var(--line-soft); background: var(--surface); color: var(--text-soft); font-size: 10px; }
    .salvage-brief__rules i { color: var(--accent); }
    .salvage-reality, .salvage-notice, .notice--error { display: flex; align-items: flex-start; gap: 9px; }
    .salvage-reality { border-left: 3px solid var(--warning); background: var(--page-deep); }
    .salvage-reality i { margin-top: 1px; color: var(--warning); }
    .salvage-reality strong { display: block; margin-bottom: 2px; color: var(--text); }
    .salvage-notice i { color: var(--success); }
    .notice-dismiss { min-height: 22px; margin: -4px -6px -4px auto; padding: 3px 6px; }
    .salvage-layout { display: grid; grid-template-columns: minmax(0, 1fr) 310px; gap: 12px; align-items: start; }
    .salvage-workspace { display: grid; min-width: 0; gap: 12px; }
    .step-number { display: inline-grid; width: 18px; height: 18px; place-items: center; border: 1px solid var(--line); background: var(--page-deep); color: var(--accent); font-size: 10px; }
    .source-form { display: grid; gap: 7px; padding: 12px; }
    .source-form label, .candidate-entry label, .expected-entry label { color: var(--text-soft); font-size: 11px; font-weight: 700; }
    .path-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
    .path-row input, .candidate-entry input, .expected-entry input { width: 100%; padding: 6px 9px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; }
    .candidate-entry { margin-top: 3px; padding-top: 8px; border-top: 1px solid var(--line-soft); }
    .candidate-entry summary { width: fit-content; color: var(--text-soft); cursor: pointer; font-size: 11px; font-weight: 700; }
    .candidate-entry label { display: block; margin: 9px 0 5px; }
    .candidate-entry label span { color: var(--text-muted); font-weight: 400; }
    .candidate-entry p { margin: 5px 0 0; color: var(--text-muted); font-size: 10px; }
    .expected-entry { display: grid; grid-template-columns: 154px minmax(110px, 160px) minmax(0, 1fr); align-items: center; gap: 8px; padding-top: 8px; border-top: 1px solid var(--line-soft); }
    .expected-entry label span { color: var(--text-muted); font-weight: 400; }
    .expected-entry p { margin: 0; color: var(--text-muted); font-size: 10px; }
    .diagnosis-panel { min-height: 260px; }
    .diagnosis-body { display: grid; gap: 12px; padding: 12px; }
    .media-report { border: 1px solid var(--line-soft); background: var(--page-deep); }
    .media-report__title { display: flex; align-items: center; gap: 9px; padding: 9px 10px; border-bottom: 1px solid var(--line-soft); background: var(--surface); }
    .media-report__title > i { color: var(--accent); font-size: 18px; }
    .media-report__title strong, .media-report__title span { display: block; }
    .media-report__title span { margin-top: 1px; color: var(--text-muted); font-size: 10px; }
    .report-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; }
    .report-facts > div { min-width: 0; padding: 8px 10px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
    .report-facts > div:nth-child(2n) { border-right: 0; }
    .report-facts dt { color: var(--text-muted); font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
    .report-facts dd { margin: 2px 0 0; color: var(--text-soft); font-size: 11px; overflow-wrap: anywhere; }
    .finding-list { display: grid; gap: 4px; padding: 8px 10px; }
    .finding-list div { display: flex; align-items: flex-start; gap: 7px; color: var(--text-soft); font-size: 11px; }
    .finding-list i { color: var(--warning); }
    .recommendation { display: grid; gap: 2px; padding: 8px 10px; border-top: 1px solid var(--line-soft); background: var(--surface); }
    .recommendation span { color: var(--text-muted); font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
    .recommendation strong { font-size: 11px; }
    .candidate-section { display: grid; gap: 8px; }
    .candidate-section__heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .candidate-section__heading strong, .candidate-section__heading span { display: block; }
    .candidate-section__heading span { color: var(--text-muted); font-size: 10px; }
    .candidate-section__actions { display: flex; align-items: center; gap: 5px; }
    .candidate-snapshot { display: grid; gap: 7px; padding: 9px; border: 1px solid var(--line); background: var(--surface); }
    .candidate-snapshot__heading, .candidate-snapshot__heading > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .candidate-snapshot__heading > div { justify-content: flex-start; }
    .candidate-snapshot__heading i { color: var(--success); }
    .candidate-snapshot__heading strong, .candidate-snapshot__heading small { display: block; }
    .candidate-snapshot__heading small, .candidate-snapshot > p { color: var(--text-muted); font-size: 10px; }
    .candidate-snapshot__facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; border: 1px solid var(--line-soft); }
    .candidate-snapshot__facts div { min-width: 0; padding: 6px; border-right: 1px solid var(--line-soft); }
    .candidate-snapshot__facts div:last-child { border-right: 0; }
    .candidate-snapshot__facts dt { color: var(--text-muted); font-size: 8px; letter-spacing: .06em; text-transform: uppercase; }
    .candidate-snapshot__facts dd { margin: 2px 0 0; overflow: hidden; color: var(--text-soft); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .candidate-snapshot > code { overflow: hidden; color: var(--text-soft); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
    .candidate-snapshot > .candidate-snapshot__warning { margin: 0; color: var(--warning); }
    .candidate-snapshot > p { margin: 0; }
    .candidate-list { display: grid; gap: 5px; }
    .candidate-guidance { display: flex; align-items: flex-start; gap: 7px; padding: 7px 8px; border-left: 2px solid var(--warning); background: var(--page-deep); color: var(--text-muted); font-size: 10px; }
    .candidate-guidance i { color: var(--warning); }
    .candidate-guidance--neutral { border-left-color: var(--accent); }
    .candidate-guidance--neutral i { color: var(--accent); }
    .operation-blocker { margin: 10px 12px 0; }
    .candidate-list label { display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 8px; border: 1px solid var(--line-soft); background: var(--page-deep); cursor: pointer; }
    .candidate-list label:hover, .candidate-list label.selected { border-color: var(--line); background: var(--surface-hover); }
    .candidate-list input { position: absolute; opacity: 0; pointer-events: none; }
    .candidate-radio { display: block; width: 12px; height: 12px; border: 1px solid var(--text-muted); border-radius: 50%; }
    .candidate-list label.selected .candidate-radio { border: 3px solid var(--accent); }
    .candidate-copy { min-width: 0; }
    .candidate-copy strong, .candidate-copy small, .candidate-copy code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .candidate-copy small { color: var(--text-muted); font-size: 10px; }
    .candidate-copy .candidate-checksum { color: var(--text-soft); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .candidate-copy .candidate-warning { color: var(--warning); white-space: normal; }
    .candidate-copy code { margin-top: 3px; color: var(--text-soft); font-size: 9px; }
    .candidate-badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 5px; border: 1px solid var(--line); color: var(--accent-strong); font-size: 9px; white-space: nowrap; }
    .operation-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .operation-grid article { display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 8px; padding: 12px; border-right: 1px solid var(--line-soft); }
    .operation-grid article:last-child { border-right: 0; }
    .operation-grid article > i { display: grid; width: 28px; height: 28px; place-items: center; border: 1px solid var(--line-soft); background: var(--page-deep); color: var(--accent); }
    .operation-grid h3 { margin: 0; font-size: 12px; }
    .operation-grid p { min-height: 47px; margin: 3px 0 8px; color: var(--text-muted); font-size: 10px; }
    .operation-grid button { grid-column: 2; justify-self: start; }
    .jobs-panel { position: sticky; top: 56px; max-height: calc(100vh - 68px); overflow: hidden; }
    .jobs-list { max-height: calc(100vh - 112px); overflow-y: auto; }
    .jobs-list > article { border-bottom: 1px solid var(--line-soft); background: var(--page-deep); }
    .jobs-list > article.job-complete { border-left: 2px solid var(--success); }
    .jobs-list > article.job-failed { border-left: 2px solid var(--danger); }
    .job-main { display: grid; width: 100%; gap: 5px; padding: 9px 9px 7px; border: 0; background: transparent; color: var(--text); cursor: pointer; text-align: left; }
    .job-main:hover { background: var(--surface-hover); }
    .job-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .job-heading strong { font-size: 11px; }
    .job-heading small { color: var(--text-muted); font-size: 9px; letter-spacing: .05em; text-transform: uppercase; }
    .job-heading small[data-status="failed"] { color: #f0d4ce; }
    .job-file { overflow: hidden; color: var(--text-soft); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .job-progress { display: block; height: 5px; border: 1px solid var(--line-soft); background: var(--surface); overflow: hidden; }
    .job-progress span { display: block; height: 100%; background: var(--accent); }
    .job-detail { display: flex; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 9px; }
    .job-detail span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .job-error { color: #f0d4ce; font-size: 10px; }
    .job-actions { display: flex; gap: 5px; padding: 0 9px 8px; }
    .job-actions button { min-height: 24px; padding: 3px 7px; font-size: 9px; }
    .job-output { display: block; padding: 0 9px 8px; color: var(--text-muted); font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .job-duration { display: flex; justify-content: space-between; gap: 8px; padding: 6px 9px; border-top: 1px solid var(--line-soft); color: var(--text-muted); font-size: 9px; }
    .job-duration.duration-warning { border-left: 2px solid var(--warning); color: var(--warning); }
    .job-duration.duration-warning::before { content: "Output shorter"; margin-right: auto; font-weight: 700; text-transform: uppercase; }
    .empty-state { display: grid; place-items: center; gap: 5px; padding: 30px 14px; color: var(--text-muted); text-align: center; }
    .empty-state > i { color: var(--accent); font-size: 20px; }
    .empty-state strong { color: var(--text-soft); font-size: 11px; }
    .empty-state span { max-width: 430px; font-size: 10px; }
    .empty-state.compact { grid-template-columns: auto minmax(0, 1fr); place-items: center start; padding: 14px; text-align: left; }
    .empty-state.compact > i { font-size: 14px; }
    .diagnosis-empty { min-height: 190px; }
    .preview-backdrop { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; padding: 18px; background: rgb(28 34 27 / .88); }
    .preview-dialog { display: grid; width: min(900px, 100%); max-height: calc(100vh - 36px); border: 1px solid var(--line); background: var(--surface); box-shadow: 0 18px 60px rgb(0 0 0 / .4); overflow: hidden; }
    .preview-dialog > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 9px 10px; border-bottom: 1px solid var(--line-soft); background: var(--surface-raised); }
    .preview-dialog h2 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 12px; }
    .preview-dialog header code { display: block; max-width: 760px; margin-top: 3px; color: var(--text-muted); font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .preview-tabs { display: flex; border-bottom: 1px solid var(--line-soft); background: var(--page-deep); }
    .preview-tabs button { min-height: 32px; padding: 5px 12px; border: 0; border-right: 1px solid var(--line-soft); border-bottom: 2px solid transparent; background: transparent; color: var(--text-soft); cursor: pointer; font-size: 10px; }
    .preview-tabs button.active { border-bottom-color: var(--accent); background: var(--surface); color: var(--text); }
    .preview-stage { position: relative; display: grid; min-height: 310px; max-height: calc(100vh - 180px); place-items: center; padding: 10px; background: #20261f; overflow: auto; }
    .preview-stage video { display: block; width: 100%; max-height: calc(100vh - 200px); background: #000; }
    .audio-preview { display: grid; width: min(560px, 100%); place-items: center; gap: 18px; padding: 35px; border: 1px solid var(--line-soft); background: var(--page-deep); }
    .audio-preview > i { color: var(--accent); font-size: 38px; }
    .audio-preview audio { width: 100%; }
    .preview-message { position: absolute; z-index: 2; display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--line); background: var(--surface); color: var(--text-soft); font-size: 11px; }
    .preview-message--error { border-color: var(--danger); color: #f0d4ce; }
    .preview-dialog > footer { padding: 7px 10px; border-top: 1px solid var(--line-soft); background: var(--page-deep); color: var(--text-muted); font-size: 9px; }
    .spin { animation: spin 900ms linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 980px) {
        .salvage-layout { grid-template-columns: minmax(0, 1fr); }
        .jobs-panel { position: static; max-height: none; }
        .jobs-list { max-height: 420px; }
    }
    @media (max-width: 700px) {
        .salvage-brief { align-items: stretch; flex-direction: column; }
        .salvage-brief__rules { justify-content: flex-start; }
        .path-row, .operation-grid { grid-template-columns: minmax(0, 1fr); }
        .expected-entry { grid-template-columns: minmax(0, 1fr); }
        .operation-grid article { border-right: 0; border-bottom: 1px solid var(--line-soft); }
        .operation-grid article:last-child { border-bottom: 0; }
        .report-facts { grid-template-columns: minmax(0, 1fr); }
        .report-facts > div { border-right: 0; }
        .candidate-snapshot__heading { align-items: stretch; flex-direction: column; }
        .candidate-snapshot__heading .button { align-self: flex-start; }
        .candidate-snapshot__facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .candidate-snapshot__facts div:nth-child(2) { border-right: 0; }
        .candidate-snapshot__facts div:nth-child(-n + 2) { border-bottom: 1px solid var(--line-soft); }
        .candidate-list label { grid-template-columns: 16px minmax(0, 1fr); }
        .candidate-badge { display: none; }
    }
</style>
