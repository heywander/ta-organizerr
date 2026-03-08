<script lang="ts">
    import { createEventDispatcher, onMount, onDestroy } from "svelte";
    const dispatch = createEventDispatcher();

    let activeTab: "unindexed" | "rescue" | "redundant" | "lost" = "unindexed";
    let scanning = false;
    let status = "idle";
    let results: any = { unindexed: [], rescue: [], redundant: [], lost: [] };
    let pollInterval: ReturnType<typeof setInterval>;
    
    // New State for Destruct Mode & Multi-selection
    let destructMode = false;
    let selectedPaths = new Set<string>();

    async function startScan() {
        scanning = true;
        try {
            await fetch("/api/recovery/scan", {
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
                const res = await fetch("/api/recovery/poll");
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
        if (!isBatch && !confirm("Recover this file?")) return;
        try {
            const res = await fetch("/api/recovery/start", {
                method: "POST",
                body: JSON.stringify({ filepath: path }),
                headers: { "Content-Type": "application/json" },
            });
            const d = await res.json();
            if (!isBatch) {
                alert(d.message);
                startScan();
            }
        } catch (e) {
            alert(e);
        }
    }

    async function deleteFile(path: string) {
        let msg = "Delete file? This cannot be undone.";
        if (destructMode) {
            msg = "☢️ DESTRUCT MODE ACTIVE ☢️\n\nThis will delete BOTH the target folder AND the source file in your archive.\n\nAre you absolutely sure?";
        }
        
        if (!confirm(msg)) return;
        
        if (destructMode && !confirm("FINAL WARNING: This is IRREVERSIBLE. Delete source file now?")) return;

        try {
            const res = await fetch("/api/recovery/delete-batch", {
                method: "POST",
                body: JSON.stringify({ 
                    filepaths: [path],
                    destruct_mode: destructMode 
                }),
                headers: { "Content-Type": "application/json" },
            });
            const d = await res.json();
            if (d.success_count > 0) {
                alert("Deleted.");
                startScan();
            } else alert("Error deleting file.");
        } catch (e) {
            alert(e);
        }
    }

    async function deleteSelected() {
        if (selectedPaths.size === 0) return;
        
        let msg = `Delete ${selectedPaths.size} selected items?`;
        if (destructMode) {
            msg = `☢️ DESTRUCT MODE ACTIVE ☢️\n\nYou are about to delete ${selectedPaths.size} items from BOTH Target and Source.\n\nAre you sure you want to proceed?`;
        }
        
        if (!confirm(msg)) return;
        if (destructMode && !confirm("FINAL WARNING: This will permanently delete SOURCE FILES. Continue?")) return;

        try {
            const res = await fetch("/api/recovery/delete-batch", {
                method: "POST",
                body: JSON.stringify({ 
                    filepaths: Array.from(selectedPaths),
                    destruct_mode: destructMode 
                }),
                headers: { "Content-Type": "application/json" },
            });
            const d = await res.json();
            alert(`Batch complete. Success: ${d.success_count}, Failed: ${d.fail_count}`);
            selectedPaths = new Set();
            startScan();
        } catch (e) {
            alert("Batch delete failed: " + e);
        }
    }

    function toggleSelect(path: string) {
        if (selectedPaths.has(path)) {
            selectedPaths.delete(path);
        } else {
            selectedPaths.add(path);
        }
        selectedPaths = selectedPaths; // Trigger Svelte reactivity
    }

    function toggleAll() {
        const items = results[activeTab] || [];
        if (selectedPaths.size === items.length && items.length > 0) {
            selectedPaths = new Set();
        } else {
            selectedPaths = new Set(items.map((i: any) => i.path));
        }
    }

    $: allSelected = results[activeTab]?.length > 0 && selectedPaths.size === results[activeTab]?.length;
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
                {#if destructMode}
                    <span class="text-xs bg-red-600 text-white px-2 py-0.5 rounded animate-pulse ml-2 font-mono">DESTRUCT MODE ACTIVE</span>
                {/if}
            </h2>
            <button
                on:click={() => dispatch("close")}
                class="text-gray-500 hover:text-white"
                aria-label="Close"><i class="bi bi-x-lg"></i></button
            >
        </div>

        <!-- Controls -->
        <div class="p-4 bg-gray-900/50 flex flex-wrap gap-4 justify-between items-center">
            <div class="flex items-center gap-3">
                <button
                    class="btn-primary px-6 py-2 rounded font-bold text-black bg-neon-cyan hover:bg-white transition-colors disabled:opacity-50"
                    on:click={startScan}
                    disabled={scanning}
                >
                    {scanning ? "Scanning..." : "Run System Scan"}
                </button>
                
                {#if selectedPaths.size > 0}
                    <button
                        class="px-6 py-2 rounded font-bold text-white bg-red-600 hover:bg-red-500 transition-colors flex items-center gap-2"
                        on:click={deleteSelected}
                    >
                        <i class="bi bi-trash"></i> Delete Selected ({selectedPaths.size})
                    </button>
                {/if}
            </div>

            <div class="flex items-center gap-6">
                <!-- Destruct Mode Toggle -->
                <label class="relative inline-flex items-center cursor-pointer group">
                    <input type="checkbox" bind:checked={destructMode} class="sr-only peer">
                    <div class="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-red-600"></div>
                    <span class="ml-3 text-sm font-medium text-gray-300 group-hover:text-white transition-colors">
                        Destruct Mode
                        <i class="bi bi-exclamation-triangle-fill text-red-500 ml-1 {destructMode ? 'visible' : 'opacity-0'}"></i>
                    </span>
                </label>

                <div class="text-xs text-mono text-gray-500">Status: <span class="text-neon-cyan">{status}</span></div>
            </div>
        </div>

        <!-- Tabs -->
        <div class="flex border-b border-gray-800 px-4">
            {#each ["unindexed", "rescue", "redundant", "lost"] as tab}
                <button
                    class="px-4 py-3 text-sm font-semibold capitalize border-b-2 transition-colors flex items-center gap-2
                    {activeTab === tab
                        ? 'border-neon-cyan text-neon-cyan'
                        : 'border-transparent text-gray-500 hover:text-gray-300'}"
                    on:click={() => { activeTab = tab as any; selectedPaths = new Set(); }}
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
                    class="flex flex-col items-center justify-center h-full text-neon-cyan gap-4"
                >
                    <div class="w-12 h-12 border-4 border-neon-cyan border-t-transparent rounded-full animate-spin"></div>
                    <div class="animate-pulse font-mono">SYSTEM WIDE SCAN IN PROGRESS...</div>
                </div>
            {:else}
                <table class="w-full text-left text-xs text-gray-300 font-mono border-collapse">
                    <thead class="sticky top-0 bg-cyber-card z-10 shadow-sm">
                        <tr class="text-gray-500 border-b border-gray-800">
                            <th class="p-3 w-10">
                                <input 
                                    type="checkbox" 
                                    checked={allSelected} 
                                    on:change={toggleAll}
                                    class="accent-neon-cyan"
                                >
                            </th>
                            <th class="p-3">Video ID</th>
                            <th class="p-3">Filename / Path</th>
                            <th class="p-3">Size/Info</th>
                            <th class="p-3 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each results[activeTab] || [] as item}
                            <tr
                                class="border-b border-gray-800/50 hover:bg-white/5 transition-colors {selectedPaths.has(item.path) ? 'bg-neon-cyan/5' : ''}"
                            >
                                <td class="p-3 text-center">
                                    <input 
                                        type="checkbox" 
                                        checked={selectedPaths.has(item.path)} 
                                        on:change={() => toggleSelect(item.path)}
                                        class="accent-neon-cyan"
                                    >
                                </td>
                                <td class="p-3 text-neon-pink font-bold"
                                    >{item.video_id}</td
                                >
                                <td
                                    class="p-3 truncate max-w-[350px] group relative"
                                    title={item.path}
                                >
                                    <span class="group-hover:text-white transition-colors">{item.filename || item.path}</span>
                                </td
                                >
                                <td class="p-3"
                                    >{item.size_mb
                                        ? item.size_mb + " MB"
                                        : item.ta_source || "-"}</td
                                >
                                <td class="p-3 text-right">
                                    <div class="flex justify-end gap-3">
                                        {#if activeTab === "unindexed"}
                                            <button
                                                class="text-neon-green hover:underline flex items-center gap-1"
                                                on:click={() =>
                                                    recoverFile(item.path)}
                                                ><i class="bi bi-plus-circle"></i> Recover</button
                                            >
                                        {:else if activeTab === "redundant"}
                                            <button
                                                class="text-red-500 hover:underline flex items-center gap-1"
                                                on:click={() =>
                                                    deleteFile(item.path)}
                                                ><i class="bi bi-trash"></i> Delete</button
                                            >
                                        {:else if activeTab === "lost"}
                                            <button
                                                class="text-neon-yellow hover:underline flex items-center gap-1"
                                                ><i class="bi bi-lightning-fill"></i> Force</button
                                            >
                                            <button
                                                class="text-red-500 hover:underline flex items-center gap-1"
                                                on:click={() =>
                                                    deleteFile(item.path)}
                                                ><i class="bi bi-trash"></i> Delete</button
                                            >
                                        {:else}
                                            <button
                                                class="text-neon-pink hover:underline flex items-center gap-1"
                                                ><i class="bi bi-rescue-ambulance"></i> Rescue</button
                                            >
                                        {/if}
                                    </div>
                                </td>
                            </tr>
                        {/each}
                        {#if !results[activeTab]?.length}
                            <tr
                                ><td
                                    colspan="5"
                                    class="p-20 text-center text-gray-600 bg-gray-900/20"
                                >
                                    <i class="bi bi-cloud-check text-4xl mb-4 block opacity-20"></i>
                                    Nothing detected in this category.
                                </td
                                ></tr
                            >
                        {/if}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>
</div>

<style>
    .accent-neon-cyan {
        accent-color: #00f3ff;
    }
</style>
