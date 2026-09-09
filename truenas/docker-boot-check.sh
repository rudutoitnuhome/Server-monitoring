#!/bin/bash
# POSTINIT recovery for the TrueNAS docker boot race.
#
# TrueNAS starts docker early in boot. Its pre-start check
# (middlewared/utils/interface.py:get_default_interface) needs a default route,
# but it waits up to 60s for the interface LINK and not at all for a ROUTE — so
# a DHCP lease that lands late makes the check return None and docker is marked
#     FAILED: [EFAULT] Unable to determine default interface
# Nothing then retries: docker.state.periodic_check runs once every 24h and only
# syncs catalogs/images, so the apps stay down until someone intervenes.
#
# This script waits for the route, then starts docker if the middleware gave up.
# Install as an initshutdownscript task, when=POSTINIT, timeout>=300.

LOG="$(dirname "$0")/docker-boot-check.log"
exec >> "$LOG" 2>&1
echo "=== $(date '+%F %T') postinit docker check"

status() {
    midclt call docker.status 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","?"))' 2>/dev/null
}

# 1. Wait for the default route — the thing the race is actually about.
for i in $(seq 1 60); do
    if ip route show default | grep -q .; then
        echo "default route present after ${i}s"
        break
    fi
    sleep 1
done
ip route show default | grep -q . || echo "WARNING: still no default route after 60s"

# 2. Give the middleware's own startup a chance to succeed on its own.
s=""
for i in $(seq 1 12); do
    s=$(status)
    echo "  t=$((i * 10))s docker=$s"
    case "$s" in
        RUNNING) echo "already running — nothing to do"; exit 0 ;;
        FAILED)  break ;;
    esac
    sleep 10
done

# 3. Only intervene on a real failure; never race a start already in progress.
case "$s" in
    FAILED|STOPPED|"")
        echo "starting docker (status=${s:-unknown})"
        midclt call docker.state.start_service true 2>&1
        sleep 5
        echo "docker now: $(status)"
        midclt call app.query 2>/dev/null | python3 -c \
            'import json,sys; [print("   ", a["name"], a["state"]) for a in json.load(sys.stdin)]' 2>/dev/null
        ;;
    *)
        echo "docker status=$s — leaving it alone"
        ;;
esac
echo "=== $(date '+%F %T') done"
