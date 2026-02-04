<script lang="ts">
    export let videos: any[] = [];
    export let loading = false;

    let searchTerm = "";
    let statusFilter = "";

    // Channels derived from videos
    $: channels = [...new Set(videos.map((v) => v.channel))].sort();
    let channelFilter = "";

    $: filteredVideos = videos.filter((v) => {
        const matchSearch =
            searchTerm === "" ||
            v.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
            v.video_id.includes(searchTerm);
        const matchStatus = statusFilter === "" || v.status === statusFilter;
        const matchChannel =
            channelFilter === "" || v.channel === channelFilter;
        return matchSearch && matchStatus && matchChannel;
    });
</script>

<div
    class="bg-cyber-card border border-gray-800 rounded-xl shadow-lg flex flex-col h-full overflow-hidden"
>
    <div
        class="p-4 border-b border-gray-800 bg-gray-900/50 flex flex-col sm:flex-row gap-3 justify-between items-center"
    >
        <h3 class="font-bold text-white flex items-center gap-2">
            <i class="bi bi-grid-3x3"></i> Video Matrix
        </h3>

        <div class="flex gap-2 w-full sm:w-auto overflow-x-auto">
            <select
                bind:value={statusFilter}
                class="bg-black border border-gray-700 text-gray-300 text-xs rounded px-2 py-1 focus:border-neon-cyan focus:outline-none"
            >
                <option value="">All Status</option>
                <option value="linked">Linked</option>
                <option value="missing">Missing</option>
            </select>

            <select
                bind:value={channelFilter}
                class="bg-black border border-gray-700 text-gray-300 text-xs rounded px-2 py-1 focus:border-neon-cyan focus:outline-none max-w-[150px]"
            >
                <option value="">All Channels</option>
                {#each channels as ch}
                    <option value={ch}>{ch}</option>
                {/each}
            </select>

            <div class="relative">
                <input
                    type="text"
                    bind:value={searchTerm}
                    placeholder="Search..."
                    class="bg-black border border-gray-700 text-gray-300 text-xs rounded pl-8 pr-2 py-1 w-full sm:w-40 focus:border-neon-cyan focus:outline-none transition-all focus:w-48"
                />
                <i
                    class="bi bi-search absolute left-2 top-1.5 text-gray-500 text-xs"
                ></i>
            </div>
        </div>
    </div>

    <div
        class="overflow-auto flex-grow max-h-[500px] scrollbar-thin scrollbar-thumb-gray-800"
    >
        <table class="w-full text-left text-xs font-mono">
            <thead
                class="bg-black/50 text-gray-500 sticky top-0 backdrop-blur-sm z-10"
            >
                <tr>
                    <th class="p-3 w-8">St</th>
                    <th class="p-3">Published</th>
                    <th class="p-3">Channel</th>
                    <th class="p-3">Title</th>
                    <th class="p-3">ID</th>
                </tr>
            </thead>
            <tbody>
                {#if loading}
                    <tr
                        ><td
                            colspan="5"
                            class="p-8 text-center text-gray-500 animate-pulse"
                            >Scanning matrix...</td
                        ></tr
                    >
                {:else if filteredVideos.length === 0}
                    <tr
                        ><td colspan="5" class="p-8 text-center text-gray-500"
                            >No signals found.</td
                        ></tr
                    >
                {:else}
                    {#each filteredVideos as v}
                        <tr
                            class="border-b border-gray-800/50 hover:bg-white/5 transition-colors group"
                        >
                            <td class="p-3">
                                <div
                                    class="w-2 h-2 rounded-full {v.status ===
                                    'linked'
                                        ? 'bg-neon-green shadow-[0_0_5px_rgba(57,255,20,0.5)]'
                                        : 'bg-red-500 shadow-[0_0_5px_rgba(239,68,68,0.5)]'}"
                                ></div>
                            </td>
                            <td class="p-3 text-gray-400 whitespace-nowrap"
                                >{v.published}</td
                            >
                            <td class="p-3 text-neon-cyan/80">{v.channel}</td>
                            <td
                                class="p-3 font-semibold text-white group-hover:text-neon-pink transition-colors truncate max-w-[200px]"
                                title={v.title}>{v.title}</td
                            >
                            <td class="p-3 text-gray-600 select-all"
                                >{v.video_id}</td
                            >
                        </tr>
                    {/each}
                {/if}
            </tbody>
        </table>
    </div>

    <div
        class="p-2 border-t border-gray-800 bg-black/30 text-right text-[10px] text-gray-500"
    >
        Showing {filteredVideos.length} / {videos.length} videos
    </div>
</div>
