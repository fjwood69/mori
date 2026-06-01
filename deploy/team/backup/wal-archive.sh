#!/bin/sh
# Called by Postgres archive_command: wal-archive %p
# No-ops cleanly if WALG_GS_PREFIX is unset — Postgres receives exit 0
# and continues without error. WAL segments are not archived.
if [ -z "$WALG_GS_PREFIX" ]; then
    exit 0
fi
exec wal-g wal-push "$1"
