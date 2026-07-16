<script lang="ts">
    import { createEventDispatcher, onMount, onDestroy, tick } from "svelte";
    const dispatch = createEventDispatcher();

    type RecoveryTab =
        | "unindexed"
        | "rescue"
        | "redundant"
        | "lost"
        | "channels";

    const recoveryTabs: RecoveryTab[] = [
        "unindexed",
        "rescue",
        "redundant",
        "lost",
        "channels",
    ];

    type RecoveryItem = {
        path: string;
        requires_destruct?: boolean;
        [key: string]: any;
    };

    let activeTab: RecoveryTab = "unindexed";
    let scanning = false;
    let status = "idle";
    let results: Record<string, RecoveryItem[]> = emptyResults();
    let pollInterval: ReturnType<typeof setInterval> | undefined;
    let pollRequestInFlight = false;
    let operationError: string | null = null;

    // State for Destruct Mode & Multi-selection
    let destructMode = false;
    let selectedPaths = new Set<string>();

    // Channel specific state
    let channels: string[] = [];
    let selectedChannel = "";
    let channelVideos: RecoveryItem[] = [];
    let loadingChannels = false;
    let searchingVideos = false;
    let channelRequestSequence = 0;
    let channelAbortController: AbortController | null = null;

    // Media preview state. The backend endpoint supports range requests, so the
    // native media element can seek without downloading an entire archive file.
    let previewItem: RecoveryItem | null = null;
    let previewUrl = "";
    let previewLoading = false;
    let previewError: string | null = null;
    let previewElement: HTMLMediaElement | null = null;
    let previewCloseButton: HTMLButtonElement | null = null;
    let previewReturnFocus: HTMLElement | null = null;
    let previewMode: "video" | "audio" = "video";
    let previewRequestSequence = 0;
    let previewAbortController: AbortController | null = null;
    let recoveryCloseButton: HTMLButtonElement | null = null;
    let recoveryWorkspace: HTMLElement | null = null;
    let previewDialog: HTMLElement | null = null;
    let modalReturnFocus: HTMLElement | null = null;
    let previousBodyOverflow = "";

    // Permissions state
    let permissions: any = null;
    let loadingPermissions = false;
    let readOnlyPermissions: [string, any][] = [];

    function emptyResults(): Record<string, RecoveryItem[]> {
        return { unindexed: [], rescue: [], redundant: [], lost: [] };
    }

    function messageFrom(error: unknown): string {
        return error instanceof Error ? error.message : String(error);
    }

    async function readResponse(res: Response): Promise<any> {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(
                body.error ||
                    body.message ||
                    `Request failed with status ${res.status}`,
            );
        }
        return body;
    }

    function recordError(context: string, error: unknown) {
        operationError = `${context}: ${messageFrom(error)}`;
    }

    function canDeleteItem(item: RecoveryItem): boolean {
        // Fail closed if an older/malformed response omits the capability flag.
        return Boolean(itemPath(item)) &&
            (destructMode || item.requires_destruct === false);
    }

    function itemPath(item: RecoveryItem | null): string | null {
        return typeof item?.path === "string" && item.path.trim()
            ? item.path
            : null;
    }

    function basename(path: string): string {
        const parts = path.replace(/\\/g, "/").split("/");
        return parts[parts.length - 1] || path;
    }

    function previewName(item: RecoveryItem): string {
        const path = itemPath(item);
        return item.title || item.filename || (path ? basename(path) : "Untitled media");
    }

    function cancelPreviewRequest() {
        previewRequestSequence += 1;
        previewAbortController?.abort();
        previewAbortController = null;
    }

    function unloadPreviewMedia() {
        if (!previewElement) return;
        previewElement.pause();
        previewElement.removeAttribute("src");
        previewElement.load();
        previewElement = null;
    }

    function closePreview(restoreFocus = true) {
        const returnFocus = previewReturnFocus;
        cancelPreviewRequest();
        unloadPreviewMedia();
        previewItem = null;
        previewUrl = "";
        previewLoading = false;
        previewError = null;
        previewMode = "video";
        previewReturnFocus = null;
        if (restoreFocus && returnFocus?.isConnected) {
            void tick().then(() => returnFocus.focus());
        }
    }

    function closeRecoveryModal() {
        closePreview(false);
        dispatch("close");
    }

    function openPreview(item: RecoveryItem) {
        if (!itemPath(item)) {
            recordError("Preview unavailable", "This item has no readable media path");
            return;
        }
        closePreview(false);
        previewReturnFocus =
            document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        previewItem = item;
        void startVideoPreview();
        void tick().then(() => previewCloseButton?.focus());
    }

    async function startVideoPreview() {
        const path = itemPath(previewItem);
        if (!path) return;
        cancelPreviewRequest();
        unloadPreviewMedia();
        previewMode = "video";
        previewError = null;
        previewLoading = true;
        previewUrl = "";

        const requestSequence = ++previewRequestSequence;
        const controller = new AbortController();
        previewAbortController = controller;
        const url = `/api/recovery/preview?filepath=${encodeURIComponent(path)}`;
        try {
            const response = await fetch(url, {
                method: "HEAD",
                signal: controller.signal,
            });
            if (!response.ok) {
                const statusMessages: Record<number, string> = {
                    401: "Your session expired. Sign in again before previewing media.",
                    403: "The server refused access to this media file.",
                    404: "The media file no longer exists at this path.",
                    415: "This file type is not available for browser preview.",
                    429: "Too many previews are already open. Close one and try again.",
                };
                throw new Error(
                    statusMessages[response.status] ||
                        `The preview request failed with status ${response.status}.`,
                );
            }
            if (requestSequence !== previewRequestSequence) return;
            previewUrl = url;
        } catch (error) {
            if ((error as Error)?.name === "AbortError") return;
            if (requestSequence !== previewRequestSequence) return;
            previewLoading = false;
            previewError = messageFrom(error);
        } finally {
            if (requestSequence === previewRequestSequence) {
                previewAbortController = null;
            }
        }
    }

    async function startAudioPreview() {
        const path = itemPath(previewItem);
        if (!path) return;
        cancelPreviewRequest();
        unloadPreviewMedia();
        previewMode = "audio";
        previewError = null;
        previewLoading = true;
        previewUrl = "";

        const requestSequence = ++previewRequestSequence;
        const controller = new AbortController();
        previewAbortController = controller;
        const url = `/api/recovery/preview-audio?filepath=${encodeURIComponent(path)}`;
        try {
            const response = await fetch(url, {
                method: "HEAD",
                signal: controller.signal,
            });
            if (!response.ok) {
                const statusMessages: Record<number, string> = {
                    401: "Your session expired. Sign in again before previewing media.",
                    403: "The server refused access to this media file.",
                    404: "The media file no longer exists at this path.",
                    415: "This file type cannot be inspected for audio.",
                    422: "No readable audio track was found in this file.",
                    429: "Another Listen only preview is active. Close it and try again.",
                    503: "The audio compatibility service is temporarily unavailable.",
                };
                throw new Error(
                    statusMessages[response.status] ||
                        `The audio preview request failed with status ${response.status}.`,
                );
            }
            if (requestSequence !== previewRequestSequence) return;
            previewUrl = url;
        } catch (error) {
            if ((error as Error)?.name === "AbortError") return;
            if (requestSequence !== previewRequestSequence) return;
            previewLoading = false;
            previewError = messageFrom(error);
        } finally {
            if (requestSequence === previewRequestSequence) {
                previewAbortController = null;
            }
        }
    }

    function handlePreviewReady() {
        previewLoading = false;
        previewError = null;
    }

    function handlePreviewError() {
        previewLoading = false;
        const code = previewElement?.error?.code;
        const messages: Record<number, string> = {
            1: "Playback was cancelled before the file finished loading.",
            2: "The media stream was interrupted while loading.",
            3:
                previewMode === "audio"
                    ? "The audio-only preview could not be decoded. The file may not contain an audio track."
                    : "The browser could not decode this video. Try Listen only.",
            4:
                previewMode === "audio"
                    ? "The audio-only preview could not start. The server may be busy or FFmpeg may be unavailable."
                    : "This video format is not supported by the browser. Try Listen only.",
        };
        previewError =
            (code && messages[code]) ||
            "The media could not be loaded. It may be missing or unreadable.";
    }

    function handleWindowKeydown(event: KeyboardEvent) {
        if (event.key === "Tab") {
            trapModalFocus(event);
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            if (previewItem) closePreview();
            else closeRecoveryModal();
        }
    }

    function trapModalFocus(event: KeyboardEvent) {
        const root = previewItem ? previewDialog : recoveryWorkspace;
        if (!root) return;
        const focusable = Array.from(
            root.querySelectorAll<HTMLElement>(
                'button:not(:disabled):not([tabindex="-1"]), input:not(:disabled):not([tabindex="-1"]), select:not(:disabled):not([tabindex="-1"]), video[controls], audio[controls], [href], [tabindex]:not([tabindex="-1"])',
            ),
        ).filter(
            (element) =>
                !element.hasAttribute("hidden") &&
                element.getAttribute("aria-hidden") !== "true",
        );
        if (focusable.length === 0) {
            event.preventDefault();
            return;
        }

        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (!root.contains(active)) {
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

    async function checkPermissions() {
        loadingPermissions = true;
        try {
            const res = await fetch("/api/system/check-permissions");
            permissions = await readResponse(res);
        } catch (e) {
            console.error("Failed to check permissions", e);
            recordError("Permission check failed", e);
        } finally {
            loadingPermissions = false;
        }
    }

    async function startScan() {
        scanning = true;
        status = "starting";
        results = emptyResults();
        selectedPaths = new Set();
        operationError = null;
        try {
            const res = await fetch("/api/recovery/scan", {
                method: "POST",
            });
            const data = await readResponse(res);
            status = data.status || "scanning";
            pollResults();
        } catch (e) {
            recordError("Scan start failed", e);
            alert("Scan start failed: " + messageFrom(e));
            scanning = false;
            status = "error";
        }
    }

    function pollResults() {
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(async () => {
            if (pollRequestInFlight) return;
            pollRequestInFlight = true;
            try {
                const res = await fetch("/api/recovery/poll");
                const data = await readResponse(res);
                status = data.status;
                if (data.status === "done") {
                    const next = data.results || {};
                    results = {
                        unindexed: next.unindexed || [],
                        rescue: next.rescue || [],
                        redundant: next.redundant || [],
                        lost: next.lost || [],
                    };
                    selectedPaths = new Set();
                    scanning = false;
                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = undefined;
                } else if (data.status === "error") {
                    scanning = false;
                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = undefined;
                    alert("Scan error: " + data.results);
                }
            } catch (e) {
                console.error(e);
                recordError("Scan polling failed", e);
                scanning = false;
                status = "error";
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = undefined;
            } finally {
                pollRequestInFlight = false;
            }
        }, 2000);
    }

    async function fetchChannels() {
        loadingChannels = true;
        try {
            const res = await fetch("/api/channels");
            const data = await readResponse(res);
            if (!Array.isArray(data)) {
                throw new Error("Channel response was not a list");
            }
            channels = data;
        } catch (e) {
            console.error("Failed to fetch channels", e);
            recordError("Failed to fetch channels", e);
        } finally {
            loadingChannels = false;
        }
    }

    async function fetchChannelVideos(channel: string) {
        const requestSequence = ++channelRequestSequence;
        channelAbortController?.abort();
        channelAbortController = null;
        selectedChannel = channel;
        channelVideos = [];
        selectedPaths = new Set();
        if (!channel) {
            searchingVideos = false;
            return;
        }

        searchingVideos = true;
        operationError = null;
        const controller = new AbortController();
        channelAbortController = controller;
        try {
            const res = await fetch(
                `/api/channels/videos?channel=${encodeURIComponent(channel)}`,
                { signal: controller.signal },
            );
            const data = await readResponse(res);
            if (!Array.isArray(data)) {
                throw new Error("Channel video response was not a list");
            }
            if (requestSequence === channelRequestSequence) {
                channelVideos = data;
                selectedPaths = new Set();
            }
        } catch (e) {
            if ((e as Error)?.name === "AbortError") return;
            console.error("Failed to fetch channel videos", e);
            if (requestSequence === channelRequestSequence) {
                channelVideos = [];
                recordError("Failed to fetch channel videos", e);
            }
        } finally {
            if (requestSequence === channelRequestSequence) {
                searchingVideos = false;
                channelAbortController = null;
            }
        }
    }

    onDestroy(() => {
        if (pollInterval) clearInterval(pollInterval);
        channelRequestSequence += 1;
        channelAbortController?.abort();
        cancelPreviewRequest();
        unloadPreviewMedia();
        document.body.style.overflow = previousBodyOverflow;
        if (modalReturnFocus?.isConnected) modalReturnFocus.focus();
    });

    async function runRecoveryAction(
        endpoint: string,
        path: string,
        action: "Recover" | "Rescue" | "Force import",
    ) {
        if (!confirm(`${action} this file?`)) return;
        operationError = null;
        try {
            const res = await fetch(endpoint, {
                method: "POST",
                body: JSON.stringify({ filepath: path }),
                headers: { "Content-Type": "application/json" },
            });
            const data = await readResponse(res);
            if (data.success === false) {
                throw new Error(data.message || `${action} failed`);
            }
            alert(data.message || `${action} completed.`);
            if (activeTab === "channels") {
                await fetchChannelVideos(selectedChannel);
            } else {
                await startScan();
            }
        } catch (e) {
            recordError(`${action} failed`, e);
            alert(`${action} failed: ${messageFrom(e)}`);
        }
    }

    function recoverFile(path: string) {
        return runRecoveryAction("/api/recovery/start", path, "Recover");
    }

    function rescueFile(path: string) {
        return runRecoveryAction("/api/recovery/start", path, "Rescue");
    }

    function forceFile(path: string) {
        return runRecoveryAction("/api/recovery/force", path, "Force import");
    }

    async function deleteFile(item: RecoveryItem) {
        if (!canDeleteItem(item)) {
            alert("Enable Destruct Mode to delete this source archive file.");
            return;
        }

        let msg = "Delete file? This cannot be undone.";
        if (destructMode) {
            msg = "Destruct mode will permanently delete both the organized entry and its source archive file. Continue?";
        }

        if (!confirm(msg)) return;
        if (destructMode && !confirm("This cannot be undone. Permanently delete the source file now?")) return;

        operationError = null;
        try {
            const res = await fetch("/api/recovery/delete-batch", {
                method: "POST",
                body: JSON.stringify({
                    filepaths: [item.path],
                    destruct_mode: destructMode,
                }),
                headers: { "Content-Type": "application/json" },
            });
            const d = await readResponse(res);
            if (d.success_count > 0) {
                alert("Deleted.");
                if (activeTab === "channels") {
                    await fetchChannelVideos(selectedChannel);
                } else {
                    await startScan();
                }
            } else {
                const err = d.errors?.[0] || "Unknown error";
                alert(`Error deleting file: ${err}`);
            }
        } catch (e) {
            recordError("Delete failed", e);
            alert("Delete failed: " + messageFrom(e));
        }
    }

    async function deleteSelected() {
        const selectedItems = visibleItems.filter(
            (item) => selectedPaths.has(item.path) && canDeleteItem(item),
        );
        const paths = selectedItems.map((item) => item.path);
        if (paths.length === 0) {
            selectedPaths = new Set();
            return;
        }

        // Never submit stale selections that are no longer in the visible dataset.
        selectedPaths = new Set(paths);
        let msg = `Delete ${paths.length} selected items?`;
        if (destructMode) {
            msg = `Destruct mode will permanently delete ${paths.length} organized entries and their source archive files. Continue?`;
        }

        if (!confirm(msg)) return;
        if (destructMode && !confirm("This cannot be undone. Permanently delete the selected source files now?")) return;

        operationError = null;
        try {
            const res = await fetch("/api/recovery/delete-batch", {
                method: "POST",
                body: JSON.stringify({
                    filepaths: paths,
                    destruct_mode: destructMode,
                }),
                headers: { "Content-Type": "application/json" },
            });
            const d = await readResponse(res);

            if (d.fail_count > 0) {
                const firstErr = d.errors?.[0] || "Check backend logs.";
                alert(`Batch partially failed.\nSuccess: ${d.success_count}\nFailed: ${d.fail_count}\n\nFirst error: ${firstErr}`);
            } else {
                alert(`Batch complete. Deleted ${d.success_count} items.`);
            }

            selectedPaths = new Set();
            if (activeTab === "channels") {
                await fetchChannelVideos(selectedChannel);
            } else {
                await startScan();
            }
        } catch (e) {
            recordError("Batch delete failed", e);
            alert("Batch delete failed: " + messageFrom(e));
        }
    }

    function toggleSelect(item: RecoveryItem) {
        if (!canDeleteItem(item)) return;
        const next = new Set(selectedPaths);
        if (next.has(item.path)) {
            next.delete(item.path);
        } else {
            next.add(item.path);
        }
        selectedPaths = next;
    }

    function toggleAll() {
        if (
            selectableItems.length > 0 &&
            selectableItems.every((item) => selectedPaths.has(item.path))
        ) {
            selectedPaths = new Set();
        } else {
            selectedPaths = new Set(selectableItems.map((item) => item.path));
        }
    }

    function handleDestructModeChange(event: Event) {
        destructMode = (event.currentTarget as HTMLInputElement).checked;
        selectedPaths = new Set();
    }

    function selectRecoveryTab(tab: RecoveryTab, focusTab = false) {
        activeTab = tab;
        selectedPaths = new Set();
        if (focusTab) {
            void tick().then(() =>
                document.getElementById(`recovery-tab-${tab}`)?.focus(),
            );
        }
    }

    function handleTabKeydown(event: KeyboardEvent, tab: RecoveryTab) {
        const currentIndex = recoveryTabs.indexOf(tab);
        let nextIndex: number | null = null;
        if (event.key === "ArrowRight") {
            nextIndex = (currentIndex + 1) % recoveryTabs.length;
        } else if (event.key === "ArrowLeft") {
            nextIndex =
                (currentIndex - 1 + recoveryTabs.length) % recoveryTabs.length;
        } else if (event.key === "Home") {
            nextIndex = 0;
        } else if (event.key === "End") {
            nextIndex = recoveryTabs.length - 1;
        }
        if (nextIndex === null) return;
        event.preventDefault();
        selectRecoveryTab(recoveryTabs[nextIndex], true);
    }

    $: visibleItems =
        activeTab === "channels" ? channelVideos : results[activeTab] || [];
    $: selectableItems = visibleItems.filter(canDeleteItem);
    $: allSelected =
        selectableItems.length > 0 &&
        selectableItems.every((item) => selectedPaths.has(item.path));

    $: readOnlyPermissions = permissions
        ? (Object.entries(permissions).filter(
              ([, details]: [string, any]) => details?.writeable === false,
          ) as [string, any][])
        : [];

    onMount(() => {
        modalReturnFocus =
            document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        previousBodyOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        fetchChannels();
        checkPermissions();
        void tick().then(() => recoveryCloseButton?.focus());
    });
</script>

<svelte:window on:keydown={handleWindowKeydown} />

<div class="recovery-overlay">
    <div
        bind:this={recoveryWorkspace}
        class="recovery-workspace"
        role="dialog"
        aria-modal="true"
        aria-labelledby="recovery-title"
        aria-hidden={previewItem ? "true" : undefined}
        inert={previewItem !== null}
    >
        <header class="recovery-header">
            <div class="recovery-heading">
                <i class="bi bi-bandaid" aria-hidden="true"></i>
                <h2 id="recovery-title">Recovery</h2>
                {#if destructMode}
                    <span class="destruct-state">Destruct mode active</span>
                {/if}
            </div>
            <button
                bind:this={recoveryCloseButton}
                on:click={closeRecoveryModal}
                class="icon-button"
                aria-label="Close recovery workspace"
                title="Close recovery workspace"
            >
                <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
        </header>

        <!-- Permission Warnings -->
        {#if readOnlyPermissions.length > 0}
            <div class="notice notice-danger" role="status">
                <i class="bi bi-exclamation-octagon-fill" aria-hidden="true"></i>
                <div class="notice-copy">
                    <strong>Filesystem permissions</strong>
                    <span>These paths are not writable. Each operation will be validated by the server.</span>
                    {#each readOnlyPermissions as [name, details]}
                        <div class="permission-detail">
                            {name}: {details?.message || details?.status || "not writable"}
                        </div>
                    {/each}
                </div>
                <button
                    on:click={checkPermissions}
                    class="button button-danger-subtle notice-action"
                    disabled={loadingPermissions}
                >
                    {loadingPermissions ? "Checking…" : "Check again"}
                </button>
            </div>
        {/if}

        {#if operationError}
            <div class="operation-error" role="alert">
                {operationError}
            </div>
        {/if}

        <div class="recovery-toolbar">
            <div class="toolbar-actions">
                <button
                    class="button button-primary"
                    on:click={startScan}
                    disabled={scanning}
                >
                    <i class="bi bi-search" aria-hidden="true"></i>
                    {scanning ? "Scanning…" : "Run scan"}
                </button>

                {#if selectedPaths.size > 0}
                    <button
                        class="button button-danger"
                        on:click={deleteSelected}
                    >
                        <i class="bi bi-trash" aria-hidden="true"></i>
                        Delete selected ({selectedPaths.size})
                    </button>
                {/if}
            </div>

            <div class="toolbar-status">
                <label class="destruct-toggle">
                    <input
                        type="checkbox"
                        checked={destructMode}
                        on:change={handleDestructModeChange}
                        class="recovery-checkbox"
                    />
                    <span>
                        Destruct mode
                        {#if destructMode}
                            <i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i>
                        {/if}
                    </span>
                </label>

                <div class="scan-status" aria-live="polite">
                    <span>Status</span>
                    <strong>{status}</strong>
                </div>
            </div>
        </div>

        <div class="recovery-tabs" role="tablist" aria-label="Recovery views">
            {#each recoveryTabs as tab}
                <button
                    id={`recovery-tab-${tab}`}
                    class:active={activeTab === tab}
                    role="tab"
                    aria-selected={activeTab === tab}
                    aria-controls="recovery-tab-panel"
                    tabindex={activeTab === tab ? 0 : -1}
                    on:click={() => selectRecoveryTab(tab)}
                    on:keydown={(event) => handleTabKeydown(event, tab)}
                >
                    {tab}
                    {#if tab !== 'channels'}
                        <span class="tab-count">{results[tab]?.length || 0}</span>
                    {/if}
                </button>
            {/each}
        </div>

        <div
            id="recovery-tab-panel"
            class="workspace-content"
            role="tabpanel"
            aria-labelledby={`recovery-tab-${activeTab}`}
        >
            {#if activeTab === "channels"}
                <div class="channel-view">
                    <div class="channel-selector">
                        <div class="field-group">
                            <label for="recovery-channel">Indexed channel</label>
                            <select
                                id="recovery-channel"
                                value={selectedChannel}
                                on:change={(e) => fetchChannelVideos(e.currentTarget.value)}
                            >
                                <option value="">Choose a channel</option>
                                {#each channels as channel}
                                    <option value={channel}>{channel}</option>
                                {/each}
                            </select>
                        </div>
                        <button
                            class="button icon-button selector-refresh"
                            on:click={fetchChannels}
                            disabled={loadingChannels}
                            title="Refresh channel list"
                            aria-label="Refresh channel list"
                        >
                            <i class="bi bi-arrow-clockwise" aria-hidden="true"></i>
                        </button>
                    </div>

                    <div class="table-shell channel-table-shell">
                        {#if searchingVideos}
                            <div class="loading-state" aria-live="polite">
                                <span class="loading-spinner" aria-hidden="true"></span>
                                <span>Loading channel videos…</span>
                            </div>
                        {:else if channelVideos.length > 0}
                            <table class="recovery-table">
                                <thead>
                                    <tr>
                                        <th class="selection-column">
                                            <input
                                                type="checkbox"
                                                checked={allSelected}
                                                on:change={toggleAll}
                                                disabled={selectableItems.length === 0}
                                                class="recovery-checkbox"
                                                aria-label="Select all available channel videos"
                                            />
                                        </th>
                                        <th>Video ID</th>
                                        <th>Title</th>
                                        <th class="action-column">Action</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each channelVideos as item}
                                        <tr class:selected-row={selectedPaths.has(item.path)}>
                                            <td class="selection-column">
                                                <input
                                                    type="checkbox"
                                                    checked={selectedPaths.has(item.path)}
                                                    on:change={() => toggleSelect(item)}
                                                    disabled={!canDeleteItem(item)}
                                                    title={!itemPath(item) ? "No media path is available" : !canDeleteItem(item) ? "Enable Destruct Mode to select this source file" : "Select item"}
                                                    class="recovery-checkbox"
                                                    aria-label={`Select ${previewName(item)}`}
                                                />
                                            </td>
                                            <td class="item-id">{item.video_id}</td>
                                            <td class="item-name" title={item.title}>{item.title}</td>
                                            <td class="action-column">
                                                <div class="row-actions">
                                                    <button
                                                        class="row-action preview-action"
                                                        on:click={() => openPreview(item)}
                                                        disabled={!itemPath(item)}
                                                        aria-label={`Preview ${previewName(item)}`}
                                                        title={itemPath(item) ? "Watch or listen" : "No media path is available"}
                                                    >
                                                        <i class="bi bi-play-circle" aria-hidden="true"></i>
                                                        Preview
                                                    </button>
                                                    <button
                                                        class="row-action action-danger icon-row-action"
                                                        on:click={() => deleteFile(item)}
                                                        disabled={!canDeleteItem(item)}
                                                        aria-label={`Delete ${previewName(item)}`}
                                                    >
                                                        <i class="bi bi-trash" aria-hidden="true"></i>
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        {:else}
                            <div class="empty-state">
                                <i class="bi bi-broadcast" aria-hidden="true"></i>
                                <span>{selectedChannel ? "No videos found for this channel." : "Choose a channel to view its archive."}</span>
                            </div>
                        {/if}
                    </div>
                </div>
            {:else if scanning && (!results[activeTab] || results[activeTab].length === 0)}
                <div class="loading-state" aria-live="polite">
                    <span class="loading-spinner" aria-hidden="true"></span>
                    <span>Scanning archive…</span>
                </div>
            {:else}
                <div class="table-shell">
                <table class="recovery-table">
                    <thead>
                        <tr>
                            <th class="selection-column">
                                <input
                                    type="checkbox"
                                    checked={allSelected}
                                    on:change={toggleAll}
                                    disabled={selectableItems.length === 0}
                                    class="recovery-checkbox"
                                    aria-label="Select all available recovery items"
                                />
                            </th>
                            <th>ID</th>
                            <th>Filename / target path</th>
                            <th>Info</th>
                            <th class="action-column">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each results[activeTab] || [] as item}
                            <tr class:selected-row={selectedPaths.has(item.path)}>
                                <td class="selection-column">
                                    <input
                                        type="checkbox"
                                        checked={selectedPaths.has(item.path)}
                                        on:change={() => toggleSelect(item)}
                                        disabled={!canDeleteItem(item)}
                                        title={!itemPath(item) ? "No media path is available" : !canDeleteItem(item) ? "Enable Destruct Mode to select this source file" : "Select item"}
                                        class="recovery-checkbox"
                                        aria-label={`Select ${previewName(item)}`}
                                    />
                                </td>
                                <td class="item-id">{item.video_id}</td>
                                <td class="item-name" title={item.path}>
                                    {item.filename || item.path}
                                </td>
                                <td>
                                    {item.size_mb
                                        ? item.size_mb + " MB"
                                        : item.ta_source || "-"}
                                    {#if item.requires_destruct}
                                        <span class="source-flag">Source</span>
                                    {/if}
                                </td>
                                <td class="action-column">
                                    <div class="row-actions">
                                        <button
                                            class="row-action preview-action"
                                            on:click={() => openPreview(item)}
                                            disabled={!itemPath(item)}
                                            aria-label={`Preview ${previewName(item)}`}
                                            title={itemPath(item) ? "Watch or listen" : "No media path is available"}
                                        >
                                            <i class="bi bi-play-circle" aria-hidden="true"></i>
                                            Preview
                                        </button>
                                        {#if activeTab === "unindexed"}
                                            <button
                                                class="row-action"
                                                on:click={() =>
                                                    recoverFile(item.path)}
                                                disabled={!itemPath(item)}
                                                ><i class="bi bi-plus-circle" aria-hidden="true"></i> Recover</button
                                            >
                                        {:else if activeTab === "redundant"}
                                            <button
                                                class="row-action action-danger"
                                                on:click={() =>
                                                    deleteFile(item)}
                                                disabled={!canDeleteItem(item)}
                                                ><i class="bi bi-trash" aria-hidden="true"></i> Delete</button
                                            >
                                        {:else if activeTab === "lost"}
                                            <button
                                                class="row-action"
                                                on:click={() => forceFile(item.path)}
                                                disabled={!itemPath(item)}
                                                ><i class="bi bi-lightning-fill" aria-hidden="true"></i> Force</button
                                            >
                                            <button
                                                class="row-action action-danger"
                                                on:click={() =>
                                                    deleteFile(item)}
                                                disabled={!canDeleteItem(item)}
                                                ><i class="bi bi-trash" aria-hidden="true"></i> Delete</button
                                            >
                                        {:else}
                                            <button
                                                class="row-action"
                                                on:click={() => rescueFile(item.path)}
                                                disabled={!itemPath(item)}
                                                ><i class="bi bi-rescue-ambulance" aria-hidden="true"></i> Rescue</button
                                            >
                                        {/if}
                                    </div>
                                </td>
                            </tr>
                        {/each}
                        {#if !results[activeTab]?.length}
                            <tr>
                                <td colspan="5" class="empty-cell">
                                    <i class="bi bi-folder2-open" aria-hidden="true"></i>
                                    <span>No items in this view.</span>
                                </td>
                            </tr>
                        {/if}
                    </tbody>
                </table>
                </div>
            {/if}
        </div>
    </div>

    {#if previewItem}
        <section
            class="preview-backdrop"
            role="dialog"
            aria-modal="true"
            aria-labelledby="recovery-preview-title"
        >
            <div bind:this={previewDialog} class="preview-dialog">
                <header class="preview-header">
                    <div class="preview-heading">
                        <div class="preview-eyebrow">
                            <i class="bi bi-play-btn" aria-hidden="true"></i>
                            Media preview
                        </div>
                        <h3
                            id="recovery-preview-title"
                            title={previewName(previewItem)}
                        >
                            {previewName(previewItem)}
                        </h3>
                    </div>
                    <button
                        bind:this={previewCloseButton}
                        class="preview-close"
                        on:click={() => closePreview()}
                        aria-label="Close media preview"
                        title="Close media preview"
                    >
                        <i class="bi bi-x-lg" aria-hidden="true"></i>
                    </button>
                </header>

                <div class="preview-body">
                    <div class="preview-context" aria-label="Media context">
                        <span>{activeTab === "channels" ? "Channel archive" : activeTab}</span>
                        {#if previewItem.video_id}
                            <span>ID {previewItem.video_id}</span>
                        {/if}
                        {#if previewItem.channel || selectedChannel}
                            <span>{previewItem.channel || selectedChannel}</span>
                        {/if}
                    </div>

                    <div class="preview-mode-switch" role="group" aria-label="Preview mode">
                        <button
                            class:active={previewMode === "video"}
                            aria-pressed={previewMode === "video"}
                            on:click={() => void startVideoPreview()}
                        >
                            <i class="bi bi-play-btn" aria-hidden="true"></i>
                            Watch
                        </button>
                        <button
                            class:active={previewMode === "audio"}
                            aria-pressed={previewMode === "audio"}
                            on:click={() => void startAudioPreview()}
                        >
                            <i class="bi bi-headphones" aria-hidden="true"></i>
                            Listen only
                        </button>
                    </div>

                    <div class="preview-media-frame" class:audio-mode={previewMode === "audio"}>
                        {#if previewLoading}
                            <div class="preview-loading" aria-live="polite">
                                <span class="preview-spinner"></span>
                                Loading archive media…
                            </div>
                        {/if}

                        {#if previewUrl}
                            {#if previewMode === "audio"}
                                <audio
                                    bind:this={previewElement}
                                    class="preview-audio"
                                    src={previewUrl}
                                    controls
                                    preload="metadata"
                                    on:loadedmetadata={handlePreviewReady}
                                    on:canplay={handlePreviewReady}
                                    on:error={handlePreviewError}
                                >
                                    Your browser cannot play this audio preview.
                                </audio>
                            {:else}
                                <!-- svelte-ignore a11y_media_has_caption -->
                                <video
                                    bind:this={previewElement}
                                    class="preview-media"
                                    src={previewUrl}
                                    controls
                                    playsinline
                                    preload="metadata"
                                    on:loadedmetadata={handlePreviewReady}
                                    on:canplay={handlePreviewReady}
                                    on:error={handlePreviewError}
                                >
                                    Your browser cannot play this archive media.
                                </video>
                            {/if}
                        {/if}
                    </div>

                    {#if previewError}
                        <div class="preview-error" role="alert">
                            <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
                            <div>
                                <strong>Preview unavailable</strong>
                                <p>{previewError}</p>
                            </div>
                        </div>
                    {:else}
                        <p class="preview-hint">
                            {previewMode === "audio"
                                ? "Listen only converts the first audio track to a temporary stream for browser compatibility."
                                : "Watch streams the original file without changing it. Use Listen only when the browser cannot decode the video."}
                            Nothing is recovered, moved, or deleted by previewing it.
                        </p>
                    {/if}

                    <div class="preview-path">
                        <span>Source file</span>
                        <code title={previewItem.path}>{previewItem.path}</code>
                    </div>
                </div>

                <footer class="preview-footer">
                    <button class="preview-done" on:click={() => closePreview()}>
                        Close preview
                    </button>
                </footer>
            </div>
        </section>
    {/if}
</div>

<style>
    .recovery-overlay {
        --canvas: #3e4637;
        --frame: #292d23;
        --surface: #434b3c;
        --row: #4c5844;
        --row-hover: #5a6452;
        --selected: #52604d;
        --rule: #8a9282;
        --rule-dim: #66705f;
        --text: #eef1e9;
        --text-secondary: #e0e6da;
        --muted: #d6ddd0;
        --danger-surface: #4a2e28;
        --danger-hover: #5a3830;
        --danger-rule: #8b5a4e;
        --danger-text: #f0d7cf;
        position: fixed;
        inset: 0;
        z-index: 100;
        display: grid;
        place-items: center;
        padding: 8px;
        color: var(--text);
        background: rgb(34 39 30 / 92%);
        font-family: Tahoma, Verdana, Arial, sans-serif;
        font-size: 12px;
        line-height: 1.35;
    }

    .recovery-overlay :global(*) {
        box-sizing: border-box;
    }

    .recovery-overlay button,
    .recovery-overlay select,
    .recovery-overlay input {
        font: inherit;
    }

    .recovery-overlay button,
    .recovery-overlay select {
        border-radius: 0;
    }

    .recovery-overlay button:focus-visible,
    .recovery-overlay select:focus-visible,
    .recovery-overlay input:focus-visible,
    .preview-media:focus-visible,
    .preview-audio:focus-visible {
        outline: 2px solid var(--text);
        outline-offset: -2px;
    }

    .recovery-workspace {
        display: flex;
        width: min(1040px, 100%);
        height: min(92dvh, 900px);
        min-height: 360px;
        flex-direction: column;
        overflow: hidden;
        background: var(--canvas);
        border: 1px solid var(--rule);
    }

    .recovery-header {
        display: flex;
        min-height: 40px;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 5px 7px 5px 10px;
        background: var(--frame);
        border-bottom: 1px solid var(--rule);
    }

    .recovery-heading {
        display: flex;
        min-width: 0;
        align-items: center;
        gap: 7px;
    }

    .recovery-heading > i {
        color: var(--text-secondary);
        font-size: 14px;
    }

    .recovery-heading h2 {
        margin: 0;
        color: var(--text);
        font-size: 14px;
        font-weight: 700;
    }

    .destruct-state,
    .source-flag {
        display: inline-flex;
        align-items: center;
        min-height: 20px;
        padding: 2px 6px;
        color: var(--danger-text);
        background: var(--danger-surface);
        border: 1px solid var(--danger-rule);
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
    }

    .icon-button {
        display: inline-grid;
        width: 28px;
        height: 28px;
        flex: 0 0 auto;
        padding: 0;
        place-items: center;
        color: var(--text-secondary);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        cursor: pointer;
    }

    .icon-button:hover:not(:disabled) {
        color: var(--text);
        background: var(--row-hover);
        border-color: var(--rule);
    }

    .notice {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        padding: 7px 9px;
        border-bottom: 1px solid;
        font-size: 11px;
    }

    .notice-danger,
    .operation-error {
        color: var(--danger-text);
        background: var(--danger-surface);
        border-color: var(--danger-rule);
    }

    .notice > i {
        margin-top: 1px;
        font-size: 13px;
    }

    .notice-copy {
        display: grid;
        min-width: 0;
        flex: 1;
        gap: 2px;
    }

    .notice-copy strong {
        font-size: 11px;
    }

    .permission-detail {
        color: #dbbdb4;
    }

    .notice-action {
        flex: 0 0 auto;
        margin-left: auto;
    }

    .operation-error {
        padding: 6px 10px;
        border-bottom: 1px solid var(--danger-rule);
        font-size: 11px;
    }

    .recovery-toolbar {
        display: flex;
        min-height: 44px;
        align-items: center;
        justify-content: space-between;
        gap: 8px 16px;
        padding: 6px 8px;
        background: var(--surface);
        border-bottom: 1px solid var(--rule);
    }

    .toolbar-actions,
    .toolbar-status,
    .row-actions {
        display: flex;
        align-items: center;
        gap: 6px;
    }

    .toolbar-status {
        gap: 12px;
    }

    .button {
        display: inline-flex;
        min-height: 29px;
        align-items: center;
        justify-content: center;
        gap: 6px;
        padding: 4px 9px;
        color: var(--text);
        background: var(--row);
        border: 1px solid var(--rule);
        font-size: 11px;
        font-weight: 700;
        cursor: pointer;
    }

    .button:hover:not(:disabled) {
        background: var(--row-hover);
    }

    .button-primary {
        color: var(--frame);
        background: var(--text-secondary);
        border-color: var(--text);
    }

    .button-primary:hover:not(:disabled) {
        background: var(--text);
    }

    .button-danger,
    .button-danger-subtle {
        color: var(--danger-text);
        background: var(--danger-surface);
        border-color: var(--danger-rule);
    }

    .button-danger:hover:not(:disabled),
    .button-danger-subtle:hover:not(:disabled) {
        background: var(--danger-hover);
    }

    .button:disabled,
    .icon-button:disabled,
    .row-action:disabled,
    .recovery-checkbox:disabled {
        cursor: not-allowed;
        opacity: 0.42;
    }

    .destruct-toggle {
        display: inline-flex;
        min-height: 28px;
        align-items: center;
        gap: 6px;
        padding: 3px 7px;
        color: var(--text-secondary);
        background: var(--frame);
        border: 1px solid var(--rule-dim);
        cursor: pointer;
        user-select: none;
    }

    .destruct-toggle:has(input:checked) {
        color: var(--danger-text);
        background: var(--danger-surface);
        border-color: var(--danger-rule);
    }

    .destruct-toggle span {
        display: inline-flex;
        align-items: center;
        gap: 5px;
    }

    .recovery-checkbox {
        width: 13px;
        height: 13px;
        margin: 0;
        accent-color: var(--text-secondary);
    }

    .destruct-toggle .recovery-checkbox {
        accent-color: var(--danger-rule);
    }

    .scan-status {
        display: inline-flex;
        min-height: 28px;
        align-items: stretch;
        border: 1px solid var(--rule-dim);
    }

    .scan-status span,
    .scan-status strong {
        display: flex;
        align-items: center;
        padding: 3px 7px;
    }

    .scan-status span {
        color: var(--muted);
        background: var(--frame);
        border-right: 1px solid var(--rule-dim);
    }

    .scan-status strong {
        min-width: 62px;
        color: var(--text);
        background: var(--row);
        font-weight: 700;
        text-transform: capitalize;
    }

    .recovery-tabs {
        display: flex;
        flex: 0 0 auto;
        overflow-x: auto;
        background: var(--frame);
        border-bottom: 1px solid var(--rule);
        scrollbar-width: thin;
        white-space: nowrap;
    }

    .recovery-tabs button {
        display: inline-flex;
        min-width: 104px;
        min-height: 32px;
        flex: 0 0 auto;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin: 0;
        padding: 5px 8px;
        color: var(--muted);
        background: var(--frame);
        border: 0;
        border-bottom: 2px solid transparent;
        border-right: 1px solid var(--rule-dim);
        cursor: pointer;
        font-size: 11px;
        font-weight: 700;
        text-transform: capitalize;
    }

    .recovery-tabs button:hover,
    .recovery-tabs button.active {
        color: var(--text);
        background: var(--selected);
    }

    .recovery-tabs button.active {
        border-bottom-color: var(--text-secondary);
    }

    .tab-count {
        min-width: 20px;
        padding: 1px 4px;
        color: var(--text-secondary);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        text-align: center;
    }

    .active .tab-count {
        color: var(--text);
        background: var(--frame);
        border-color: var(--rule);
    }

    .workspace-content {
        min-height: 0;
        flex: 1;
        overflow: hidden;
        padding: 7px;
        background: var(--canvas);
    }

    .channel-view {
        display: grid;
        height: 100%;
        min-height: 0;
        grid-template-rows: auto minmax(0, 1fr);
        gap: 7px;
    }

    .channel-selector {
        display: flex;
        align-items: flex-end;
        gap: 5px;
    }

    .field-group {
        display: grid;
        min-width: 0;
        flex: 1;
        gap: 3px;
    }

    .field-group label {
        color: var(--muted);
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
    }

    .field-group select {
        width: 100%;
        height: 29px;
        padding: 3px 7px;
        color: var(--text);
        background: var(--frame);
        border: 1px solid var(--rule-dim);
        cursor: pointer;
    }

    .selector-refresh {
        height: 29px;
    }

    .table-shell {
        height: 100%;
        min-height: 0;
        overflow: auto;
        background: var(--frame);
        border: 1px solid var(--rule);
    }

    .channel-table-shell {
        min-height: 150px;
    }

    .recovery-table {
        width: 100%;
        min-width: 680px;
        color: var(--text-secondary);
        border-collapse: collapse;
        table-layout: fixed;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 11px;
        text-align: left;
    }

    .recovery-table thead {
        position: sticky;
        z-index: 2;
        top: 0;
        color: var(--muted);
        background: var(--frame);
    }

    .recovery-table th,
    .recovery-table td {
        height: 30px;
        overflow: hidden;
        padding: 4px 7px;
        border-right: 1px solid var(--rule-dim);
        border-bottom: 1px solid var(--rule-dim);
        text-overflow: ellipsis;
        vertical-align: middle;
        white-space: nowrap;
    }

    .recovery-table th:last-child,
    .recovery-table td:last-child {
        border-right: 0;
    }

    .recovery-table th {
        height: 27px;
        color: var(--muted);
        font-family: Tahoma, Verdana, Arial, sans-serif;
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
    }

    .recovery-table tbody tr {
        background: var(--row);
    }

    .recovery-table tbody tr:nth-child(even) {
        background: #485240;
    }

    .recovery-table tbody tr:hover,
    .recovery-table tbody tr.selected-row {
        background: var(--row-hover);
    }

    .recovery-table tbody tr.selected-row {
        background: var(--selected);
    }

    .selection-column {
        width: 38px;
        text-align: center;
    }

    .action-column {
        width: 260px;
        text-align: right;
    }

    .item-id {
        width: 110px;
        color: var(--text);
        font-weight: 700;
    }

    .item-name {
        color: var(--text);
    }

    .source-flag {
        min-height: 17px;
        margin-left: 6px;
        padding: 1px 4px;
        font-size: 9px;
    }

    .row-actions {
        justify-content: flex-end;
        gap: 4px;
    }

    .row-action {
        display: inline-flex;
        min-height: 22px;
        align-items: center;
        gap: 4px;
        padding: 2px 5px;
        color: var(--text-secondary);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        cursor: pointer;
        font-family: Tahoma, Verdana, Arial, sans-serif;
        font-size: 10px;
        font-weight: 700;
    }

    .row-action:hover:not(:disabled) {
        color: var(--text);
        background: var(--selected);
        border-color: var(--rule);
    }

    .row-action.action-danger {
        color: var(--danger-text);
        background: var(--danger-surface);
        border-color: var(--danger-rule);
    }

    .row-action.action-danger:hover:not(:disabled) {
        background: var(--danger-hover);
    }

    .icon-row-action {
        width: 24px;
        justify-content: center;
        padding-inline: 0;
    }

    .empty-state,
    .loading-state,
    .empty-cell {
        color: var(--muted);
    }

    .empty-state,
    .loading-state {
        display: flex;
        width: 100%;
        height: 100%;
        min-height: 150px;
        align-items: center;
        justify-content: center;
        gap: 8px;
        background: var(--frame);
        font-size: 11px;
    }

    .empty-state i,
    .empty-cell i {
        font-size: 14px;
    }

    .empty-cell {
        height: 150px !important;
        background: var(--frame);
        text-align: center;
    }

    .empty-cell span {
        margin-left: 6px;
    }

    .loading-spinner,
    .preview-spinner {
        display: inline-block;
        width: 13px;
        height: 13px;
        flex: 0 0 auto;
        border: 2px solid var(--rule-dim);
        border-top-color: var(--text);
        border-radius: 50%;
        animation: recovery-spin 700ms linear infinite;
    }

    .preview-backdrop {
        position: fixed;
        inset: 0;
        z-index: 120;
        display: grid;
        padding: 12px;
        place-items: center;
        background: rgb(24 28 22 / 96%);
    }

    .preview-dialog {
        display: flex;
        width: min(900px, 100%);
        max-height: calc(100dvh - 24px);
        flex-direction: column;
        overflow: hidden;
        color: var(--text);
        background: var(--canvas);
        border: 1px solid var(--rule);
    }

    .preview-header {
        display: flex;
        min-height: 43px;
        align-items: flex-start;
        justify-content: space-between;
        gap: 10px;
        padding: 6px 7px 6px 9px;
        background: var(--frame);
        border-bottom: 1px solid var(--rule);
    }

    .preview-heading {
        min-width: 0;
    }

    .preview-eyebrow {
        display: flex;
        align-items: center;
        gap: 5px;
        color: var(--muted);
        font-size: 9px;
        font-weight: 700;
        text-transform: uppercase;
    }

    .preview-header h3 {
        overflow: hidden;
        margin: 2px 0 0;
        color: var(--text);
        font-size: 13px;
        font-weight: 700;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .preview-close {
        display: grid;
        width: 28px;
        height: 28px;
        flex: 0 0 auto;
        padding: 0;
        place-items: center;
        color: var(--text-secondary);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        cursor: pointer;
    }

    .preview-close:hover,
    .preview-done:hover {
        color: var(--text);
        background: var(--row-hover);
        border-color: var(--rule);
    }

    .preview-body {
        min-height: 0;
        overflow-y: auto;
        padding: 7px;
    }

    .preview-context {
        display: flex;
        overflow-x: auto;
        margin-bottom: 6px;
        white-space: nowrap;
    }

    .preview-context span {
        padding: 3px 7px;
        color: var(--text-secondary);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        border-right: 0;
        font-size: 9px;
        font-weight: 700;
        text-transform: uppercase;
    }

    .preview-context span:last-child {
        border-right: 1px solid var(--rule-dim);
    }

    .preview-mode-switch {
        display: flex;
        margin-bottom: 6px;
    }

    .preview-mode-switch button {
        display: inline-flex;
        min-height: 28px;
        align-items: center;
        gap: 5px;
        padding: 4px 8px;
        color: var(--muted);
        background: var(--frame);
        border: 1px solid var(--rule-dim);
        border-right: 0;
        cursor: pointer;
        font-size: 10px;
        font-weight: 700;
    }

    .preview-mode-switch button:last-child {
        border-right: 1px solid var(--rule-dim);
    }

    .preview-mode-switch button:hover,
    .preview-mode-switch button.active {
        color: var(--text);
        background: var(--selected);
    }

    .preview-mode-switch button.active {
        border-bottom-color: var(--text);
    }

    .preview-media-frame {
        position: relative;
        display: grid;
        min-height: 240px;
        overflow: hidden;
        place-items: center;
        background: #0c0f0b;
        border: 1px solid var(--rule-dim);
    }

    .preview-media {
        display: block;
        width: 100%;
        min-height: 240px;
        max-height: 56dvh;
        background: #0c0f0b;
    }

    .preview-media-frame.audio-mode {
        min-height: 92px;
    }

    .preview-audio {
        width: min(620px, calc(100% - 24px));
    }

    .preview-loading {
        position: absolute;
        z-index: 1;
        display: flex;
        align-items: center;
        gap: 7px;
        padding: 5px 7px;
        color: var(--text);
        background: var(--frame);
        border: 1px solid var(--rule);
        font-size: 11px;
        pointer-events: none;
    }

    .preview-hint {
        margin: 6px 0 0;
        color: var(--muted);
        font-size: 10px;
    }

    .preview-error {
        display: flex;
        gap: 7px;
        margin-top: 6px;
        padding: 6px 7px;
        color: var(--danger-text);
        background: var(--danger-surface);
        border: 1px solid var(--danger-rule);
        font-size: 11px;
    }

    .preview-error p {
        margin: 1px 0 0;
        color: #dbbdb4;
    }

    .preview-path {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        align-items: baseline;
        gap: 7px;
        margin-top: 6px;
        padding: 4px 6px;
        color: var(--muted);
        background: var(--surface);
        border: 1px solid var(--rule-dim);
        font-size: 9px;
        text-transform: uppercase;
    }

    .preview-path code {
        overflow: hidden;
        color: var(--text-secondary);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 10px;
        text-overflow: ellipsis;
        text-transform: none;
        white-space: nowrap;
    }

    .preview-footer {
        display: flex;
        justify-content: flex-end;
        padding: 6px 7px;
        background: var(--frame);
        border-top: 1px solid var(--rule);
    }

    .preview-done {
        min-height: 28px;
        padding: 4px 9px;
        color: var(--text);
        background: var(--row);
        border: 1px solid var(--rule);
        font-size: 11px;
        font-weight: 700;
        cursor: pointer;
    }

    .recovery-workspace,
    .preview-body,
    .table-shell,
    .recovery-tabs,
    .preview-context {
        scrollbar-color: var(--rule) var(--frame);
        scrollbar-width: thin;
    }

    .recovery-workspace :global(*::-webkit-scrollbar) {
        width: 7px;
        height: 7px;
    }

    .recovery-workspace :global(*::-webkit-scrollbar-track) {
        background: var(--frame);
    }

    .recovery-workspace :global(*::-webkit-scrollbar-thumb) {
        background: var(--rule-dim);
        border: 1px solid var(--frame);
    }

    .recovery-workspace :global(*::-webkit-scrollbar-thumb:hover) {
        background: var(--rule);
    }

    @keyframes recovery-spin {
        to {
            transform: rotate(360deg);
        }
    }

    @media (max-width: 720px) {
        .recovery-toolbar {
            align-items: stretch;
            flex-direction: column;
        }

        .toolbar-actions,
        .toolbar-status {
            justify-content: space-between;
        }

        .toolbar-actions .button {
            flex: 1;
        }

        .action-column {
            width: 240px;
        }
    }

    @media (max-width: 640px) {
        .recovery-overlay {
            padding: 0;
        }

        .recovery-workspace {
            width: 100%;
            height: 100dvh;
            min-height: 0;
            border-top: 0;
            border-bottom: 0;
        }

        .recovery-heading h2 {
            font-size: 13px;
        }

        .destruct-state {
            max-width: 130px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .workspace-content {
            padding: 5px;
        }

        .preview-backdrop {
            padding: 0;
        }

        .preview-dialog {
            width: 100%;
            height: 100dvh;
            max-height: none;
            border: 0;
        }

        .preview-body {
            flex: 1;
        }

        .preview-media-frame,
        .preview-media {
            min-height: 180px;
        }

        .preview-media-frame.audio-mode {
            min-height: 92px;
        }

        .preview-media {
            max-height: 48dvh;
        }
    }

    @media (max-width: 430px) {
        .toolbar-status {
            align-items: stretch;
            flex-direction: column;
            gap: 5px;
        }

        .destruct-toggle,
        .scan-status {
            width: 100%;
        }

        .scan-status strong {
            flex: 1;
        }

        .notice {
            flex-wrap: wrap;
        }

        .notice-action {
            width: 100%;
            margin-left: 21px;
        }

        .preview-path {
            grid-template-columns: 1fr;
            gap: 2px;
        }
    }

    @media (prefers-reduced-motion: reduce) {
        .loading-spinner,
        .preview-spinner {
            animation-duration: 1.8s;
        }
    }
</style>
