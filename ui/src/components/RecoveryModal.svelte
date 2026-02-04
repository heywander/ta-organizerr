<script lang="ts">
    import { createEventDispatcher, onMount, onDestroy } from "svelte";
    const dispatch = createEventDispatcher();

    let activeTab: "unindexed" | "rescue" | "redundant" | "lost" = "unindexed";
    let scanning = false;
    let status = "idle";
    let results: any = { unindexed: [], rescue: [], redundant: [], lost: [] };
    let pollInterval: ReturnType<typeof setInterval>;

    async function startScan() {
        scanning = true;
        try {
            await fetch("http://localhost:8002/api/recovery/scan", {
                method: "POST",
            });
            pollResults();
        } catch (e) {
            alert("Scan start failed: " + e);
            scanning = false;
        }
    }

    function pollResults() {
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(async () => {
            try {
                const res = await fetch(
                    "http://localhost:8002/api/recovery/poll",
                );
                const data = await res.json();
                status = data.status;
                if (data.status === "done") {
                    results = data.results || results;
                    scanning = false;
                    clearInterval(pollInterval);
                } else if (data.status === "error") {
                    scanning = false;
                    clearInterval(pollInterval);
                    alert("Scan error: " + data.results);
                }
            } catch (e) {
                console.error(e);
            }
        }, 2000);
    }

    onDestroy(() => {
        if (pollInterval) clearInterval(pollInterval);
    });

    async function recoverFile(path: string, isBatch = false) {
        // Implementation mirrors existing JS logic
        if (!isBatch && !confirm("Recover this file?")) return;
        try {
            const res = await fetch(
                "http://localhost:8002/api/recovery/start",
                {
                    method: "POST",
                    body: JSON.stringify({ filepath: path }),
                    headers: { "Content-Type": "application/json" },
                },
            );
            const d = await res.json();
            if (!isBatch) {
                alert(d.message);
                startScan();
            } // Refresh
        } catch (e) {
            alert(e);
        }
    }

    async function deleteFile(path: string) {
        if (!confirm("Delete file? This cannot be undone.")) return;
        try {
            const res = await fetch(
                "http://localhost:8002/api/recovery/delete",
                {
                    method: "POST",
                    body: JSON.stringify({ filepath: path }),
                    headers: { "Content-Type": "application/json" },
                },
            );
            const d = await res.json();
            if (d.success) {
                alert("Deleted.");
                startScan();
            } else alert("Error: " + d.error);
        } catch (e) {
            alert(e);
        }
    }
</script>

<div
    class="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
>
    <div
        class="bg-cyber-card border border-neon-cyan/30 rounded-xl w-full max-w-5xl h-[80vh] flex flex-col shadow-[0_0_50px_rgba(0,243,255,0.1)]"
    >
        <!-- Header -->
        <div
            class="p-5 border-b border-gray-800 flex justify-between items-center"
        >
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
                <i class="bi bi-bandaid text-neon-pink"></i> Advanced Recovery
            </h2>
            <button
                on:click={() => dispatch("close")}
                class="text-gray-500 hover:text-white"
                aria-label="Close"><i class="bi bi-x-lg"></i></button
            >
        </div>

        <!-- Controls -->
        <div class="p-4 bg-gray-900/50 flex justify-between items-center">
            <button
                class="btn-primary px-6 py-2 rounded font-bold text-black bg-neon-cyan hover:bg-white transition-colors"
                on:click={startScan}
                disabled={scanning}
            >
                {scanning ? "Scanning..." : "Run System Scan"}
            </button>
            <div class="text-xs text-mono text-gray-500">Status: {status}</div>
        </div>

        <!-- Tabs -->
        <div class="flex border-b border-gray-800 px-4">
            {#each ["unindexed", "rescue", "redundant", "lost"] as tab}
                <button
                    class="px-4 py-3 text-sm font-semibold capitalize border-b-2 transition-colors flex items-center gap-2
                    {activeTab === tab
                        ? 'border-neon-cyan text-neon-cyan'
                        : 'border-transparent text-gray-500 hover:text-gray-300'}"
                    on:click={() => (activeTab = tab as any)}
                >
                    {tab}
                    <span class="bg-gray-800 text-xs px-1.5 rounded-full"
                        >{results[tab]?.length || 0}</span
                    >
                </button>
            {/each}
        </div>

        <!-- Content -->
        <div class="flex-grow overflow-y-auto p-4 bg-black/30">
            {#if scanning && (!results[activeTab] || results[activeTab].length === 0)}
                <div
                    class="flex items-center justify-center h-full text-neon-cyan animate-pulse"
                >
                    Scanning...
                </div>
            {:else}
                <table class="w-full text-left text-xs text-gray-300 font-mono">
                    <thead>
                        <tr class="text-gray-500 border-b border-gray-800">
                            <th class="p-3">Video ID</th>
                            <th class="p-3">Filename / Path</th>
                            <th class="p-3">Size/Info</th>
                            <th class="p-3 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each results[activeTab] || [] as item}
                            <tr
                                class="border-b border-gray-800/50 hover:bg-white/5"
                            >
                                <td class="p-3 text-neon-pink"
                                    >{item.video_id}</td
                                >
                                <td
                                    class="p-3 truncate max-w-[300px]"
                                    title={item.path}
                                    >{item.filename || item.path}</td
                                >
                                <td class="p-3"
                                    >{item.size_mb
                                        ? item.size_mb + " MB"
                                        : item.ta_source || "-"}</td
                                >
                                <td class="p-3 text-right">
                                    {#if activeTab === "unindexed"}
                                        <button
                                            class="text-neon-green hover:underline"
                                            on:click={() =>
                                                recoverFile(item.path)}
                                            >Recover</button
                                        >
                                    {:else if activeTab === "redundant"}
                                        <button
                                            class="text-red-500 hover:underline"
                                            on:click={() =>
                                                deleteFile(item.path)}
                                            >Delete</button
                                        >
                                    {:else if activeTab === "lost"}
                                        <button
                                            class="text-neon-yellow hover:underline mr-2"
                                            >Force</button
                                        >
                                        <button
                                            class="text-red-500 hover:underline"
                                            on:click={() =>
                                                deleteFile(item.path)}
                                            >Delete</button
                                        >
                                    {:else}
                                        <button
                                            class="text-neon-pink hover:underline"
                                            >Rescue</button
                                        >
                                    {/if}
                                </td>
                            </tr>
                        {/each}
                        {#if !results[activeTab]?.length}
                            <tr
                                ><td
                                    colspan="4"
                                    class="p-10 text-center text-gray-600"
                                    >No items found.</td
                                ></tr
                            >
                        {/if}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>
</div>
