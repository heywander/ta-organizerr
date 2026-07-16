<script lang="ts">
    import { createEventDispatcher, onDestroy, onMount } from "svelte";

    export let show = false;

    const dispatch = createEventDispatcher();

    let hiddenChannels: string[] = [];
    let newChannel = "";
    let loading = true;
    let submitting = false;
    let error: string | null = null;
    let closeButton: HTMLButtonElement;
    let dialogElement: HTMLElement;
    let modalReturnFocus: HTMLElement | null = null;
    let previousBodyOverflow = "";

    function closeModal() {
        dispatch("close");
    }

    function handleKeydown(event: KeyboardEvent) {
        if (event.key === "Tab") {
            const focusable = Array.from(
                dialogElement?.querySelectorAll<HTMLElement>(
                    'button:not(:disabled), input:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
                ) || [],
            );
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (first && last) {
                if (!dialogElement.contains(document.activeElement)) {
                    event.preventDefault();
                    (event.shiftKey ? last : first).focus();
                } else if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            closeModal();
        }
    }

    async function fetchHidden() {
        loading = true;
        try {
            const res = await fetch("/api/hidden");
            if (!res.ok) throw new Error("Failed to fetch hidden list");
            const data = await res.json();
            hiddenChannels = data.channels || [];
            error = null;
        } catch (caught: any) {
            error = caught.message;
        } finally {
            loading = false;
        }
    }

    async function addChannel() {
        const channel = newChannel.trim();
        if (!channel || submitting) return;
        submitting = true;
        error = null;
        try {
            const res = await fetch("/api/hidden", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ channel }),
            });
            if (!res.ok) throw new Error("Failed to add channel");
            newChannel = "";
            await fetchHidden();
        } catch (caught: any) {
            error = caught.message;
        } finally {
            submitting = false;
        }
    }

    async function removeChannel(channel: string) {
        if (
            !confirm(
                `Unhide channel "${channel}"? It will return to the public target on the next scan.`,
            )
        )
            return;
        error = null;
        try {
            const res = await fetch("/api/hidden", {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ channel }),
            });
            if (!res.ok) throw new Error("Failed to remove channel");
            await fetchHidden();
        } catch (caught: any) {
            error = caught.message;
        }
    }

    onMount(() => {
        modalReturnFocus =
            document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        previousBodyOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        void fetchHidden();
        closeButton?.focus();
    });

    onDestroy(() => {
        document.body.style.overflow = previousBodyOverflow;
        if (modalReturnFocus?.isConnected) modalReturnFocus.focus();
    });
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop hidden-backdrop">
    <section
        bind:this={dialogElement}
        class="modal-surface hidden-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="hidden-channels-title"
    >
        <header class="panel-header">
            <div>
                <h2 class="section-title" id="hidden-channels-title">
                    <i class="bi bi-eye-slash"></i>
                    Hidden channels
                </h2>
                <p class="section-description">Route selected channels outside the public library.</p>
            </div>
            <button
                bind:this={closeButton}
                class="button-quiet icon-button"
                on:click={closeModal}
                aria-label="Close hidden channels"
            >
                <i class="bi bi-x-lg"></i>
            </button>
        </header>

        <div class="hidden-body">
            <p class="notice">
                <i class="bi bi-info-circle" aria-hidden="true"></i>
                Channels listed here synchronize to <code>/hidden</code> instead of
                <code>/target</code>.
            </p>

            <form class="hidden-form" on:submit|preventDefault={addChannel}>
                <label for="hidden-channel-name">Exact channel name</label>
                <div class="hidden-form__controls">
                    <input
                        id="hidden-channel-name"
                        type="text"
                        bind:value={newChannel}
                        placeholder="Channel name"
                        autocomplete="off"
                    />
                    <button class="button-primary" type="submit" disabled={submitting || !newChannel.trim()}>
                        <i class="bi bi-plus-lg"></i>
                        {submitting ? "Adding…" : "Add"}
                    </button>
                </div>
            </form>

            <div class="hidden-list" aria-busy={loading}>
                <div class="hidden-list__heading">
                    <span>Channel</span>
                    <span>{hiddenChannels.length} hidden</span>
                </div>
                {#if loading}
                    <div class="hidden-empty">Loading hidden channels…</div>
                {:else if hiddenChannels.length === 0}
                    <div class="hidden-empty">No hidden channels.</div>
                {:else}
                    {#each hiddenChannels as channel}
                        <div class="hidden-row">
                            <span class="hidden-row__name" title={channel}>
                                <i class="bi bi-folder-fill" aria-hidden="true"></i>
                                {channel}
                            </span>
                            <button
                                class="button-quiet"
                                title={`Return ${channel} to the public library`}
                                on:click={() => removeChannel(channel)}
                            >
                                <i class="bi bi-eye"></i>
                                Unhide
                            </button>
                        </div>
                    {/each}
                {/if}
            </div>

            {#if error}
                <div class="notice notice--error" role="alert">{error}</div>
            {/if}
        </div>

        <footer class="hidden-footer">
            <button class="button" on:click={closeModal}>Close</button>
        </footer>
    </section>
</div>

<style>
    .hidden-backdrop {
        position: fixed;
        inset: 0;
        z-index: 90;
        display: grid;
        place-items: center;
        padding: 16px;
    }

    .hidden-dialog {
        display: flex;
        width: min(560px, 100%);
        max-height: calc(100dvh - 24px);
        flex-direction: column;
        overflow: hidden;
    }

    .icon-button {
        width: 32px;
        padding: 0;
    }

    .hidden-body {
        display: grid;
        min-height: 0;
        gap: 12px;
        padding: 12px;
        overflow-y: auto;
    }

    .hidden-body .notice {
        display: flex;
        gap: 8px;
        margin: 0;
    }

    .hidden-body code {
        color: var(--text);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    .hidden-form {
        display: grid;
        gap: 5px;
    }

    .hidden-form > label {
        color: var(--text-soft);
        font-size: 11px;
        font-weight: 600;
    }

    .hidden-form__controls {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 6px;
    }

    .hidden-form input {
        width: 100%;
        padding-inline: 9px;
    }

    .hidden-list {
        max-height: min(330px, 45vh);
        overflow-y: auto;
        border: 1px solid var(--line-soft);
        background: var(--page-deep);
    }

    .hidden-list__heading,
    .hidden-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
    }

    .hidden-list__heading {
        min-height: 28px;
        padding: 0 9px;
        border-bottom: 1px solid var(--line-soft);
        background: var(--surface-raised);
        color: var(--text-muted);
        font-size: 10px;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    .hidden-row {
        min-height: 36px;
        padding-left: 9px;
        border-bottom: 1px solid var(--line-soft);
    }

    .hidden-row:last-child {
        border-bottom: 0;
    }

    .hidden-row:hover {
        background: var(--surface-hover);
    }

    .hidden-row__name {
        display: flex;
        min-width: 0;
        align-items: center;
        gap: 8px;
        overflow: hidden;
        color: var(--text);
        font-size: 12px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .hidden-row__name i {
        color: var(--text-soft);
    }

    .hidden-empty {
        padding: 26px 12px;
        color: var(--text-muted);
        font-size: 12px;
        text-align: center;
    }

    .hidden-footer {
        display: flex;
        justify-content: flex-end;
        padding: 8px 12px;
        border-top: 1px solid var(--line-soft);
        background: var(--surface-raised);
    }

    @media (max-width: 480px) {
        .hidden-backdrop {
            padding: 0;
        }

        .hidden-dialog {
            width: 100%;
            height: 100dvh;
            max-height: 100dvh;
        }

        .hidden-dialog > header {
            min-height: 56px;
            padding-top: max(7px, env(safe-area-inset-top));
            padding-right: max(10px, env(safe-area-inset-right));
            padding-left: max(10px, env(safe-area-inset-left));
        }

        .hidden-body {
            padding-right: max(12px, env(safe-area-inset-right));
            padding-left: max(12px, env(safe-area-inset-left));
        }

        .hidden-body .notice {
            flex-wrap: wrap;
            overflow-wrap: anywhere;
            line-height: 1.45;
        }

        .hidden-form__controls {
            grid-template-columns: 1fr;
            gap: 8px;
        }

        .hidden-form input,
        .hidden-form button {
            min-height: 44px;
            font-size: 16px;
        }

        .hidden-form button {
            width: 100%;
        }

        .icon-button,
        .hidden-row button,
        .hidden-footer button {
            min-height: 44px;
        }

        .icon-button {
            width: 44px;
        }

        .hidden-row {
            min-height: 56px;
            gap: 8px;
            padding: 6px 6px 6px 10px;
        }

        .hidden-row__name {
            overflow: visible;
            font-size: 14px;
            line-height: 1.35;
            overflow-wrap: anywhere;
            text-overflow: clip;
            white-space: normal;
        }

        .hidden-footer {
            min-height: 60px;
            padding-right: max(12px, env(safe-area-inset-right));
            padding-bottom: max(8px, env(safe-area-inset-bottom));
            padding-left: max(12px, env(safe-area-inset-left));
        }

        .hidden-footer button {
            min-width: 104px;
        }
    }
</style>
