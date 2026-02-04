<script lang="ts">
    import { onMount, onDestroy } from "svelte";
    import StatsCard from "./StatsCard.svelte";

    let stats = {
        total_videos: 0,
        verified_links: 0,
        missing_count: 0,
        new_links: 0, // API might not return this directly unless scan happened, let's check API
    };

    // API returns: { total_videos, verified_links, missing_count, videos: [...] }
    // "New/Fixed" was calculated client side in old HTML by checking status diff or something?
    // Old HTML: id="stat-new". But looking at ta_symlink.py:
    // API /api/status returns totals.
    // Wait, the python code `api_status` calculates total, linked, missing.
    // It doesn't seem to persist "new" counts unless a scan JUST ran.
    // The old HTML says "New / Fixed", but where does it get it?
    // Old HTML JS: `document.getElementById('stat-new').textContent = 0;` (default)
    // It updates it?  `updateTable` doesn't update stats.
    // `fetchStatus` updates `stat-total`, `stat-linked`, `stat-error`.
    // `stat-new` is NOT updated in `fetchStatus` in the original HTML!
    // It's only 0? Or maybe I missed where it's updated.
    // Ah, the Python `process_videos` returns new_links count, but that's only after a scan.
    // The `/api/status` endpoint does NOT return `new_links`.
    // So likely "New/Fixed" is only relevant after a scan.
    // I will just omit it or keep it 0 for now.

    let interval: ReturnType<typeof setInterval>;

    async function fetchStats() {
        try {
            const res = await fetch("http://localhost:8002/api/status");
            if (!res.ok) return;
            const data = await res.json();
            stats = {
                total_videos: data.total_videos,
                verified_links: data.verified_links,
                missing_count: data.missing_count,
                new_links: 0, // Placeholder
            };
        } catch (e) {
            console.error("Stats fetch error", e);
        }
    }

    onMount(() => {
        fetchStats();
        interval = setInterval(fetchStats, 5000);
    });

    onDestroy(() => {
        clearInterval(interval);
    });
</script>

<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8 w-full">
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
        title="New / Fixed"
        value={stats.new_links}
        color="yellow"
        icon="bi-stars"
    />
    <StatsCard
        title="Missing / Error"
        value={stats.missing_count}
        color="red"
        icon="bi-exclamation-triangle"
    />
</div>
