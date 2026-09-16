# truenas-discoveryd is killed by its own ExecReload at boot and never restarts

**Product / version:** TrueNAS 26.0.0-BETA.3 (fresh install, single boot environment)
**Component:** truenas-discoveryd (truenas_pydiscovery) — mDNS / NetBIOS-NS / WS-Discovery
**Severity:** Medium — silent, total loss of network discovery; persists until manual intervention

## Summary

`truenas-discoveryd` is terminated by `SIGHUP` shortly after starting at boot. Because
the unit sets `Restart=on-failure`, and systemd's `on-failure` deliberately excludes
deaths by `SIGHUP`/`SIGINT`/`SIGTERM`/`SIGPIPE`, systemd never restarts it. The system
then serves no mDNS, NetBIOS-NS or WS-Discovery at all, indefinitely, with no indication
in the UI that anything is wrong.

On the affected system this went unnoticed for **four days**.

## Impact

* `<hostname>.local` stops resolving.
* SMB shares disappear from the macOS Finder sidebar and Windows network browsing.
* Time Machine cannot discover the target share (a share with `purpose:
  TIMEMACHINE_SHARE` is configured on this system).
* Nothing else is affected — SSH, the web UI and SMB-by-IP keep working, which makes the
  failure look like a client-side DNS problem rather than a dead service.
* **The failure is silent:** Network → Global Configuration still shows
  `service_announcement` = `{netbios: true, mdns: true, wsd: true}`, there is no alert,
  and `midclt call service.query` does not list `discovery`, so an administrator has no
  obvious way to notice or diagnose it.

## Evidence

System booted `2026-09-11 06:14:00`. `systemctl status truenas-discoveryd`, observed four
days later on 2026-09-15:

```
○ truenas-discoveryd.service - TrueNAS Discovery Daemon (mDNS + NetBIOS NS + WS-Discovery)
     Loaded: loaded (/usr/lib/systemd/system/truenas-discoveryd.service; enabled; preset: enabled)
     Active: inactive (dead) since Fri 2026-09-11 06:16:20 PDT; 4 days ago
   Duration: 221ms
    Process: 7179 ExecStart=/usr/bin/truenas-discoveryd -c /etc/truenas-discovery/truenas-discoveryd.conf (code=killed, signal=HUP)
    Process: 7181 ExecReload=/bin/kill -HUP $MAINPID (code=exited, status=0/SUCCESS)
   Main PID: 7179 (code=killed, signal=HUP)
```

Note the pairing: `ExecReload` ran and exited 0, and the main process died of `SIGHUP`
**221 ms** into its lifetime.

Nothing was listening on udp/5353 (`ss -lun | grep 5353` returned no rows) for the whole
period.

Shipped unit, `/usr/lib/systemd/system/truenas-discoveryd.service` (unmodified):

```ini
[Service]
Type=simple
ExecStart=/usr/bin/truenas-discoveryd -c /etc/truenas-discovery/truenas-discoveryd.conf
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
```

## Analysis

Two separate problems, one certain and one probable.

**1. Certain — the restart policy cannot recover from this death.** `Restart=on-failure`
is the one policy that treats `SIGHUP` termination as success. Whatever causes a `SIGHUP`
death, systemd will never restart the service. For a daemon whose documented reload
mechanism *is* `SIGHUP`, this looks like the wrong choice.

**2. Probable — a startup race.** `SIGHUP` is intended as a reload
(`truenas-discoveryd-service.conf(5)`: config is read "on startup and on SIGHUP reload"),
and a reload against a fully started daemon behaves correctly — `midclt call -j
service.control RELOAD discovery` on a running instance leaves it running. The likely
explanation is that middleware reloads the service (as part of regenerating `/etc` during
startup) before the daemon has installed its `SIGHUP` handler, so the default disposition
terminates it. `/usr/bin/truenas-discoveryd` is a Python script, so interpreter start-up
and imports are slow under boot-time load, which would widen that window considerably —
consistent with the 221 ms figure above.

I could not reproduce the kill deterministically on an otherwise idle system: a
stop/start followed by a reload 150 ms later did not kill the daemon. So treat (2) as a
hypothesis; (1) is verifiable from the unit file and systemd's documented behaviour.

Also relevant: `middlewared/plugins/service_/services/discovery.py:reload()` issues
`reload` whenever `get_state()` reports the service running, with no readiness check.

## Suggested fixes

1. **`Restart=always` in the unit** (with the existing `RestartSec=5`). Smallest change;
   makes any `SIGHUP` death self-healing regardless of cause. Note systemd will still not
   restart after an explicit `systemctl stop`, so it does not fight the middleware.
2. **Install the `SIGHUP` handler as the very first action** in the daemon, before any
   slow imports or setup, closing the race window.
3. **`Type=notify` + `sd_notify(READY=1)`**, so systemd knows when the daemon is actually
   ready and a reload cannot be delivered to a half-initialised process.

(1) and (2) together would be belt and braces; (3) is the most correct fix.

## Workaround

```bash
midclt call -j service.control START discovery
```

On the affected system this is now run from a cron task every five minutes, since the
daemon dies on essentially every boot.

## Environment

* TrueNAS 26.0.0-BETA.3, fresh install (not an upgrade), single boot environment.
* Dual-port NIC, one port up (`enp1s0f0`), DHCP with a reservation on the router.
* `service_announcement`: `{netbios: true, mdns: true, wsd: true}`.
* Config present and valid at `/etc/truenas-discovery/` (`truenas-discoveryd.conf`,
  `services.d/`); the daemon starts and works correctly when started manually.
