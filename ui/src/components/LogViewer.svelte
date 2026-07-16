<script lang="ts">
    import { onMount, onDestroy, tick } from "svelte";

    export let endpoint = "/api/logs";
    export let title = "System log";

    let logs: string[] = [];
    let logEndRef: HTMLDivElement;
    let logBodyRef: HTMLDivElement;
    let interval: ReturnType<typeof setInterval>;
    let nextIndex = 0;
    let mounted = false;
    let fetching = false;
    let requestSequence = 0;
    let activeController: AbortController | null = null;
    let following = true;
    let connectionState: "checking" | "connected" | "error" = "checking";

    const CONNECTION_ERROR = "Log connection unavailable.";

    $: displayTitle = title;

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
            connectionState = "connected";

            const responseLogs = Array.isArray(data.logs) ? data.logs : [];
            if (responseLogs.length > 0) {
                const currentLogs = logs.filter(
                    (log) => log !== CONNECTION_ERROR,
                );
                logs = [...currentLogs, ...responseLogs];
                if (logs.length > 1000) logs = logs.slice(-1000);
            }

            if (Number.isFinite(Number(data.next_index))) {
                nextIndex = Number(data.next_index);
            }
        } catch (e) {
            if ((e as Error)?.name === "AbortError") return;
            if (requestId !== requestSequence) return;
            connectionState = "error";
            if (
                logs.length === 0 ||
                logs[logs.length - 1] !== CONNECTION_ERROR
            ) {
                logs = [...logs, CONNECTION_ERROR];
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

    async function scrollToEnd() {
        await tick();
        if (following && logBodyRef) {
            logBodyRef.scrollTop = logBodyRef.scrollHeight;
        }
    }

    function handleLogScroll() {
        if (!logBodyRef) return;
        const distanceFromBottom =
            logBodyRef.scrollHeight -
            logBodyRef.scrollTop -
            logBodyRef.clientHeight;
        following = distanceFromBottom <= 16;
    }

    function resumeFollowing() {
        following = true;
        void scrollToEnd();
    }

    $: if (mounted && logs && logEndRef && following) {
        void scrollToEnd();
    }

    function clearLogs() {
        logs = [];
        // Preserve the server cursor so clearing does not replay the buffer.
        requestSequence += 1;
        activeController?.abort();
        activeController = null;
        fetching = false;
        void fetchLogs();
    }
</script>

<section class="panel log-panel" aria-labelledby="log-heading">
    <header class="section-header log-panel__header">
        <h2 id="log-heading" class="section-title">
            <span
                class="status-dot log-status"
                data-state={connectionState}
                role="img"
                aria-label={connectionState === "connected"
                    ? "Log connected"
                    : connectionState === "error"
                      ? "Log unavailable"
                      : "Checking log connection"}
            ></span>
            {displayTitle}
        </h2>
        <div class="log-panel__actions">
            <button
                type="button"
                on:click={resumeFollowing}
                class="button-quiet log-panel__control"
                aria-pressed={following}
            >
                <i class={`bi ${following ? "bi-arrow-down-circle-fill" : "bi-pause-circle"}`}></i>
                {following ? "Following" : "Resume"}
            </button>
            <button
                type="button"
                on:click={clearLogs}
                class="button-quiet log-panel__control"
            >Clear</button>
        </div>
    </header>
    <div
        bind:this={logBodyRef}
        class="log-panel__body"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        on:scroll={handleLogScroll}
    >
        {#each logs as log, i}
            <div class:error-line={log === CONNECTION_ERROR} class="log-line">
                <span class="log-line__index" aria-hidden="true">{i + 1}</span>
                <span>{log}</span>
            </div>
        {/each}
        {#if logs.length === 0}
            <div class="log-panel__empty">
                Waiting for {displayTitle.toLowerCase()} entries…
            </div>
        {/if}
        <div bind:this={logEndRef}></div>
    </div>
</section>

<style>
    .log-panel {
        display: flex;
        min-height: 250px;
        flex-direction: column;
        overflow: hidden;
    }

    .log-panel__header {
        min-height: 38px;
    }

    .log-status[data-state="checking"] {
        background: var(--warning);
    }

    .log-status[data-state="error"] {
        background: var(--danger);
        border-color: #f0d4ce;
    }

    .log-panel__actions {
        display: flex;
        align-items: center;
    }

    .log-panel__control {
        min-height: 26px;
        padding: 3px 7px;
        border-left: 1px solid var(--line-soft);
        font-size: 10px;
    }

    .log-panel__body {
        height: 300px;
        overflow-y: auto;
        background: var(--page-deep);
        color: var(--text-soft);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 10px;
        line-height: 1.5;
    }

    .log-line {
        display: grid;
        grid-template-columns: 42px minmax(0, 1fr);
        min-height: 25px;
        padding: 4px 9px 4px 0;
        border-bottom: 1px solid color-mix(in srgb, var(--line-soft) 58%, transparent);
        overflow-wrap: anywhere;
    }

    .log-line:hover {
        background: color-mix(in srgb, var(--surface-hover) 62%, transparent);
    }

    .log-line__index {
        padding-right: 9px;
        color: var(--text-muted);
        text-align: right;
        user-select: none;
    }

    .error-line {
        color: #f0d4ce;
    }

    .log-panel__empty {
        padding: 48px 12px;
        color: var(--text-muted);
        text-align: center;
    }

    @media (max-width: 600px) {
        .log-panel__header {
            min-height: 52px;
            gap: 6px;
        }

        .log-panel__control {
            min-height: 44px;
            padding: 6px 10px;
            font-size: 12px;
        }

        .log-panel__body {
            height: min(320px, 52dvh);
            font-size: 12px;
            line-height: 1.55;
            overscroll-behavior: contain;
        }

        .log-line {
            grid-template-columns: 30px minmax(0, 1fr);
            min-height: 32px;
            padding: 6px 9px 6px 0;
        }

        .log-line__index {
            padding-right: 7px;
        }
    }
</style>
