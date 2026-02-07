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

    let interval: ReturnType<typeof setInterval>;

    async function fetchData() {
        try {
            const res = await fetch("/api/status");
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
        }
    }

    function handleScanTriggered() {
        // Maybe show a toast or log
        // Refetch data soon
        setTimeout(fetchData, 2000);
    }

    onMount(() => {
        fetchData();
        interval = setInterval(fetchData, 10000); // reduced polling for full list
    });

    onDestroy(() => {
        clearInterval(interval);
    });
</script>

<div class="space-y-8">
    <!-- Stats Row -->
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
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
        <!-- Placeholder for symmetry or future stat -->
        <StatsCard
            title="System Status"
            value="ONLINE"
            color="pink"
            icon="bi-cpu"
        />
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div class="lg:col-span-1 space-y-6">
            <div
                class="bg-cyber-card border border-gray-800 rounded-xl p-6 shadow-lg relative overflow-hidden group"
            >
                <div
                    class="absolute inset-0 bg-neon-cyan/5 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none"
                ></div>
                <h3
                    class="text-lg font-bold mb-4 flex items-center text-white neon-text-cyan"
                >
                    <i class="bi bi-joystick mr-2"></i> Control Deck
                </h3>
                <DashboardControls
                    on:scan={handleScanTriggered}
                    on:openRecovery={() => (showRecovery = true)}
                    on:openHidden={() => (showHiddenManager = true)}
                />
            </div>

            <LogViewer />
        </div>

        <div class="lg:col-span-2">
            <VideoTable {videos} {loading} />
        </div>
    </div>

    {#if showRecovery}
        <RecoveryModal on:close={() => (showRecovery = false)} />
    {/if}

    {#if showHiddenManager}
        <HiddenManager on:close={() => (showHiddenManager = false)} />
    {/if}
</div>
