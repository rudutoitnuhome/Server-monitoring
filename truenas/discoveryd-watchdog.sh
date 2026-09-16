#!/bin/bash
# Revive truenas-discoveryd after it is killed by SIGHUP.
#
# TrueNAS 26 serves mDNS + NetBIOS-NS + WS-Discovery from truenas-discoveryd
# (there is no avahi). Observed: it was killed by SIGHUP 221 ms into its run at
# boot and stayed dead for four days. The staying-dead part is the certain bit —
# the unit sets Restart=on-failure, and systemd's on-failure deliberately
# EXCLUDES deaths by SIGHUP/SIGINT/SIGTERM/SIGPIPE, so nothing revives it.
# (The unit's ExecReload is `kill -HUP $MAINPID`, but a reload against a
# running daemon does NOT kill it, so the exact trigger is unconfirmed — most
# likely a reload racing the daemon's handler setup during startup.) Symptoms:
# truenas.local stops resolving, SMB shares vanish from Finder, Time Machine
# cannot discover the target. `systemctl status truenas-discoveryd` shows
# `code=killed, signal=HUP`.
#
# Run from cron every few minutes. Logs only when it actually intervenes, so
# the log doubles as a record of how often the bug fires.

LOG="$(dirname "$0")/discoveryd-watchdog.log"

systemctl is-active --quiet truenas-discoveryd && exit 0

# Respect the admin's setting: if every announcement type is switched off in
# Network -> Global Configuration, the daemon is meant to be down. Leave it.
if ! midclt call network.configuration.config 2>/dev/null | python3 -c '
import json, sys
a = json.load(sys.stdin).get("service_announcement") or {}
sys.exit(0 if any(a.get(k) for k in ("mdns", "netbios", "wsd")) else 1)'; then
    exit 0
fi

{
    echo "=== $(date '+%F %T') truenas-discoveryd is down — restarting"
    systemctl status truenas-discoveryd 2>&1 | grep -E "Active:|code=killed" | sed 's/^/  was: /'
    midclt call -j service.control START discovery 2>&1 | tail -1
    sleep 2
    echo "  now: $(systemctl is-active truenas-discoveryd), udp/5353 sockets: $(ss -lun 2>/dev/null | grep -c 5353)"
} >> "$LOG" 2>&1
