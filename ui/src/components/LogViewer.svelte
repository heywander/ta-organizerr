<script lang="ts">
    import { onMount, onDestroy } from "svelte";

    export let endpoint = "http://localhost:8002/api/logs";
    export let title = "SYSTEM_LOGS";

    let logs: string[] = [];
    let logEndRef: HTMLDivElement;
    let interval: ReturnType<typeof setInterval>;
    let nextIndex = 0;
    let mounted = false;

    async function fetchLogs() {
        try {
            const res = await fetch(`${endpoint}?start=${nextIndex}`);
            if (!res.ok) return;
            const data = await res.json();

            if (data.logs && data.logs.length > 0) {
                logs = [...logs, ...data.logs];
                nextIndex = data.next_index;
                // Limit local buffer
                if (logs.length > 1000) logs = logs.slice(-1000);
            }
        } catch (e) {
            if (
                logs.length === 0 ||
                logs[logs.length - 1] !== "Error connecting to logs..."
            ) {
                logs = [...logs, "Error connecting to logs..."];
                // Don't spam error
            }
        }
    }

    onMount(() => {
        mounted = true;
        fetchLogs();
        interval = setInterval(fetchLogs, 2000);
    });

    onDestroy(() => {
        clearInterval(interval);
    });

    // Auto-scroll
    $: if (mounted && logs && logEndRef) {
        logEndRef.scrollIntoView({ behavior: "smooth" });
    }

    function clearLogs() {
        logs = [];
        nextIndex = 0;
    }
</script>

<div
    class="border border-gray-800 rounded-xl overflow-hidden bg-black shadow-lg flex flex-col"
>
    <div
        class="bg-gray-900/50 px-4 py-2 border-b border-gray-800 flex justify-between items-center"
    >
        <span class="text-xs font-mono text-gray-400 flex items-center">
            <span class="w-2 h-2 rounded-full bg-neon-green animate-pulse mr-2"
            ></span>
            {title}
        </span>
        <button
            on:click={clearLogs}
            class="text-xs text-gray-500 hover:text-white transition-colors"
            >CLEAR</button
        >
    </div>
    <div
        class="p-4 overflow-y-auto h-[300px] font-mono text-xs space-y-1 scrollbar-thin scrollbar-thumb-gray-700 scrollbar-track-transparent"
    >
        {#each logs as log, i}
            <div
                class="break-words text-neon-green/80 border-l-2 border-transparent hover:border-neon-green hover:bg-neon-green/5 pl-2 transition-colors"
            >
                <span class="opacity-50 select-none mr-2">[{i}]</span>
                {log}
            </div>
        {/each}
        {#if logs.length === 0}
            <div class="text-gray-600 italic text-center mt-10">
                Waiting for {title === "SYSTEM_LOGS" ? "system" : "transcode"} logs...
            </div>
        {/if}
        <div bind:this={logEndRef}></div>
    </div>
</div>
