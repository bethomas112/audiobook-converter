#!/bin/bash
# Runs the two processes this app needs inside one container: the Huey
# consumer (actually converts books - see app/queue.py) and the FastAPI
# web server (serves the UI and runs the inbox watcher - see app/main.py).
# They only ever talk to each other through the shared SQLite database,
# never directly.
#
# `wait -n` blocks until EITHER process exits, then the trap kills whatever
# is still running and this script exits with the same code - so a crash
# in either one stops the container instead of quietly leaving the other
# half running (which Docker's restart policy would never see and fix).
set -e

python -m huey.bin.huey_consumer app.queue.huey -w 1 &
HUEY_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-2012}" &
WEB_PID=$!

trap 'kill -TERM $HUEY_PID $WEB_PID 2>/dev/null' TERM INT

wait -n $HUEY_PID $WEB_PID
EXIT_CODE=$?

kill -TERM $HUEY_PID $WEB_PID 2>/dev/null
wait

exit $EXIT_CODE
