#!/bin/bash
# Runs as root (the image's default user) just long enough to line up the
# baked-in `appuser` with the PUID/PGID the operator asked for, fix
# ownership of the data directories, then permanently drop root before
# the app itself ever runs - see run.sh for the actual processes.
#
# Same PUID/PGID convention as the linuxserver.io-style images (sonarr,
# etc.): unset means "run as UID/GID 1000", matching appuser's baked-in
# default so the usermod/groupmod calls below are no-ops in that case.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

groupmod -o -g "$PGID" appuser
usermod -o -u "$PUID" appuser

# Non-recursive: fixes ownership of the mount points themselves without
# walking a potentially large existing archive/output library on every
# container start. New files appuser creates are already owned correctly;
# this only matters for directories a bind mount brought in from the host.
chown appuser:appuser /data/inbox /data/work /data/archive /data/output /data/config

exec gosu appuser ./run.sh
