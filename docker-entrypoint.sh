#!/bin/sh
# Adjust the in-container 'app' user to match the host volume's ownership,
# then drop privileges and exec the supplied command.
#
# Override the runtime uid/gid via PUID / PGID env vars (defaults: 1000:1000).
# This is the linuxserver.io / Unraid convention.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -g app)" != "$PGID" ]; then
    groupmod -o -g "$PGID" app
fi
if [ "$(id -u app)" != "$PUID" ]; then
    usermod -o -u "$PUID" app
fi

# App-owned dirs: full recursive chown is safe because they only contain
# state we manage (DB, import staging).
for dir in /app/data /app/import; do
    mkdir -p "$dir"
    chown -R "$PUID:$PGID" "$dir"
done

# User-curated dirs: only chown the mount root so the app can create new
# children. Recursive chown could touch huge trees and clobber pre-existing
# host-side ownership. If you're migrating from a root-running container and
# the app can't write into existing folders, run on the host:
#   sudo chown -R <PUID>:<PGID> ./target ./hidden
for dir in /app/target /app/hidden /app/source; do
    if [ -d "$dir" ]; then
        chown "$PUID:$PGID" "$dir" 2>/dev/null || true
    fi
done

exec gosu app "$@"
