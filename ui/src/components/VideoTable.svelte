<script lang="ts">
    export let videos: any[] = [];
    export let loading = false;

    let searchTerm = "";
    let statusFilter = "";

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
</script>

<section class="panel video-index" aria-labelledby="video-index-heading">
    <header class="section-header video-index__header">
        <div>
            <h2 id="video-index-heading" class="section-title">
                <i class="bi bi-collection-play" aria-hidden="true"></i>
                Library index
            </h2>
            <p class="section-description">
                {filteredVideos.length} of {videos.length} videos shown
            </p>
        </div>

        <div class="video-index__filters" role="group" aria-label="Video filters">
            <select
                bind:value={statusFilter}
                aria-label="Filter by link status"
                title="Filter by link status"
            >
                <option value="">All statuses</option>
                <option value="healthy">Linked or adopted</option>
                <option value="problem">Missing or error</option>
            </select>

            <select
                bind:value={channelFilter}
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
                    {#each filteredVideos as v}
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
                            <td class="published-column tabular-nums">{v.published}</td>
                            <td class="channel-column" title={v.channel}>{v.channel}</td>
                            <td class="title-column" title={v.title}>
                                <i class="bi bi-file-earmark-play" aria-hidden="true"></i>
                                <span>{v.title}</span>
                            </td>
                            <td class="id-column" title={v.video_id}>{v.video_id}</td>
                        </tr>
                    {/each}
                {/if}
            </tbody>
        </table>
    </div>
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
</style>
