<script lang="ts">
    import { onMount, onDestroy } from "svelte";
    import StatsCard from "./StatsCard.svelte";
    import LogViewer from "./LogViewer.svelte";
    import VideoTable from "./VideoTable.svelte";
    import DashboardControls from "./DashboardControls.svelte";
    import RecoveryModal from "./RecoveryModal.svelte";
    import HiddenManager from "./HiddenManager.svelte";

    let stats = {
        total_videos: 0,
        verified_links: 0,
        missing_count: 0,
    };

    let videos: any[] = [];
    let loading = true;
    let error: string | null = null;
    let showRecovery = false;
    let showHiddenManager = false;
    let requestInFlight = false;
    let refreshQueued = false;
    let destroyed = false;

    let interval: ReturnType<typeof setInterval>;

    async function fetchData() {
        if (requestInFlight) {
            refreshQueued = true;
            return;
        }
        requestInFlight = true;
        try {
            const res = await fetch("/api/status");
            if (res.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (!res.ok) throw new Error("Failed to fetch status");
            const data = await res.json();

            stats = {
                total_videos: data.total_videos,
                verified_links: data.verified_links,
                missing_count: data.missing_count,
            };
            videos = data.videos || [];
            loading = false;
            error = null;
        } catch (e: any) {
            console.error("Fetch error", e);
            error = e.toString();
            loading = false;
        } finally {
            requestInFlight = false;
            if (refreshQueued && !destroyed) {
                refreshQueued = false;
                void fetchData();
            }
        }
    }

    function handleScanTriggered() {
        setTimeout(fetchData, 2000);
    }

    onMount(() => {
        fetchData();
        interval = setInterval(fetchData, 10000);
    });

    onDestroy(() => {
        destroyed = true;
        clearInterval(interval);
    });

    $: systemStatus = loading ? "Checking" : error ? "Unavailable" : "Available";
    $: systemColor = error ? "red" : loading ? "yellow" : "green";
</script>

<div class="space-y-3">
    <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
        <StatsCard
            title="Total Videos"
            value={stats.total_videos}
            color="cyan"
            icon="bi-collection-play"
        />
        <StatsCard
            title="Linked & Verified"
            value={stats.verified_links}
            color="green"
            icon="bi-link-45deg"
        />
        <StatsCard
            title="Missing / Error"
            value={stats.missing_count}
            color="red"
            icon="bi-exclamation-triangle"
        />
        <StatsCard
            title="Service"
            value={systemStatus}
            color={systemColor}
            icon="bi-cpu"
        />
    </div>

    {#if error}
        <div class="notice notice--error" role="alert">
            Status refresh failed: {error}
        </div>
    {/if}

    <div class="dashboard-grid">
        <section class="panel dashboard-actions">
            <div class="panel-header">
                <h2 class="section-title">
                    <i class="bi bi-sliders"></i> Actions
                </h2>
            </div>
            <div class="p-3">
                <DashboardControls
                    on:scan={handleScanTriggered}
                    on:openRecovery={() => (showRecovery = true)}
                    on:openHidden={() => (showHiddenManager = true)}
                />
            </div>
        </section>

        <section class="dashboard-library">
            <VideoTable {videos} {loading} />
        </section>

        <aside class="dashboard-log">
            <LogViewer />
        </aside>
    </div>

    {#if showRecovery}
        <RecoveryModal on:close={() => (showRecovery = false)} />
    {/if}

    {#if showHiddenManager}
        <HiddenManager on:close={() => (showHiddenManager = false)} />
    {/if}
</div>

<style>
    .dashboard-grid {
        display: grid;
        grid-template-areas:
            "actions library"
            "log library";
        grid-template-columns: 300px minmax(0, 1fr);
        gap: 12px;
        align-items: start;
    }

    .dashboard-actions {
        grid-area: actions;
    }

    .dashboard-library {
        grid-area: library;
        min-width: 0;
    }

    .dashboard-log {
        grid-area: log;
        min-width: 0;
    }

    @media (max-width: 1023px) {
        .dashboard-grid {
            grid-template-areas:
                "actions"
                "library"
                "log";
            grid-template-columns: minmax(0, 1fr);
        }
    }
</style>
