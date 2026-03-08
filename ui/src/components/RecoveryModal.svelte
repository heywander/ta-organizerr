<script lang="ts">
    import { createEventDispatcher, onMount, onDestroy } from "svelte";
    const dispatch = createEventDispatcher();

    let activeTab: "unindexed" | "rescue" | "redundant" | "lost" | "channels" = "unindexed";
    let scanning = false;
    let status = "idle";
    let results: any = { unindexed: [], rescue: [], redundant: [], lost: [] };
    let pollInterval: ReturnType<typeof setInterval>;
    
    // State for Destruct Mode & Multi-selection
    let destructMode = false;
    let selectedPaths = new Set<string>();

    // Channel specific state
    let channels: string[] = [];
    let selectedChannel = "";
    let channelVideos: any[] = [];
    let loadingChannels = false;
    let searchingVideos = false;

    // Permissions state
    let permissions: any = null;
    let loadingPermissions = false;

    async function checkPermissions() {
        loadingPermissions = true;
        try {
            const res = await fetch("/api/system/check-permissions");
            permissions = await res.json();
        } catch (e) {
            console.error("Failed to check permissions", e);
        } finally {
            loadingPermissions = false;
        }
    }

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

    async function fetchChannels() {
        loadingChannels = true;
        try {
            const res = await fetch("/api/channels");
            channels = await res.json();
        } catch (e) {
            console.error("Failed to fetch channels", e);
        } finally {
            loadingChannels = false;
        }
    }

    async function fetchChannelVideos(channel: string) {
        if (!channel) return;
        searchingVideos = true;
        selectedChannel = channel;
        selectedPaths = new Set();
        try {
            const res = await fetch(`/api/channels/videos?channel=${encodeURIComponent(channel)}`);
            channelVideos = await res.json();
        } catch (e) {
            console.error("Failed to fetch channel videos", e);
        } finally {
            searchingVideos = false;
        }
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
                if (activeTab === "channels") fetchChannelVideos(selectedChannel);
                else startScan();
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
                if (activeTab === "channels") fetchChannelVideos(selectedChannel);
                else startScan();
            } else {
                const err = d.errors?.[0] || "Unknown error";
                alert(`Error deleting file: ${err}`);
            }
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
            
            if (d.fail_count > 0) {
                const firstErr = d.errors?.[0] || "Check backend logs.";
                alert(`Batch partially failed.\nSuccess: ${d.success_count}\nFailed: ${d.fail_count}\n\nFirst error: ${firstErr}`);
            } else {
                alert(`Batch complete. Deleted ${d.success_count} items.`);
            }
            
            selectedPaths = new Set();
            if (activeTab === "channels") fetchChannelVideos(selectedChannel);
            else startScan();
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
        selectedPaths = selectedPaths;
    }

    function toggleAll() {
        const items = activeTab === "channels" ? channelVideos : (results[activeTab] || []);
        if (selectedPaths.size === items.length && items.length > 0) {
            selectedPaths = new Set();
        } else {
            selectedPaths = new Set(items.map((i: any) => i.path));
        }
    }

    $: allSelected = (activeTab === "channels" ? channelVideos : results[activeTab])?.length > 0 && selectedPaths.size === (activeTab === "channels" ? channelVideos : results[activeTab])?.length;

    $: sourceRO = permissions?.source?.writeable === false;
    $: targetRO = permissions?.target?.writeable === false;

    onMount(() => {
        fetchChannels();
        checkPermissions();
    });
</script>

<div
    class="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
>
    <div
        class="bg-cyber-card border border-neon-cyan/30 rounded-xl w-full max-w-5xl h-[80vh] flex flex-col shadow-[0_0_50px_rgba(0,243,255,0.1)] overflow-hidden"
    >
        <!-- Header -->
        <div
            class="p-5 border-b border-gray-800 flex justify-between items-center bg-gray-900/40"
        >
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
                <i class="bi bi-bandaid text-neon-pink"></i> Advanced Recovery
                {#if destructMode}
                    <span class="text-xs bg-red-600 text-white px-2 py-0.5 rounded animate-pulse ml-2 font-mono">DESTRUCT MODE ACTIVE</span>
                {/if}
            </h2>
            <button
                on:click={() => dispatch("close")}
                class="text-gray-500 hover:text-white transition-colors"
                aria-label="Close"><i class="bi bi-x-lg"></i></button
            >
        </div>

        <!-- Permission Warnings -->
        {#if sourceRO || targetRO}
            <div class="bg-red-900/30 border-b border-red-500/50 p-3 flex items-center gap-3 animate-in fade-in slide-in-from-top duration-300">
                <i class="bi bi-exclamation-octagon-fill text-red-500 text-lg"></i>
                <div class="text-xs text-red-200">
                    <span class="font-bold uppercase">Filesystem Alert:</span>
                    {#if sourceRO && targetRO}
                        Both Source AND Target directories are currently <span class="text-white underline">READ-ONLY</span>. Deletion and reorganization will fail.
                    {:else if sourceRO}
                        Source archive is <span class="text-white underline">READ-ONLY</span>. Destruct Mode will fail.
                    {:else}
                        Target library is <span class="text-white underline">READ-ONLY</span>. Symlink cleanup will fail.
                    {/if}
                </div>
                <button on:click={checkPermissions} class="ml-auto text-[10px] bg-red-500/20 hover:bg-red-500/40 px-2 py-1 rounded border border-red-500/30 transition-colors">
                    Retry Check
                </button>
            </div>
        {/if}

        <!-- Controls -->
        <div class="p-4 bg-gray-900/50 flex flex-wrap gap-4 justify-between items-center">
            <div class="flex items-center gap-3">
                <button
                    class="btn-primary px-6 py-2 rounded font-bold text-black bg-neon-cyan hover:bg-white transition-all transform active:scale-95 disabled:opacity-50"
                    on:click={startScan}
                    disabled={scanning}
                >
                    {scanning ? "Scanning..." : "Run System Scan"}
                </button>
                
                {#if selectedPaths.size > 0}
                    <button
                        class="px-6 py-2 rounded font-bold text-white bg-red-600 hover:bg-red-500 transition-all transform active:scale-95 flex items-center gap-2 shadow-[0_0_15px_rgba(220,38,38,0.4)]"
                        on:click={deleteSelected}
                        disabled={targetRO || (destructMode && sourceRO)}
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
                    <span class="ml-3 text-sm font-medium text-gray-300 group-hover:text-white transition-colors flex items-center gap-2">
                        Destruct Mode
                        {#if destructMode}
                            <i class="bi bi-exclamation-triangle-fill text-red-500 animate-pulse"></i>
                        {/if}
                    </span>
                </label>

                <div class="text-xs text-mono text-gray-500">Status: <span class="text-neon-cyan underline decoration-dotted">{status}</span></div>
            </div>
        </div>

        <!-- Tabs -->
        <div class="flex border-b border-gray-800 px-4 bg-gray-900/20">
            {#each ["unindexed", "rescue", "redundant", "lost", "channels"] as tab}
                <button
                    class="px-4 py-3 text-sm font-semibold capitalize border-b-2 transition-all flex items-center gap-2
                    {activeTab === tab
                        ? 'border-neon-cyan text-neon-cyan bg-neon-cyan/5'
                        : 'border-transparent text-gray-500 hover:text-gray-300 hover:bg-white/5'}"
                    on:click={() => { activeTab = tab as any; selectedPaths = new Set(); }}
                >
                    {tab}
                    {#if tab !== 'channels'}
                        <span class="bg-gray-800 text-[10px] px-1.5 py-0.5 rounded-full min-w-[1.2rem] text-center"
                            >{results[tab]?.length || 0}</span
                        >
                    {/if}
                </button>
            {/each}
        </div>

        <!-- Content -->
        <div class="flex-grow overflow-y-auto p-4 bg-black/30">
            {#if activeTab === "channels"}
                <div class="flex flex-col h-full gap-4">
                    <!-- Channel Selector -->
                    <div class="flex gap-4 items-end">
                        <div class="flex-grow">
                            <label class="block text-[10px] text-gray-500 mb-1 uppercase tracking-widest">Select Channel (Indexed Only)</label>
                            <select 
                                class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white outline-none focus:border-neon-cyan transition-colors"
                                value={selectedChannel}
                                on:change={(e) => fetchChannelVideos(e.currentTarget.value)}
                            >
                                <option value="">-- Choose a Channel --</option>
                                {#each channels as channel}
                                    <option value={channel}>{channel}</option>
                                {/each}
                            </select>
                        </div>
                        <button 
                            class="p-2.5 rounded bg-gray-800 text-white hover:bg-gray-700 transition-colors border border-gray-700"
                            on:click={fetchChannels}
                            title="Refresh Channel List"
                        >
                            <i class="bi bi-arrow-clockwise"></i>
                        </button>
                    </div>

                    <!-- Video Table -->
                    <div class="flex-grow overflow-y-auto border border-gray-800 rounded bg-gray-900/20">
                        {#if searchingVideos}
                            <div class="flex flex-col items-center justify-center h-full text-neon-cyan gap-4">
                                <div class="w-8 h-8 border-2 border-neon-cyan border-t-transparent rounded-full animate-spin"></div>
                                <span class="text-xs font-mono animate-pulse">QUERYING TA DATABASE...</span>
                            </div>
                        {:else if channelVideos.length > 0}
                            <table class="w-full text-left text-[11px] text-gray-300 font-mono border-collapse">
                                <thead class="sticky top-0 bg-gray-900 z-10">
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
                                        <th class="p-3">Title</th>
                                        <th class="p-3 text-right">Action</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {#each channelVideos as item}
                                        <tr class="border-b border-gray-800/50 hover:bg-white/5 transition-colors {selectedPaths.has(item.path) ? 'bg-neon-cyan/5' : ''}">
                                            <td class="p-3 text-center">
                                                <input 
                                                    type="checkbox" 
                                                    checked={selectedPaths.has(item.path)} 
                                                    on:change={() => toggleSelect(item.path)}
                                                    class="accent-neon-cyan"
                                                >
                                            </td>
                                            <td class="p-3 text-neon-pink font-bold">{item.video_id}</td>
                                            <td class="p-3 truncate max-w-[400px]" title={item.title}>{item.title}</td>
                                            <td class="p-3 text-right">
                                                <button
                                                    class="text-red-500 hover:text-red-400 font-bold transition-colors disabled:opacity-30"
                                                    on:click={() => deleteFile(item.path)}
                                                    disabled={targetRO || (destructMode && sourceRO)}
                                                >
                                                    <i class="bi bi-trash"></i>
                                                </button>
                                            </td>
                                        </tr>
                                    {/each}
                                </tbody>
                            </table>
                        {:else}
                            <div class="flex flex-col items-center justify-center h-full text-gray-700 gap-3">
                                <i class="bi bi-broadcast text-4xl opacity-10"></i>
                                <span class="text-xs uppercase tracking-widest font-bold">No channel selected</span>
                            </div>
                        {/if}
                    </div>
                </div>
            {:else if scanning && (!results[activeTab] || results[activeTab].length === 0)}
                <div
                    class="flex flex-col items-center justify-center h-full text-neon-cyan gap-4"
                >
                    <div class="w-12 h-12 border-4 border-neon-cyan border-t-transparent rounded-full animate-spin"></div>
                    <div class="animate-pulse font-mono tracking-widest">SYSTEM WIDE SCAN IN PROGRESS...</div>
                </div>
            {:else}
                <table class="w-full text-left text-[11px] text-gray-300 font-mono border-collapse">
                    <thead class="sticky top-0 bg-gray-900 z-10">
                        <tr class="text-gray-500 border-b border-gray-800">
                            <th class="p-3 w-10">
                                <input 
                                    type="checkbox" 
                                    checked={allSelected} 
                                    on:change={toggleAll}
                                    class="accent-neon-cyan"
                                >
                            </th>
                            <th class="p-3">ID</th>
                            <th class="p-3">Filename / Target Path</th>
                            <th class="p-3">Info</th>
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
                                                class="text-neon-green hover:underline flex items-center gap-1 transition-all hover:scale-105"
                                                on:click={() =>
                                                    recoverFile(item.path)}
                                                ><i class="bi bi-plus-circle"></i> RECOVER</button
                                            >
                                        {:else if activeTab === "redundant"}
                                            <button
                                                class="text-red-500 hover:underline font-bold transition-all hover:scale-105 disabled:opacity-30"
                                                on:click={() =>
                                                    deleteFile(item.path)}
                                                disabled={targetRO || (destructMode && sourceRO)}
                                                ><i class="bi bi-trash"></i> DELETE</button
                                            >
                                        {:else if activeTab === "lost"}
                                            <button
                                                class="text-neon-yellow hover:underline flex items-center gap-1"
                                                ><i class="bi bi-lightning-fill"></i> FORCE</button
                                            >
                                            <button
                                                class="text-red-500 hover:underline font-bold disabled:opacity-30"
                                                on:click={() =>
                                                    deleteFile(item.path)}
                                                disabled={targetRO || (destructMode && sourceRO)}
                                                ><i class="bi bi-trash"></i> DELETE</button
                                            >
                                        {:else}
                                            <button
                                                class="text-neon-pink hover:underline flex items-center gap-1"
                                                ><i class="bi bi-rescue-ambulance"></i> RESCUE</button
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
                                    class="p-20 text-center text-gray-600 bg-gray-900/10"
                                >
                                    <i class="bi bi-shield-lock text-4xl mb-4 block opacity-10"></i>
                                    NO ANOMALIES DETECTED IN THIS SECTOR.
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
    
    .bg-cyber-card {
        background: linear-gradient(135deg, #0f0f13 0%, #050505 100%);
    }

    /* Custom Scrollbar for better Cyberpunk feel */
    ::-webkit-scrollbar {
        width: 4px;
    }
    ::-webkit-scrollbar-track {
        background: rgba(15, 15, 15, 0.5);
    }
    ::-webkit-scrollbar-thumb {
        background: #00f3ff22;
        border-radius: 10px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #00f3ff66;
    }
</style>
