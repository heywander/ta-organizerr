<script lang="ts">
    import { onMount, onDestroy } from "svelte";
    import LogViewer from "./LogViewer.svelte";

    let videos: any[] = [];
    let loading = false;
    let page = 1;
    let total = 0;
    let unknown = 0;
    let pages = 1;
    let scanning = false;
    let scanRequested = false;
    let scanPoll: ReturnType<typeof setInterval> | null = null;
    let error: string | null = null;
    let pendingPaths = new Set<string>();
    let startedPaths = new Set<string>();

    function messageFrom(value: unknown): string {
        return value instanceof Error ? value.message : String(value);
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

    async function fetchVideos(p = 1) {
        loading = true;
        error = null;
        try {
            const res = await fetch(
                `/api/transcode/videos?page=${p}&per_page=100`,
            );
            const data = await readResponse(res);
            if (!Array.isArray(data.videos)) {
                throw new Error("Video response was not a list");
            }
            videos = data.videos;
            total = Number(data.total) || 0;
            unknown = Number(data.unknown) || 0;
            scanning = Boolean(data.scanning);
            pages = Math.max(1, Number(data.pages) || 1);
            page = Math.max(1, Number(data.page) || p);
            syncScanPoll();
        } catch (e) {
            console.error(e);
            videos = [];
            error = `Could not load available media: ${messageFrom(e)}`;
        } finally {
            loading = false;
        }
    }

    async function startTranscode(filepath: string) {
        if (pendingPaths.has(filepath) || startedPaths.has(filepath)) return;
        if (
            !confirm(
                "Start transcoding? This replaces the underlying archive source file in place and cannot be undone.",
            )
        )
            return;

        const nextPending = new Set(pendingPaths);
        nextPending.add(filepath);
        pendingPaths = nextPending;
        error = null;
        try {
            const res = await fetch("/api/transcode/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ filepath }),
            });
            const data = await readResponse(res);
            const nextStarted = new Set(startedPaths);
            nextStarted.add(filepath);
            startedPaths = nextStarted;
            alert(data.message || "Transcode started.");
        } catch (e) {
            error = `Could not start transcode: ${messageFrom(e)}`;
            alert(error);
        } finally {
            const remaining = new Set(pendingPaths);
            remaining.delete(filepath);
            pendingPaths = remaining;
        }
    }

    function stopScanPoll() {
        if (scanPoll !== null) {
            clearInterval(scanPoll);
            scanPoll = null;
        }
    }

    function syncScanPoll() {
        // Keep refreshing while the server probes files so finished rows
        // drop out of the queue without a manual refresh.
        if (scanning) {
            if (scanPoll === null) {
                scanPoll = setInterval(() => fetchVideos(page), 8000);
            }
        } else {
            stopScanPoll();
        }
    }

    async function scanCodecs() {
        if (scanRequested || scanning) return;
        if (
            !confirm(
                "Probe every linked video for its codecs? This reads file headers in the background and does not modify media.",
            )
        )
            return;
        scanRequested = true;
        error = null;
        try {
            const res = await fetch("/api/transcode/scan", { method: "POST" });
            const data = await readResponse(res);
            scanning = true;
            syncScanPoll();
            alert(data.message || "Codec scan started.");
        } catch (e) {
            error = `Could not start codec scan: ${messageFrom(e)}`;
            alert(error);
        } finally {
            scanRequested = false;
        }
    }

    function codecLabel(v: any): string {
        if (!v.video_codec && !v.audio_codec) return "not probed";
        return `${v.video_codec ?? "?"} / ${v.audio_codec ?? "?"}`;
    }

    function refreshVideos() {
        // A deliberate refresh clears local job markers; the server still owns
        // concurrency and validates every request.
        startedPaths = new Set();
        return fetchVideos(1);
    }

    onMount(() => {
        fetchVideos();
    });

    onDestroy(stopScanPoll);
</script>

<div class="transcode-page">
    <section class="panel transcode-summary" aria-labelledby="transcode-heading">
        <div>
            <h1 id="transcode-heading" class="section-title">
                <i class="bi bi-film" aria-hidden="true"></i>
                Transcode media
            </h1>
            <p class="section-description">
                {total} linked {total === 1 ? "file" : "files"} not yet H.264/AAC{#if unknown > 0}
                    ({unknown} not probed yet){/if}. Transcoding replaces the source file in place.
            </p>
        </div>
        <div class="transcode-summary__actions">
            <button
                type="button"
                class="button"
                on:click={scanCodecs}
                disabled={scanning || scanRequested}
                title="Probe linked files with ffprobe so only media that needs converting is listed"
            >
                <i class="bi bi-search" aria-hidden="true"></i>
                {scanning ? "Scanning codecs…" : "Scan codecs"}
            </button>
            <button
                type="button"
                class="button"
                on:click={refreshVideos}
                disabled={loading}
            >
                <i class="bi bi-arrow-clockwise" aria-hidden="true"></i>
                {loading ? "Refreshing…" : "Refresh list"}
            </button>
        </div>
    </section>

    {#if error}
        <div class="notice notice--error" role="alert">
            {error}
        </div>
    {/if}

    <div class="transcode-layout">
        <section class="panel media-list" aria-labelledby="eligible-media-heading">
            <header class="section-header">
                <div>
                    <h2 id="eligible-media-heading" class="section-title">
                        <i class="bi bi-folder2-open" aria-hidden="true"></i>
                        Eligible files
                    </h2>
                    <p class="section-description">
                        Only linked archive media that still needs converting is listed. Unprobed files sort last.
                    </p>
                </div>
            </header>

            <div class="media-list__table-wrap">
                <table class="media-list__table">
                    <thead>
                        <tr>
                            <th class="channel-column">Channel</th>
                            <th class="published-column">Published</th>
                            <th>Title</th>
                            <th class="codec-column">Codecs</th>
                            <th class="action-column">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#if loading}
                            <tr class="state-row">
                                <td colspan="5" class="table-message">Loading media…</td>
                            </tr>
                        {:else if videos.length === 0}
                            <tr class="state-row">
                                <td colspan="5" class="table-message">
                                    {#if unknown > 0}
                                        No probed videos need transcoding. Run a codec scan to check the rest.
                                    {:else}
                                        Every linked video is already H.264/AAC.
                                    {/if}
                                </td>
                            </tr>
                        {:else}
                            {#each videos as v}
                                <tr>
                                    <td class="channel-column" title={v.channel}>
                                        <span class="mobile-cell-label">Channel</span>
                                        {v.channel}
                                    </td>
                                    <td class="published-column tabular-nums">
                                        <span class="mobile-cell-label">Published</span>
                                        {v.published}
                                    </td>
                                    <td class="title-column" data-label="Title" title={v.title}>
                                        <span class="mobile-cell-label">Title</span>
                                        <i class="bi bi-file-earmark-play" aria-hidden="true"></i>
                                        <span>{v.title}</span>
                                    </td>
                                    <td class="codec-column" title={codecLabel(v)}>
                                        <span class="mobile-cell-label">Codecs</span>
                                        {codecLabel(v)}
                                    </td>
                                    <td class="action-column">
                                        <button
                                            type="button"
                                            class="button media-list__action"
                                            on:click={() => startTranscode(v.symlink)}
                                            disabled={pendingPaths.has(v.symlink) || startedPaths.has(v.symlink)}
                                            aria-label={`Transcode ${v.title}`}
                        >
                                            {#if pendingPaths.has(v.symlink)}
                                                <i class="bi bi-arrow-repeat" aria-hidden="true"></i>
                                                Starting…
                                            {:else if startedPaths.has(v.symlink)}
                                                <i class="bi bi-check2" aria-hidden="true"></i>
                                                Started
                                            {:else}
                                                <i class="bi bi-play-fill" aria-hidden="true"></i>
                                                Transcode
                                            {/if}
                                        </button>
                                    </td>
                                </tr>
                            {/each}
                        {/if}
                    </tbody>
                </table>
            </div>

            {#if pages > 1}
                <nav class="media-list__pagination" aria-label="Media pages">
                    <button
                        type="button"
                        disabled={loading || page === 1}
                        on:click={() => fetchVideos(page - 1)}
                        class="button-quiet"
                        aria-label="Previous page"
                    >
                        <i class="bi bi-chevron-left" aria-hidden="true"></i>
                    </button>
                    <span>Page {page} of {pages}</span>
                    <button
                        type="button"
                        disabled={loading || page === pages}
                        on:click={() => fetchVideos(page + 1)}
                        class="button-quiet"
                        aria-label="Next page"
                        >
                        <i class="bi bi-chevron-right" aria-hidden="true"></i>
                    </button>
                </nav>
            {/if}
        </section>

        <aside>
            <LogViewer endpoint="/api/transcode/logs" title="Transcode log" />
        </aside>
    </div>
</div>

<style>
    .transcode-page {
        display: flex;
        flex-direction: column;
        gap: 12px;
    }

    .transcode-summary {
        display: flex;
        min-height: 62px;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 10px 12px;
        background: var(--surface-raised) !important;
    }

    .transcode-summary__actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
    }

    .transcode-layout {
        display: grid;
        grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
        gap: 12px;
    }

    .media-list {
        display: flex;
        min-height: 520px;
        flex-direction: column;
        overflow: hidden;
    }

    .media-list__table-wrap {
        max-height: 500px;
        flex: 1;
        overflow: auto;
    }

    .media-list__table {
        width: 100%;
        table-layout: fixed;
        text-align: left;
        font-size: 11px;
    }

    .media-list__table thead {
        position: sticky;
        top: 0;
        z-index: 2;
    }

    .media-list__table th,
    .media-list__table td {
        height: 32px;
        padding: 5px 9px;
        border-bottom: 1px solid var(--line-soft);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .channel-column {
        width: 25%;
        color: var(--text-soft);
    }

    .published-column {
        width: 94px;
        color: var(--text-muted);
    }

    .title-column i {
        margin-right: 7px;
        color: var(--accent);
    }

    .title-column span {
        color: var(--text);
        font-weight: 600;
    }

    .codec-column {
        width: 118px;
        color: var(--text-muted);
        font-variant-numeric: tabular-nums;
    }

    .action-column {
        width: 112px;
        text-align: right;
    }

    .media-list__action {
        min-height: 26px;
        padding: 3px 7px;
        font-size: 10px;
    }

    .table-message {
        height: 112px !important;
        color: var(--text-muted);
        text-align: center;
    }

    .mobile-cell-label {
        display: none;
    }

    .media-list__pagination {
        display: flex;
        min-height: 38px;
        align-items: center;
        justify-content: center;
        gap: 8px;
        border-top: 1px solid var(--line-soft);
        color: var(--text-muted);
        font-size: 10px;
    }

    .media-list__pagination button {
        min-height: 26px;
        padding: 3px 7px;
    }

    @media (max-width: 900px) {
        .transcode-layout {
            grid-template-columns: minmax(0, 1fr);
        }
    }

    @media (max-width: 600px) {
        .transcode-summary {
            align-items: stretch;
            flex-direction: column;
        }

        .transcode-summary__actions {
            flex-direction: column;
        }

        .transcode-summary .button {
            width: 100%;
        }

        .transcode-page :global(button) {
            min-width: 44px;
            min-height: 44px;
        }

        .media-list {
            min-height: 0;
            overflow: visible;
        }

        .media-list__table-wrap {
            max-height: none;
            overflow: visible;
        }

        .media-list__table,
        .media-list__table tbody {
            display: block;
        }

        .media-list__table thead {
            display: none;
        }

        .media-list__table tbody {
            display: grid;
            gap: 8px;
            padding: 8px;
        }

        .media-list__table tr {
            display: grid;
            min-width: 0;
            grid-template-areas:
                "title"
                "channel"
                "published"
                "codecs"
                "action";
            grid-template-columns: minmax(0, 1fr);
            border: 1px solid var(--line-soft);
            background: var(--page-deep);
        }

        .media-list__table th,
        .media-list__table td {
            width: auto;
            height: auto;
            min-width: 0;
            padding: 7px 9px;
            overflow: visible;
            text-overflow: clip;
            white-space: normal;
        }

        .mobile-cell-label {
            display: block;
            margin-bottom: 2px;
            color: var(--text-muted);
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }

        .title-column {
            display: block;
            grid-area: title;
            border-bottom: 1px solid var(--line-soft) !important;
            overflow-wrap: anywhere;
        }

        .title-column span {
            min-width: 0;
            font-size: 13px;
            line-height: 1.35;
            overflow-wrap: anywhere;
        }

        .title-column .mobile-cell-label {
            margin-bottom: 2px;
            color: var(--text-muted);
            font-size: 10px;
            line-height: normal;
        }

        .channel-column {
            grid-area: channel;
            overflow-wrap: anywhere;
        }

        .published-column {
            display: block;
            grid-area: published;
        }

        .codec-column {
            display: block;
            grid-area: codecs;
        }

        .action-column {
            grid-area: action;
            padding-top: 3px !important;
            text-align: left;
        }

        .media-list__action {
            width: 100%;
            min-height: 44px;
            font-size: 12px;
        }

        .media-list__table .state-row {
            display: block;
        }

        .state-row .table-message {
            display: block;
            height: auto !important;
            padding: 30px 12px;
        }

        .media-list__pagination {
            min-height: 52px;
        }

        .media-list__pagination button {
            min-width: 44px;
            min-height: 44px;
        }
    }
</style>
