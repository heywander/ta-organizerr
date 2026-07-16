<script lang="ts">
    import { createEventDispatcher } from "svelte";

    const dispatch = createEventDispatcher();

    let scanning = false;
    let scanResult: string | null = null;
    let scanState: "neutral" | "error" = "neutral";
    let checkingOrphans = false;
    let orphanResult: string | null = null;
    let orphanState: "neutral" | "success" | "warning" | "error" = "neutral";

    async function readResponse(res: Response): Promise<any> {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(
                body.error ||
                    body.message ||
                    `Request failed with status ${res.status}`,
            );
        }
        return body;
    }

    async function triggerScan() {
        if (!confirm("Start a full library scan?")) return;
        scanning = true;
        scanResult = null;
        scanState = "neutral";
        try {
            const res = await fetch("/api/scan", { method: "POST" });
            const data = await readResponse(res);
            scanResult =
                data.status === "running"
                    ? "A full scan is already running. Follow the system log for progress."
                    : "Full scan started in the background. Follow the system log for progress.";
            dispatch("scan");
        } catch (error) {
            scanResult = "Scan request failed: " + error;
            scanState = "error";
        } finally {
            scanning = false;
        }
    }

    async function checkOrphans() {
        checkingOrphans = true;
        orphanResult = "Checking for orphaned links…";
        orphanState = "neutral";
        try {
            const res = await fetch("/api/check-orphans", {
                method: "POST",
            });
            const data = await readResponse(res);
            if (data.count === 0) {
                orphanResult = "No orphaned links found.";
                orphanState = "success";
            } else {
                orphanResult = `${data.count} orphaned ${data.count === 1 ? "link" : "links"} found.`;
                orphanState = "warning";
            }
        } catch (error) {
            orphanResult = "Orphan check failed: " + error;
            orphanState = "error";
        } finally {
            checkingOrphans = false;
        }
    }
</script>

<div class="operations-grid">
    <button
        class="button-primary operation-primary"
        on:click={triggerScan}
        disabled={scanning}
    >
        <i
            class={`bi ${scanning ? "bi-arrow-repeat operation-spinner" : "bi-arrow-clockwise"}`}
            aria-hidden="true"
        ></i>
        {scanning ? "Submitting…" : "Run full scan"}
    </button>

    <div class="operation-segments">
        <button class="button" on:click={checkOrphans} disabled={checkingOrphans}>
            <i class="bi bi-link-45deg" aria-hidden="true"></i>
            {checkingOrphans ? "Checking…" : "Check links"}
        </button>
        <button class="button" on:click={() => dispatch("openRecovery")}>
            <i class="bi bi-life-preserver" aria-hidden="true"></i>
            Recovery
        </button>
        <button class="button" on:click={() => dispatch("openHidden")}>
            <i class="bi bi-eye-slash" aria-hidden="true"></i>
            Hidden
        </button>
    </div>

    {#if scanResult}
        <div
            class="operation-result"
            data-state={scanState}
            role={scanState === "error" ? "alert" : "status"}
        >
            <span class="status-dot" aria-hidden="true"></span>
            <span>{scanResult}</span>
        </div>
    {/if}

    {#if orphanResult}
        <div
            class="operation-result"
            data-state={orphanState}
            role={orphanState === "error" ? "alert" : "status"}
        >
            <span class="status-dot" aria-hidden="true"></span>
            <span>{orphanResult}</span>
        </div>
    {/if}
</div>

<style>
    .operations-grid {
        display: grid;
        gap: 8px;
    }

    .operation-primary {
        width: 100%;
    }

    .operation-segments {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .operation-segments :global(.button) {
        min-width: 0;
        padding-inline: 6px;
        border-right-width: 0;
        font-size: 11px;
    }

    .operation-segments :global(.button:last-child) {
        border-right-width: 1px;
    }

    .operation-result {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        min-height: 32px;
        padding: 7px 9px;
        border: 1px solid var(--line-soft);
        background: var(--page-deep);
        color: var(--text-soft);
        font-size: 11px;
    }

    .operation-result .status-dot {
        flex: 0 0 auto;
        margin-top: 4px;
    }

    .operation-result[data-state="warning"] .status-dot {
        background: var(--warning);
    }

    .operation-result[data-state="neutral"] .status-dot {
        background: var(--accent);
    }

    .operation-result[data-state="error"] {
        border-color: var(--danger);
        color: #f0d4ce;
    }

    .operation-result[data-state="error"] .status-dot {
        background: var(--danger);
    }

    .operation-spinner {
        animation: operation-spin 900ms linear infinite;
    }

    @keyframes operation-spin {
        to {
            transform: rotate(360deg);
        }
    }
</style>
