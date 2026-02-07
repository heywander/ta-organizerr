<script lang="ts">
    import { createEventDispatcher } from "svelte";
    const dispatch = createEventDispatcher();

    let scanning = false;
    let checkingOrphans = false;
    let orphanResult: string | null = null;

    async function triggerScan() {
        if (!confirm("Start full library scan?")) return;
        scanning = true;
        try {
            await fetch("/api/scan", { method: "POST" });
            dispatch("scan");
            // Reset scanning state after a bit since it's async background
            setTimeout(() => (scanning = false), 2000);
        } catch (e) {
            alert("Error: " + e);
            scanning = false;
        }
    }

    async function checkOrphans() {
        checkingOrphans = true;
        orphanResult = "Measuring quantum fluctuations (scanning)...";
        try {
            const res = await fetch("/api/check-orphans", {
                method: "POST",
            });
            const data = await res.json();
            if (data.count === 0) {
                orphanResult = "✅ All systems nominal. No orphans.";
            } else {
                orphanResult = `⚠️ Found ${data.count} orphaned links!`;
            }
        } catch (e) {
            orphanResult = "❌ Sensor Error: " + e;
        } finally {
            checkingOrphans = false;
        }
    }
</script>

<div class="flex flex-col gap-4">
    <button
        class="btn-cyber-primary w-full py-4 text-lg font-bold uppercase tracking-wider shadow-lg flex items-center justify-center gap-2 group relative overflow-hidden"
        on:click={triggerScan}
        disabled={scanning}
    >
        {#if scanning}
            <span class="animate-spin"><i class="bi bi-arrow-repeat"></i></span>
            Scanning...
        {:else}
            <div
                class="absolute inset-0 bg-neon-cyan/20 translate-y-full group-hover:translate-y-0 transition-transform duration-300"
            ></div>
            <i
                class="bi bi-arrow-repeat group-hover:rotate-180 transition-transform duration-500"
            ></i> Run Full Scan
        {/if}
    </button>

    <div class="grid grid-cols-2 gap-3">
        <button
            class="btn-cyber-secondary py-3 text-sm font-semibold border border-neon-yellow/30 text-neon-yellow hover:bg-neon-yellow/10 transition-colors rounded-lg flex items-center justify-center gap-2"
            on:click={checkOrphans}
            disabled={checkingOrphans}
        >
            <i class="bi bi-binoculars"></i> Check Orphans
        </button>

        <button
            class="btn-cyber-secondary py-3 text-sm font-semibold border border-neon-pink/30 text-neon-pink hover:bg-neon-pink/10 transition-colors rounded-lg flex items-center justify-center gap-2"
            on:click={() => dispatch("openRecovery")}
        >
            <i class="bi bi-bandaid"></i> Recovery Mode
        </button>

        <button
            class="btn-cyber-secondary py-3 text-sm font-semibold border border-purple-500/30 text-purple-400 hover:bg-purple-500/10 transition-colors rounded-lg flex items-center justify-center gap-2 col-span-2"
            on:click={() => dispatch("openHidden")}
        >
            <i class="bi bi-eye-slash"></i> Hidden Channels
        </button>
    </div>

    {#if orphanResult}
        <div
            class="mt-2 p-3 rounded bg-black/40 border border-gray-700 text-xs font-mono"
        >
            {orphanResult}
        </div>
    {/if}
</div>

<style>
    .btn-cyber-primary {
        background: linear-gradient(45deg, #00f3ff, #0066ff);
        color: black;
        border: none;
        border-radius: 0.5rem;
        transition: all 0.3s ease;
    }
    .btn-cyber-primary:hover {
        filter: brightness(1.2);
        box-shadow: 0 0 20px rgba(0, 243, 255, 0.4);
    }
    .btn-cyber-primary:disabled {
        background: #333;
        color: #666;
        cursor: not-allowed;
    }
</style>
