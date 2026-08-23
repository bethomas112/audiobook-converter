#!/bin/bash
# Runs the three processes this app needs inside one container: two Huey
# consumers (app/queue.py) and the FastAPI web server (serves the UI and
# runs the inbox watcher - see app/main.py). The two consumers are
# deliberately separate processes/queues, not just separate task types on
# one worker - app.queue.huey handles conversion (process_job), which can
# run for tens of minutes, while app.queue.lookup_huey handles detection
# and metadata search (start_job), which normally finishes in under a
# second. A single shared worker would leave a freshly-dropped book's
# lookup stuck behind an unrelated in-progress conversion for however long
# that conversion takes. All three processes only ever talk to each other
# through the shared SQLite database, never directly.
#
# Always invoked by entrypoint.sh via `gosu` after it has dropped root -
# everything below runs as the unprivileged app user.
#
# `wait -n` blocks until ANY process exits, then the trap kills whatever
# is still running and this script exits with the same code - so a crash
# in any one of them stops the container instead of quietly leaving the
# others running (which Docker's restart policy would never see and fix).
set -e

python -m huey.bin.huey_consumer app.queue.huey -w 1 &
HUEY_PID=$!

python -m huey.bin.huey_consumer app.queue.lookup_huey -w 1 &
LOOKUP_HUEY_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-2012}" &
WEB_PID=$!

trap 'kill -TERM $HUEY_PID $LOOKUP_HUEY_PID $WEB_PID 2>/dev/null' TERM INT

wait -n $HUEY_PID $LOOKUP_HUEY_PID $WEB_PID
EXIT_CODE=$?

kill -TERM $HUEY_PID $LOOKUP_HUEY_PID $WEB_PID 2>/dev/null
wait

exit $EXIT_CODE
