<script lang="ts">
    import { onMount } from "svelte";

    export let videos: any[] = [];
    export let loading = false;

    const MOBILE_QUERY = "(max-width: 600px)";
    const MOBILE_BATCH_SIZE = 60;

    let searchTerm = "";
    let statusFilter = "";
    let mobileView =
        typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;
    let mobileVisibleCount = MOBILE_BATCH_SIZE;

    function isHealthyStatus(status: unknown): boolean {
        return status === "linked" || status === "adopted";
    }

    $: channels = [
        ...new Set(
            videos
                .map((video) => String(video.channel || "").trim())
                .filter(Boolean),
        ),
    ].sort((a, b) => a.localeCompare(b));
    let channelFilter = "";

    function resetMobileBatch() {
        mobileVisibleCount = MOBILE_BATCH_SIZE;
    }

    function loadMoreVideos() {
        mobileVisibleCount += MOBILE_BATCH_SIZE;
    }

    onMount(() => {
        const mediaQuery = window.matchMedia(MOBILE_QUERY);
        const updateMobileView = (matches: boolean) => {
            mobileView = matches;
            resetMobileBatch();
        };
        const handleChange = (event: MediaQueryListEvent) =>
            updateMobileView(event.matches);

        updateMobileView(mediaQuery.matches);
        mediaQuery.addEventListener("change", handleChange);
        return () => mediaQuery.removeEventListener("change", handleChange);
    });

    $: filteredVideos = videos.filter((v) => {
        const matchSearch =
            searchTerm === "" ||
            String(v.title || "")
                .toLowerCase()
                .includes(searchTerm.toLowerCase()) ||
            String(v.video_id || "").includes(searchTerm);
        const matchStatus =
            statusFilter === "" ||
            (statusFilter === "problem"
                ? !isHealthyStatus(v.status)
                : statusFilter === "healthy"
                  ? isHealthyStatus(v.status)
                  : v.status === statusFilter);
        const matchChannel =
            channelFilter === "" || String(v.channel || "") === channelFilter;
        return matchSearch && matchStatus && matchChannel;
    });
    $: renderedVideos = mobileView
        ? filteredVideos.slice(0, mobileVisibleCount)
        : filteredVideos;
</script>

<section class="panel video-index" aria-labelledby="video-index-heading">
    <header class="section-header video-index__header">
        <div>
            <h2 id="video-index-heading" class="section-title">
                <i class="bi bi-collection-play" aria-hidden="true"></i>
                Library index
            </h2>
            <p class="section-description">
                {#if mobileView}
                    {renderedVideos.length} of {filteredVideos.length} matching shown · {videos.length} total
                {:else}
                    {filteredVideos.length} of {videos.length} videos shown
                {/if}
            </p>
        </div>

        <div class="video-index__filters" role="group" aria-label="Video filters">
            <select
                bind:value={statusFilter}
                on:change={resetMobileBatch}
                aria-label="Filter by link status"
                title="Filter by link status"
            >
                <option value="">All statuses</option>
                <option value="healthy">Linked or adopted</option>
                <option value="problem">Missing or error</option>
            </select>

            <select
                bind:value={channelFilter}
                on:change={resetMobileBatch}
                aria-label="Filter by channel"
                title="Filter by channel"
            >
                <option value="">All channels</option>
                {#each channels as ch}
                    <option value={ch}>{ch}</option>
                {/each}
            </select>

            <label class="video-index__search">
                <span class="sr-only">Search videos</span>
                <i class="bi bi-search" aria-hidden="true"></i>
                <input
                    type="text"
                    bind:value={searchTerm}
                    on:input={resetMobileBatch}
                    placeholder="Search title or ID"
                />
            </label>
        </div>
    </header>

    <div class="video-index__table-wrap">
        <table class="video-index__table">
            <thead>
                <tr>
                    <th class="status-column"><span class="sr-only">Status</span></th>
                    <th class="published-column">Published</th>
                    <th class="channel-column">Channel</th>
                    <th>Title</th>
                    <th class="id-column">Video ID</th>
                </tr>
            </thead>
            <tbody>
                {#if loading}
                    <tr>
                        <td colspan="5" class="table-message">Loading library…</td>
                    </tr>
                {:else if filteredVideos.length === 0}
                    <tr>
                        <td colspan="5" class="table-message">No videos match these filters.</td>
                    </tr>
                {:else}
                    {#each renderedVideos as v}
                        <tr>
                            <td class="status-column">
                                <span
                                    class:status-dot--problem={!isHealthyStatus(v.status)}
                                    class="status-dot"
                                    role="img"
                                    aria-label={v.status === "adopted" ? "Adopted real file" : isHealthyStatus(v.status) ? "Linked" : "Missing or errored"}
                                    title={v.status === "adopted" ? "Adopted real file" : isHealthyStatus(v.status) ? "Linked" : "Missing or errored"}
                                ></span>
                            </td>
                            <td class="published-column tabular-nums">
                                <span class="mobile-cell-label">Published: </span>{v.published}
                            </td>
                            <td class="channel-column" title={v.channel}>
                                <span class="mobile-cell-label">Channel: </span>{v.channel}
                            </td>
                            <td class="title-column" title={v.title}>
                                <span class="mobile-cell-label">Title: </span>
                                <i class="bi bi-file-earmark-play" aria-hidden="true"></i>
                                <span>{v.title}</span>
                            </td>
                            <td class="id-column" title={v.video_id}>
                                <span class="mobile-cell-label">ID: </span>{v.video_id}
                            </td>
                        </tr>
                    {/each}
                {/if}
            </tbody>
        </table>
    </div>

    {#if mobileView && renderedVideos.length < filteredVideos.length}
        <div class="video-index__load-more">
            <button class="button" type="button" on:click={loadMoreVideos}>
                Load more
                <span>{renderedVideos.length} of {filteredVideos.length} shown</span>
            </button>
        </div>
    {/if}
</section>

<style>
    .video-index {
        display: flex;
        min-height: 420px;
        height: 100%;
        flex-direction: column;
        overflow: hidden;
    }

    .video-index__header {
        align-items: flex-start;
    }

    .video-index__filters {
        display: flex;
        max-width: 100%;
        gap: 6px;
    }

    .video-index__filters select {
        width: auto;
        max-width: 150px;
        padding: 4px 24px 4px 8px;
        font-size: 11px;
    }

    .video-index__search {
        position: relative;
        min-width: 170px;
    }

    .video-index__search i {
        position: absolute;
        top: 50%;
        left: 9px;
        z-index: 1;
        color: var(--text-muted);
        font-size: 11px;
        transform: translateY(-50%);
        pointer-events: none;
    }

    .video-index__search input {
        width: 100%;
        padding: 4px 8px 4px 28px;
        font-size: 11px;
    }

    .video-index__table-wrap {
        max-height: 500px;
        flex: 1;
        overflow: auto;
    }

    .video-index__table {
        width: 100%;
        table-layout: fixed;
        text-align: left;
        font-size: 11px;
    }

    .video-index__table thead {
        position: sticky;
        top: 0;
        z-index: 2;
    }

    .video-index__table th,
    .video-index__table td {
        height: 31px;
        padding: 5px 9px;
        border-bottom: 1px solid var(--line-soft);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .video-index__table th {
        height: 29px;
    }

    .status-column {
        width: 30px;
        padding-right: 2px !important;
        text-align: center;
    }

    .status-dot--problem {
        border-color: color-mix(in srgb, var(--danger) 75%, var(--text-soft));
        background: var(--danger);
    }

    .published-column {
        width: 92px;
        color: var(--text-muted);
    }

    .channel-column {
        width: 27%;
        color: var(--text-soft);
    }

    .title-column {
        color: var(--text);
    }

    .title-column i {
        margin-right: 7px;
        color: var(--accent);
    }

    .title-column span {
        font-weight: 600;
    }

    .id-column {
        width: 118px;
        color: var(--text-muted);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 10px;
        user-select: all;
    }

    .table-message {
        height: 112px !important;
        color: var(--text-muted);
        text-align: center;
    }

    .mobile-cell-label {
        display: none;
    }

    @media (max-width: 720px) {
        .video-index__header {
            flex-direction: column;
        }

        .video-index__filters {
            width: 100%;
            overflow-x: auto;
        }

        .video-index__search {
            min-width: 180px;
            flex: 1;
        }

        .id-column {
            display: none;
        }

        .channel-column {
            width: 34%;
        }
    }

    @media (max-width: 500px) {
        .video-index__filters {
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            overflow: visible;
        }

        .video-index__filters select {
            width: 100%;
            max-width: none;
        }

        .video-index__search {
            min-width: 0;
            grid-column: 1 / -1;
        }

        .published-column {
            display: none;
        }

        .channel-column {
            width: 38%;
        }
    }

    .video-index__load-more {
        display: none;
    }

    @media (max-width: 600px) {
        .video-index {
            min-height: 0;
        }

        .video-index__filters {
            display: grid;
            width: 100%;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            overflow: visible;
        }

        .video-index__filters select,
        .video-index__search input {
            min-height: 44px;
            font-size: 16px;
        }

        .video-index__filters select {
            width: 100%;
            max-width: none;
        }

        .video-index__search {
            min-width: 0;
            grid-column: 1 / -1;
        }

        .video-index__search i {
            font-size: 14px;
        }

        .video-index__table-wrap {
            max-height: none;
            padding: 6px;
            background: var(--page-deep);
            overflow: visible;
        }

        .video-index__table,
        .video-index__table tbody {
            display: block;
        }

        .video-index__table thead {
            display: none;
        }

        .video-index__table tbody tr {
            display: grid;
            grid-template-columns: 22px minmax(0, 1fr);
            gap: 3px 8px;
            margin-bottom: 7px;
            padding: 10px;
            border: 1px solid var(--line-soft) !important;
            background: var(--surface);
        }

        .video-index__table tbody tr:last-child {
            margin-bottom: 0;
        }

        .video-index__table td {
            display: block;
            width: auto !important;
            height: auto;
            padding: 0;
            border: 0;
            overflow: visible;
            text-overflow: clip;
            white-space: normal;
        }

        .video-index__table .status-column {
            display: grid;
            min-height: 44px;
            grid-column: 1;
            grid-row: 1 / span 4;
            align-self: start;
            place-items: start center;
            padding-top: 5px !important;
        }

        .video-index__table .title-column {
            grid-column: 2;
            grid-row: 1;
            font-size: 14px;
            line-height: 1.4;
            overflow-wrap: anywhere;
        }

        .video-index__table .title-column i {
            margin-right: 5px;
        }

        .video-index__table .channel-column,
        .video-index__table .published-column,
        .video-index__table .id-column {
            display: block;
            grid-column: 2;
            color: var(--text-muted);
            font-size: 12px;
            line-height: 1.35;
            overflow-wrap: anywhere;
        }

        .video-index__table .channel-column {
            grid-row: 2;
        }

        .video-index__table .published-column {
            grid-row: 3;
        }

        .video-index__table .id-column {
            grid-row: 4;
            font-size: 11px;
        }

        .video-index__table .mobile-cell-label {
            display: inline;
            color: var(--text-soft);
            font-weight: 600;
        }

        .video-index__table .table-message {
            min-height: 112px;
            grid-column: 1 / -1;
            align-content: center;
            padding: 24px 10px;
            text-align: center;
        }

        .video-index__load-more {
            display: block;
            padding: 8px;
            border-top: 1px solid var(--line-soft);
            background: var(--surface-raised);
        }

        .video-index__load-more .button {
            width: 100%;
            min-height: 44px;
            flex-wrap: wrap;
            font-size: 13px;
        }

        .video-index__load-more span {
            color: var(--text-muted);
            font-size: 11px;
            font-weight: 500;
        }
    }
</style>
