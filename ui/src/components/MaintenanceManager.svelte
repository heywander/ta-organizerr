<script lang="ts">
    import { onMount } from "svelte";
    import LogViewer from "./LogViewer.svelte";

    let running = false;

    async function readResponse(res: Response): Promise<any> {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(
                body.error || body.message || `Request failed with status ${res.status}`,
            );
        }
        return body;
    }

    async function fixSymlinks() {
        if (!confirm("Fix broken symlinks? This will:\n\n1. Delete 0-byte files left by crashes\n2. Rescan to recreate proper symlinks\n\nContinue?")) return;
        
        running = true;
        try {
            const res = await fetch("/api/maintenance/fix-symlinks", {
                method: "POST",
            });
            const d = await readResponse(res);
            if (d.status === "started") {
                alert("Symlink repair started. Check logs for progress.");
            } else {
                alert(d.message || "Unknown error");
            }
        } catch (e) {
            alert("Error: " + e);
        } finally {
            running = false;
        }
    }

    async function runCleanup() {
        if (!confirm("Run cleanup? This removes empty directories.")) return;
        
        running = true;
        try {
            const res = await fetch("/api/cleanup", {
                method: "POST",
            });
            await readResponse(res);
            alert("Cleanup started. Check logs.");
        } catch (e) {
            alert("Error: " + e);
        } finally {
            running = false;
        }
    }

    async function runScan() {
        if (!confirm("Run full scan? This may take a while.")) return;
        
        running = true;
        try {
            const res = await fetch("/api/scan", {
                method: "POST",
            });
            await readResponse(res);
            alert("Scan started. Check logs for progress.");
        } catch (e) {
            alert("Error: " + e);
        } finally {
            running = false;
        }
    }
</script>

<div class="space-y-6">
    <!-- Header -->
    <div class="bg-cyber-card p-4 rounded-xl border border-gray-800 shadow-lg">
        <h2 class="text-xl font-bold text-white flex items-center gap-2">
            <i class="bi bi-tools text-neon-pink"></i> Maintenance
        </h2>
        <p class="text-gray-500 text-xs mt-1">
            Repair broken symlinks and clean up after crashes.
        </p>
    </div>

    <!-- Actions Grid -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <!-- Fix Symlinks -->
        <div class="bg-cyber-card p-6 rounded-xl border border-gray-800 text-center">
            <i class="bi bi-link-45deg text-4xl text-neon-cyan mb-3"></i>
            <h3 class="text-white font-bold mb-2">Fix Broken Symlinks</h3>
            <p class="text-gray-500 text-xs mb-4">
                Removes 0-byte files and recreates proper symlinks
            </p>
            <button
                class="btn-primary bg-neon-cyan/20 text-neon-cyan border border-neon-cyan/50 hover:bg-neon-cyan/40 px-4 py-2 rounded transition-colors w-full font-bold"
                on:click={fixSymlinks}
                disabled={running}
            >
                {#if running}
                    <i class="bi bi-arrow-repeat animate-spin"></i> Running...
                {:else}
                    <i class="bi bi-wrench"></i> Fix Symlinks
                {/if}
            </button>
        </div>

        <!-- Cleanup -->
        <div class="bg-cyber-card p-6 rounded-xl border border-gray-800 text-center">
            <i class="bi bi-trash text-4xl text-neon-pink mb-3"></i>
            <h3 class="text-white font-bold mb-2">Clean Empty Folders</h3>
            <p class="text-gray-500 text-xs mb-4">
                Removes empty channel and video directories
            </p>
            <button
                class="btn-primary bg-neon-pink/20 text-neon-pink border border-neon-pink/50 hover:bg-neon-pink/40 px-4 py-2 rounded transition-colors w-full font-bold"
                on:click={runCleanup}
                disabled={running}
            >
                <i class="bi bi-broom"></i> Cleanup
            </button>
        </div>

        <!-- Full Scan -->
        <div class="bg-cyber-card p-6 rounded-xl border border-gray-800 text-center">
            <i class="bi bi-search text-4xl text-neon-yellow mb-3"></i>
            <h3 class="text-white font-bold mb-2">Full Scan</h3>
            <p class="text-gray-500 text-xs mb-4">
                Rescan all videos and recreate symlinks
            </p>
            <button
                class="btn-primary bg-neon-yellow/20 text-neon-yellow border border-neon-yellow/50 hover:bg-neon-yellow/40 px-4 py-2 rounded transition-colors w-full font-bold"
                on:click={runScan}
                disabled={running}
            >
                <i class="bi bi-arrow-clockwise"></i> Rescan All
            </button>
        </div>
    </div>

    <!-- Logs -->
    <div class="bg-black/50 border border-gray-800 rounded-xl p-4 h-[400px]">
        <LogViewer endpoint="/api/logs" title="SYSTEM_LOGS" />
    </div>
</div>
