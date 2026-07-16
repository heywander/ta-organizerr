<script lang="ts">
    export let title: string;
    export let value: number | string;
    export let color: "cyan" | "green" | "pink" | "yellow" | "red" =
        "cyan";
    export let icon = "bi-activity";
</script>

<div class="stat-tile" data-tone={color}>
    <div class="stat-tile__copy">
        <div class="stat-tile__label">{title}</div>
        <div class="stat-tile__value">{value}</div>
    </div>
    <i class="bi {icon} stat-tile__icon" aria-hidden="true"></i>
</div>

<style>
    .stat-tile {
        display: flex;
        min-height: 66px;
        align-items: center;
        justify-content: space-between;
        padding: 9px 11px;
        border: 1px solid var(--line-soft);
        border-top: 2px solid var(--text-muted);
        background: var(--surface);
    }

    .stat-tile[data-tone="green"] {
        border-top-color: var(--success);
    }

    .stat-tile[data-tone="yellow"] {
        border-top-color: var(--warning);
    }

    .stat-tile[data-tone="red"] {
        border-top-color: var(--danger);
    }

    .stat-tile[data-tone="pink"] {
        border-top-color: var(--accent);
    }

    .stat-tile__label {
        color: var(--text-muted);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.02em;
    }

    .stat-tile__value {
        margin-top: 3px;
        color: var(--text);
        font-size: clamp(18px, 2.5vw, 21px);
        font-weight: 650;
        line-height: 1;
        font-variant-numeric: tabular-nums;
    }

    .stat-tile__icon {
        color: var(--text-muted);
        font-size: 17px;
    }

    @media (max-width: 600px) {
        .stat-tile {
            min-height: 72px;
            padding: 10px;
        }

        .stat-tile__copy {
            min-width: 0;
        }

        .stat-tile__label {
            font-size: 11px;
            line-height: 1.25;
        }

        .stat-tile__value {
            overflow-wrap: anywhere;
            font-size: 20px;
            line-height: 1.1;
        }

        .stat-tile__icon {
            margin-left: 6px;
            font-size: 15px;
        }
    }

    @media (max-width: 380px) {
        .stat-tile__icon {
            display: none;
        }
    }
</style>
