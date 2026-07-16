<script lang="ts">
    import { onMount } from "svelte";
    import LogViewer from "./LogViewer.svelte";

    let videos: any[] = [];
    let loading = false;
    let page = 1;
    let total = 0;
    let pages = 1;
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
            pages = Math.max(1, Number(data.pages) || 1);
            page = Math.max(1, Number(data.page) || p);
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

    function refreshVideos() {
        // There is no persistent job-status endpoint yet; a deliberate refresh
        // lets the user retry while the server remains the concurrency authority.
        startedPaths = new Set();
        return fetchVideos(1);
    }

    onMount(() => {
        fetchVideos();
    });
</script>

<div class="space-y-6">
    <!-- Header Controls -->
    <div
        class="flex justify-between items-center bg-cyber-card p-4 rounded-xl border border-gray-800 shadow-lg"
    >
        <div>
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
                <i class="bi bi-film text-neon-pink"></i> Available Media
            </h2>
            <p class="text-gray-500 text-xs mt-1">
                Found {total} linked videos available for transcoding.
            </p>
        </div>
        <div class="flex gap-4">
            <button
                class="btn-primary bg-neon-cyan/20 text-neon-cyan border border-neon-cyan/50 hover:bg-neon-cyan/40 px-4 py-2 rounded transition-colors flex items-center gap-2 font-bold disabled:opacity-50"
                on:click={refreshVideos}
                disabled={loading}
            >
                <i class="bi bi-arrow-clockwise"></i> Refresh Media
            </button>
        </div>
    </div>

    {#if error}
        <div
            class="bg-red-900/20 border border-red-500/30 rounded px-4 py-2 text-sm text-red-300"
            role="alert"
        >
            {error}
        </div>
    {/if}

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div class="lg:col-span-2">
            <div
                class="bg-black/50 border border-gray-800 rounded-xl overflow-hidden h-[600px] flex flex-col"
            >
                <div class="overflow-auto flex-grow scrollbar-thin">
                    <table class="w-full text-left text-xs font-mono">
                        <thead
                            class="bg-gray-900/80 sticky top-0 text-gray-400"
                        >
                            <tr>
                                <th class="p-3">Channel</th>
                                <th class="p-3">Published</th>
                                <th class="p-3">Title</th>
                                <th class="p-3 text-right">Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {#if loading}
                                <tr
                                    ><td
                                        colspan="4"
                                        class="p-8 text-center animate-pulse"
                                        >Loading media...</td
                                    ></tr
                                >
                            {:else if videos.length === 0}
                                <tr
                                    ><td
                                        colspan="4"
                                        class="p-8 text-center text-gray-500"
                                        >No linked videos are currently available.</td
                                    ></tr
                                >
                            {:else}
                                {#each videos as v}
                                    <tr
                                        class="border-b border-gray-800/30 hover:bg-white/5 transition-colors"
                                    >
                                        <td class="p-3 text-neon-cyan/80"
                                            >{v.channel}</td
                                        >
                                        <td class="p-3 text-gray-500"
                                            >{v.published}</td
                                        >
                                        <td
                                            class="p-3 text-white truncate max-w-[200px]"
                                            title={v.title}>{v.title}</td
                                        >
                                        <td class="p-3 text-right">
                                            <button
                                                class="text-neon-pink hover:text-white border border-neon-pink/30 hover:bg-neon-pink/20 px-2 py-1 rounded disabled:opacity-50"
                                                on:click={() =>
                                                    startTranscode(v.symlink)}
                                                disabled={pendingPaths.has(v.symlink) || startedPaths.has(v.symlink)}
                                            >
                                                {#if pendingPaths.has(v.symlink)}
                                                    <i class="bi bi-arrow-repeat animate-spin"></i> Starting...
                                                {:else if startedPaths.has(v.symlink)}
                                                    <i class="bi bi-check2"></i> Started
                                                {:else}
                                                    <i class="bi bi-play-fill"></i> Transcode
                                                {/if}
                                            </button>
                                        </td>
                                    </tr>
                                {/each}
                            {/if}
                        </tbody>
                    </table>
                </div>
                <!-- Pagination -->
                {#if pages > 1}
                    <div
                        class="p-2 border-t border-gray-800 flex justify-center gap-2"
                    >
                        <button
                            disabled={loading || page === 1}
                            on:click={() => fetchVideos(page - 1)}
                            class="px-3 py-1 bg-gray-800 rounded hover:bg-gray-700 disabled:opacity-50"
                            >&lt;</button
                        >
                        <span class="text-gray-500 text-xs py-1"
                            >Page {page} of {pages}</span
                        >
                        <button
                            disabled={loading || page === pages}
                            on:click={() => fetchVideos(page + 1)}
                            class="px-3 py-1 bg-gray-800 rounded hover:bg-gray-700 disabled:opacity-50"
                            >&gt;</button
                        >
                    </div>
                {/if}
            </div>
        </div>

        <div class="lg:col-span-1">
            <LogViewer endpoint="/api/transcode/logs" title="TRANSCODE_LOGS" />
        </div>
    </div>
</div>
