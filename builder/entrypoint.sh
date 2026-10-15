#!/bin/sh
# Runs as root: the Engine/Docker may create the ./data bind dirs as root.
# Create and chown the data dirs, then drop to the unprivileged builder user.
set -e
if [ "$(id -u)" = "0" ]; then
    for d in "${EXPORT_DIR:-/data/export}" "${WORK_DIR:-/data/work}"; do
        mkdir -p "$d"
        # Only fix ownership when needed (big exports: avoid a full walk every start).
        if [ "$(stat -c %u "$d")" != "${BUILDER_UID:-1000}" ] || \
           [ -n "$(find "$d" ! -user "${BUILDER_UID:-1000}" -print -quit 2>/dev/null)" ]; then
            chown -R "${BUILDER_UID:-1000}:${BUILDER_GID:-1000}" "$d"
        fi
    done
    exec setpriv --reuid="${BUILDER_UID:-1000}" --regid="${BUILDER_GID:-1000}" --init-groups "$@"
fi
exec "$@"
