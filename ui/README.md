# TubeSorter UI

This directory contains TubeSorter's Astro pages and Svelte dashboard
components. The production build is static: the Docker build copies `ui/dist`
into the Flask image, and Flask serves the generated pages and assets alongside
the `/api` routes.

## Setup

Run commands from this directory:

```bash
bun install --frozen-lockfile
```

The committed `bun.lock` is the reproducible dependency source. Update it only
when intentionally changing frontend dependencies.

## Commands

```bash
bun run dev      # Astro development server on http://localhost:4321
bun run build    # Production output in ./dist
bun run preview  # Preview the generated static build
```

Dashboard components call relative `/api` URLs. The standalone Astro development
server does not provide the Flask API, so use it for isolated layout work or
configure an intentional local proxy. For an end-to-end check, build the UI and
run the Flask app or Docker Compose so pages and API share one origin.

## Layout

- `src/pages/` defines login, setup, dashboard, maintenance, and transcode pages.
- `src/components/` contains the interactive Svelte managers and controls.
- `src/layouts/` contains the shared HTML shell.
- `src/styles/` contains global styling.
- `public/` contains files copied directly into the build.

Do not commit `node_modules` or generated `dist` output. The production Docker
build runs `bun install --frozen-lockfile` followed by `bun run build`; run the
same build locally before submitting frontend changes.
