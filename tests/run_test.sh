#!/usr/bin/env bash
# End-to-end test for ntfy-bridge and scheduler. Usage: ./tests/run_test.sh
set -uo pipefail
cd "$(dirname "$0")"
C="docker compose -f docker-compose.test.yml"

cleanup() { $C down -v --remove-orphans >/dev/null 2>&1; }
trap cleanup EXIT
cleanup

# The expected ON/OFF outcome depends on the clock, so the schedule is
# generated fresh for whatever time the test runs at.
python3 gen_schedule.py > schedule.yml
echo "--- generated schedule ---"; cat schedule.yml

$C build >/dev/null 2>&1 || { echo "image build failed"; $C build; exit 1; }
$C up -d mosquitto mock ntfy-bridge >/dev/null 2>&1 || { echo "stack failed to start"; exit 1; }
sleep 5
$C up -d driver >/dev/null 2>&1
sleep 4
# scheduler starts last so the driver is already subscribed to its topics
$C up -d scheduler >/dev/null 2>&1

DRIVER=$($C ps -q driver)
docker wait "$DRIVER" >/dev/null
RC=$(docker inspect -f '{{.State.ExitCode}}' "$DRIVER")

echo "=================== driver output ==================="
docker logs "$DRIVER"
echo "=================== bridge log ======================"
$C logs --no-log-prefix ntfy-bridge
echo "=================== scheduler log ==================="
$C logs --no-log-prefix scheduler
echo "====================================================="

# A crash inside the paho callback tears down the connection and the bridge
# reconnects, so more than one "connected" line means it fell over mid-run.
CONNECTS=$($C logs --no-log-prefix ntfy-bridge 2>/dev/null | grep -c "connected to mqtt broker")
if [ "$CONNECTS" -eq 1 ]; then
  echo "PASS  bridge held a single MQTT connection for the whole run"
else
  echo "FAIL  bridge reconnected $CONNECTS times (expected 1) — it crashed mid-run"
  RC=1
fi

echo "driver exit code: $RC"
exit "$RC"
