<script lang="ts">
    import { createEventDispatcher, onMount } from "svelte";

    export let show = false;

    const dispatch = createEventDispatcher();

    let hiddenChannels: string[] = [];
    let newChannel = "";
    let loading = true;
    let error: string | null = null;

    async function fetchHidden() {
        loading = true;
        try {
            const res = await fetch("/api/hidden");
            if (!res.ok) throw new Error("Failed to fetch hidden list");
            const data = await res.json();
            hiddenChannels = data.channels || [];
            error = null;
        } catch (e: any) {
            error = e.message;
        } finally {
            loading = false;
        }
    }

    async function addChannel() {
        if (!newChannel.trim()) return;
        try {
            const res = await fetch("/api/hidden", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ channel: newChannel.trim() }),
            });
            if (!res.ok) throw new Error("Failed to add channel");
            newChannel = "";
            fetchHidden();
        } catch (e: any) {
            error = e.message;
        }
    }

    async function removeChannel(channel: string) {
        if (
            !confirm(
                `Unhide channel "${channel}"? It will be moved to public target on next scan.`,
            )
        )
            return;
        try {
            const res = await fetch("/api/hidden", {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ channel }),
            });
            if (!res.ok) throw new Error("Failed to remove channel");
            fetchHidden();
        } catch (e: any) {
            error = e.message;
        }
    }

    onMount(() => {
        fetchHidden();
    });
</script>

<div
    class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4"
>
    <div
        class="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-lg shadow-2xl overflow-hidden"
    >
        <div
            class="p-6 border-b border-gray-800 flex justifying-between items-center"
        >
            <h2 class="text-xl font-bold text-white flex items-center">
                <i class="bi bi-eye-slash text-purple-400 mr-2"></i> Hidden Channels
            </h2>
            <button
                class="text-gray-400 hover:text-white transition-colors"
                on:click={() => dispatch("close")}
            >
                <i class="bi bi-x-lg text-lg"></i>
            </button>
        </div>

        <div class="p-6 space-y-6">
            <div
                class="text-sm text-gray-400 bg-gray-800/50 p-3 rounded border border-gray-700"
            >
                <i class="bi bi-info-circle mr-1"></i>
                Channels in this list will be synchronized to the
                <code>/hidden</code>
                folder instead of the public <code>/target</code> folder.
            </div>

            <!-- Add New -->
            <div class="flex gap-2">
                <input
                    type="text"
                    bind:value={newChannel}
                    placeholder="Enter Exact Channel Name..."
                    class="flex-1 bg-gray-800 border border-gray-700 rounded px-4 py-2 text-white focus:outline-none focus:border-purple-500 transition-colors"
                    on:keydown={(e) => e.key === "Enter" && addChannel()}
                />
                <button
                    class="bg-purple-600 hover:bg-purple-500 text-white px-4 py-2 rounded font-medium transition-colors"
                    on:click={addChannel}
                >
                    <i class="bi bi-plus-lg mr-1"></i> Hide
                </button>
            </div>

            <!-- List -->
            <div
                class="max-h-60 overflow-y-auto space-y-2 pr-1 custom-scrollbar"
            >
                {#if loading}
                    <div class="text-center py-4 text-gray-500">Loading...</div>
                {:else if hiddenChannels.length === 0}
                    <div class="text-center py-4 text-gray-500 italic">
                        No hidden channels
                    </div>
                {:else}
                    {#each hiddenChannels as channel}
                        <div
                            class="flex items-center justify-between bg-gray-800 p-3 rounded group hover:bg-gray-750 transition-colors border border-transparent hover:border-gray-700"
                        >
                            <span class="text-gray-200 font-medium"
                                >{channel}</span
                            >
                            <button
                                class="text-gray-500 hover:text-red-400 transition-colors opacity-0 group-hover:opacity-100"
                                title="Unhide"
                                on:click={() => removeChannel(channel)}
                            >
                                <i class="bi bi-eye text-lg"></i>
                            </button>
                        </div>
                    {/each}
                {/if}
            </div>

            {#if error}
                <div
                    class="text-red-400 text-sm bg-red-900/20 p-2 rounded border border-red-900/50"
                >
                    {error}
                </div>
            {/if}
        </div>

        <div class="p-4 bg-gray-950 border-t border-gray-800 flex justify-end">
            <button
                class="px-4 py-2 rounded text-gray-300 hover:bg-gray-800 transition-colors"
                on:click={() => dispatch("close")}
            >
                Close
            </button>
        </div>
    </div>
</div>
