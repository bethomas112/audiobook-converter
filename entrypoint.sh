#!/bin/sh
set -e

python -m huey.bin.huey_consumer app.queue.huey -w 1 &
HUEY_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
WEB_PID=$!

trap 'kill -TERM $HUEY_PID $WEB_PID 2>/dev/null' TERM INT

wait -n $HUEY_PID $WEB_PID
EXIT_CODE=$?

kill -TERM $HUEY_PID $WEB_PID 2>/dev/null
wait

exit $EXIT_CODE
