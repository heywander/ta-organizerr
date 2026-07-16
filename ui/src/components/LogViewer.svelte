<script lang="ts">
    import { onMount, onDestroy } from "svelte";

    export let endpoint = "/api/logs";
    export let title = "SYSTEM_LOGS";

    let logs: string[] = [];
    let logEndRef: HTMLDivElement;
    let interval: ReturnType<typeof setInterval>;
    let nextIndex = 0;
    let mounted = false;
    let fetching = false;
    let requestSequence = 0;
    let activeController: AbortController | null = null;

    const CONNECTION_ERROR = "Error connecting to logs...";

    async function fetchLogs() {
        if (fetching) return;

        const requestId = ++requestSequence;
        const controller = new AbortController();
        activeController = controller;
        fetching = true;

        try {
            const res = await fetch(`${endpoint}?start=${nextIndex}`, {
                signal: controller.signal,
            });
            if (!res.ok) {
                throw new Error(`Log request failed with status ${res.status}`);
            }
            const data = await res.json();
            if (requestId !== requestSequence) return;

            const responseLogs = Array.isArray(data.logs) ? data.logs : [];
            if (responseLogs.length > 0) {
                const currentLogs = logs.filter(
                    (log) => log !== CONNECTION_ERROR,
                );
                logs = [...currentLogs, ...responseLogs];
                // Limit local buffer
                if (logs.length > 1000) logs = logs.slice(-1000);
            }

            if (Number.isFinite(Number(data.next_index))) {
                nextIndex = Number(data.next_index);
            }
        } catch (e) {
            if ((e as Error)?.name === "AbortError") return;
            if (requestId !== requestSequence) return;
            if (
                logs.length === 0 ||
                logs[logs.length - 1] !== CONNECTION_ERROR
            ) {
                logs = [...logs, CONNECTION_ERROR];
                // Don't spam error
            }
        } finally {
            if (requestId === requestSequence) {
                fetching = false;
                activeController = null;
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
        mounted = false;
        requestSequence += 1;
        activeController?.abort();
    });

    // Auto-scroll
    $: if (mounted && logs && logEndRef) {
        logEndRef.scrollIntoView({ behavior: "smooth" });
    }

    function clearLogs() {
        logs = [];
        // Preserve the current server cursor. Resetting it to zero would replay
        // the entire bounded server buffer on the next interval.
        requestSequence += 1;
        activeController?.abort();
        activeController = null;
        fetching = false;
        void fetchLogs();
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
