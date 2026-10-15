<script lang="ts">
    import { onMount, onDestroy } from "svelte";
    import LogViewer from "./LogViewer.svelte";

    let videos: any[] = [];
    let loading = false;
    let page = 1;
    let total = 0;
    let unknown = 0;
    let pages = 1;
    let running = false;
    let active: string[] = [];
    let interval: ReturnType<typeof setInterval>;

    async function fetchVideos(p = 1) {
        loading = true;
        try {
            const res = await fetch(
                `/api/transcode/videos?page=${p}&per_page=100`,
            );
            const data = await res.json();
            videos = data.videos || [];
            total = data.total;
            unknown = data.unknown ?? 0;
            pages = data.pages;
            page = data.page;
            running = !!data.running;
            active = data.active || [];
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

    async function scanCodecs() {
        // Probes linked videos with ffprobe in the background so the queue
        // only lists files that actually need converting.
        if (!confirm("Scan linked videos for their codecs? This runs in the background.")) return;
        try {
            const res = await fetch("/api/transcode/scan", { method: "POST" });
            const d = await res.json();
            alert(d.message);
        } catch (e) {
            alert(e);
        }
    }

    function codecLabel(v: any): string {
        if (!v.video_codec && !v.audio_codec) return "unknown";
        return `${v.video_codec ?? "?"} / ${v.audio_codec ?? "?"}`;
    }

    onMount(() => {
        fetchVideos();
        // Refresh while a job or scan is running so finished files drop out.
        interval = setInterval(() => fetchVideos(page), 10000);
    });

    onDestroy(() => clearInterval(interval));
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
                {total} linked videos not yet H.264/AAC
                {#if unknown > 0}({unknown} not probed yet){/if}
                {#if running}· <span class="text-neon-pink">transcoding {active.length} file{active.length === 1 ? "" : "s"}</span>{/if}
            </p>
        </div>
        <div class="flex gap-4">
            <button
                class="btn-primary bg-neon-cyan/20 text-neon-cyan border border-neon-cyan/50 hover:bg-neon-cyan/40 px-4 py-2 rounded transition-colors flex items-center gap-2 font-bold"
                on:click={scanCodecs}
            >
                <i class="bi bi-search"></i> Scan Codecs
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
                                <th class="p-3">Codecs</th>
                                <th class="p-3 text-right">Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {#if loading}
                                <tr
                                    ><td
                                        colspan="5"
                                        class="p-8 text-center animate-pulse"
                                        >Scanning...</td
                                    ></tr
                                >
                            {:else if videos.length === 0}
                                <tr
                                    ><td
                                        colspan="5"
                                        class="p-8 text-center text-gray-500"
                                        >Queue empty. Every probed video is already H.264/AAC.</td
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
                                        <td class="p-3 text-gray-400">{codecLabel(v)}</td>
                                        <td class="p-3 text-right">
                                            <button
                                                class="text-neon-pink hover:text-white border border-neon-pink/30 hover:bg-neon-pink/20 px-2 py-1 rounded"
                                                disabled={active.includes(v.symlink)}
                                                on:click={() =>
                                                    startTranscode(v.symlink)}
                                            >
                                                <i class="bi bi-play-fill"></i>
                                                {active.includes(v.symlink) ? "Running" : "Transcode"}
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
