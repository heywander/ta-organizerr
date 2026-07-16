<script lang="ts">
    import LogViewer from "./LogViewer.svelte";

    type TaskName = "symlinks" | "cleanup" | "scan";
    let runningTask: TaskName | null = null;

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

    async function fixSymlinks() {
        if (
            !confirm(
                "Repair broken symlinks? This removes zero-byte crash remnants and rescans the library to recreate valid links.",
            )
        )
            return;

        runningTask = "symlinks";
        try {
            const res = await fetch("/api/maintenance/fix-symlinks", {
                method: "POST",
            });
            const data = await readResponse(res);
            if (data.status === "started") {
                alert("Symlink repair started. Follow progress in the activity log.");
            } else {
                alert(data.message || "The repair request did not start.");
            }
        } catch (error) {
            alert("Repair failed: " + error);
        } finally {
            runningTask = null;
        }
    }

    async function runCleanup() {
        if (!confirm("Remove empty channel and video directories?")) return;

        runningTask = "cleanup";
        try {
            const res = await fetch("/api/cleanup", { method: "POST" });
            await readResponse(res);
            alert("Cleanup started. Follow progress in the activity log.");
        } catch (error) {
            alert("Cleanup failed: " + error);
        } finally {
            runningTask = null;
        }
    }

    async function runScan() {
        if (!confirm("Start a full library scan? This may take a while.")) return;

        runningTask = "scan";
        try {
            const res = await fetch("/api/scan", { method: "POST" });
            await readResponse(res);
            alert("Scan started. Follow progress in the activity log.");
        } catch (error) {
            alert("Scan failed: " + error);
        } finally {
            runningTask = null;
        }
    }
</script>

<div class="maintenance-page">
    <header class="page-heading">
        <div>
            <h1><i class="bi bi-tools"></i> Maintenance</h1>
            <p>Repair library links, remove empty directories, and run a complete rescan.</p>
        </div>
        <span class="page-heading__status">
            <i class="bi bi-hourglass-split" aria-hidden="true"></i>
            {runningTask ? "Submitting request" : "No request pending"}
        </span>
    </header>

    <section class="panel" aria-labelledby="maintenance-actions-title">
        <div class="panel-header">
            <div>
                <h2 class="section-title" id="maintenance-actions-title">Maintenance tasks</h2>
                <p class="section-description">Only one task can be started at a time.</p>
            </div>
        </div>

        <div class="task-list">
            <div class="task-row">
                <span class="task-row__icon"><i class="bi bi-link-45deg"></i></span>
                <div class="task-row__copy">
                    <strong>Repair symlinks</strong>
                    <span>Remove zero-byte remnants and recreate valid library links.</span>
                </div>
                <button class="button-primary" on:click={fixSymlinks} disabled={runningTask !== null}>
                    <i class="bi bi-wrench-adjustable"></i>
                    {runningTask === "symlinks" ? "Starting…" : "Repair"}
                </button>
            </div>

            <div class="task-row">
                <span class="task-row__icon"><i class="bi bi-folder-minus"></i></span>
                <div class="task-row__copy">
                    <strong>Remove empty folders</strong>
                    <span>Delete empty channel and video directories left after moves.</span>
                </div>
                <button class="button" on:click={runCleanup} disabled={runningTask !== null}>
                    <i class="bi bi-trash3"></i>
                    {runningTask === "cleanup" ? "Starting…" : "Clean up"}
                </button>
            </div>

            <div class="task-row">
                <span class="task-row__icon"><i class="bi bi-arrow-clockwise"></i></span>
                <div class="task-row__copy">
                    <strong>Full library scan</strong>
                    <span>Inspect every source item and refresh organized links.</span>
                </div>
                <button class="button" on:click={runScan} disabled={runningTask !== null}>
                    <i class="bi bi-search"></i>
                    {runningTask === "scan" ? "Starting…" : "Run scan"}
                </button>
            </div>
        </div>
    </section>

    <LogViewer endpoint="/api/logs" title="Activity log" />
</div>

<style>
    .maintenance-page {
        display: grid;
        gap: 12px;
    }

    .page-heading {
        display: flex;
        min-height: 58px;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 10px 12px;
        border: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .page-heading h1 {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0;
        color: var(--text);
        font-size: 15px;
        font-weight: 700;
    }

    .page-heading p {
        margin: 3px 0 0;
        color: var(--text-muted);
        font-size: 11px;
    }

    .page-heading__status {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        color: var(--text-muted);
        font-size: 11px;
        white-space: nowrap;
    }

    .task-list {
        display: grid;
    }

    .task-row {
        display: grid;
        grid-template-columns: 32px minmax(0, 1fr) auto;
        min-height: 58px;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border-bottom: 1px solid var(--line-soft);
        background: var(--surface);
    }

    .task-row:last-child {
        border-bottom: 0;
    }

    .task-row:hover {
        background: var(--surface-hover);
    }

    .task-row__icon {
        display: grid;
        width: 30px;
        height: 30px;
        place-items: center;
        border: 1px solid var(--line-soft);
        background: var(--page-deep);
        color: var(--accent);
    }

    .task-row__copy {
        display: grid;
        gap: 2px;
    }

    .task-row__copy strong {
        color: var(--text);
        font-size: 12px;
        font-weight: 600;
    }

    .task-row__copy span {
        color: var(--text-muted);
        font-size: 11px;
    }

    @media (max-width: 560px) {
        .page-heading {
            align-items: stretch;
            flex-direction: column;
            gap: 8px;
        }

        .page-heading__status {
            white-space: normal;
        }

        .task-row {
            grid-template-columns: 30px minmax(0, 1fr);
            padding: 10px;
        }

        .task-row > :global(button) {
            width: 100%;
            min-height: 44px;
            grid-column: 1 / -1;
        }

        .maintenance-page :global(button) {
            min-width: 44px;
            min-height: 44px;
        }
    }
</style>
