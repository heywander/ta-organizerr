<script lang="ts">
    import { onMount } from "svelte";
    import LogViewer from "./LogViewer.svelte";

    let videos: any[] = [];
    let loading = false;
    let page = 1;
    let total = 0;
    let pages = 1;

    async function fetchVideos(p = 1) {
        loading = true;
        try {
            const res = await fetch(
                `/api/transcode/videos?page=${p}&per_page=100`,
            );
            const data = await res.json();
            videos = data.videos || [];
            total = data.total;
            pages = data.pages;
            page = data.page;
        } catch (e) {
            console.error(e);
        } finally {
            loading = false;
        }
    }

    async function startTranscode(filepath: string) {
        if (!confirm("Start transcoding?")) return;
        try {
            const res = await fetch("/api/transcode/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ filepath }),
            });
            const d = await res.json();
            alert(d.message);
        } catch (e) {
            alert(e);
        }
    }

    async function findMissing() {
        // Triggers orphan check which populates the missing list
        if (!confirm("Scan for missing videos?")) return;
        loading = true;
        try {
            const res = await fetch("/api/check-orphans", {
                method: "POST",
            });
            const d = await res.json();
            alert(`Found ${d.count} missing videos.`);
            fetchVideos(1);
        } catch (e) {
            alert(e);
            loading = false;
        }
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
                <i class="bi bi-film text-neon-pink"></i> Transcode Queue
            </h2>
            <p class="text-gray-500 text-xs mt-1">
                Found {total} videos requiring transcode.
            </p>
        </div>
        <div class="flex gap-4">
            <button
                class="btn-primary bg-neon-cyan/20 text-neon-cyan border border-neon-cyan/50 hover:bg-neon-cyan/40 px-4 py-2 rounded transition-colors flex items-center gap-2 font-bold"
                on:click={findMissing}
            >
                <i class="bi bi-search"></i> Find Missing
            </button>
        </div>
    </div>

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
                                        >Scanning...</td
                                    ></tr
                                >
                            {:else if videos.length === 0}
                                <tr
                                    ><td
                                        colspan="4"
                                        class="p-8 text-center text-gray-500"
                                        >Queue empty. No missing videos found.</td
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
                                                class="text-neon-pink hover:text-white border border-neon-pink/30 hover:bg-neon-pink/20 px-2 py-1 rounded"
                                                on:click={() =>
                                                    startTranscode(v.symlink)}
                                            >
                                                <i class="bi bi-play-fill"></i> Transcode
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
                            disabled={page === 1}
                            on:click={() => fetchVideos(page - 1)}
                            class="px-3 py-1 bg-gray-800 rounded hover:bg-gray-700 disabled:opacity-50"
                            >&lt;</button
                        >
                        <span class="text-gray-500 text-xs py-1"
                            >Page {page} of {pages}</span
                        >
                        <button
                            disabled={page === pages}
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
