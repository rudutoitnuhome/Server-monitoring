# Server Monitor → MQTT → Home Assistant

A small Python agent that reads **CPU**, **hard-drive**, and **NVIDIA GPU**
temperatures from a Linux host and publishes them to MQTT using
**Home Assistant MQTT Discovery**. Run it on each machine (Proxmox,
TrueNAS SCALE, Plex); each host shows up as its own *device* in Home Assistant
with one temperature sensor per CPU / disk / GPU.

## How it works

| Metric | Source | Notes |
|--------|--------|-------|
| CPU temp | `sensors -j` (lm-sensors) | Falls back to `/sys/class/hwmon` if lm-sensors isn't installed. Optional per-core entities. |
| Disk temps | `smartctl` (smartmontools) | One entity per drive (SATA, SAS, NVMe). Skips drives in **standby** so it won't wake sleeping disks. |
| GPU temps | `nvidia-smi` | One entity per NVIDIA GPU. Silently skipped when no GPU/driver is present. |

The agent publishes a single retained JSON state topic per host plus a retained
availability topic (with MQTT Last-Will), and HA discovery configs that map each
field to a sensor via `value_template`.

```
server-monitor/<node>/state          {"cpu_temp":45.0,"disk_xxxx_temp":38.0,"gpu0_temp":51.0}
server-monitor/<node>/availability   online | offline
homeassistant/sensor/<node>_<key>/config   (discovery payloads, retained)
```

## Requirements on each host

Linux with Python 3.9+. Install the sensor tools you need:

```bash
# Proxmox / TrueNAS SCALE (Debian-based)
apt install lm-sensors smartmontools
sensors-detect --auto        # one-time, for CPU sensors
# NVIDIA driver provides nvidia-smi automatically
```

> On TrueNAS SCALE the root filesystem is largely read-only; install into a
> dataset/app or run from a path under `/mnt`. The tools above ship with SCALE.

## Install

```bash
git clone <this repo> server-monitoring
cd server-monitoring
sudo ./install.sh
sudo nano /etc/server-monitor/config.yaml   # set MQTT broker + node_name
sudo systemctl start server-monitor
journalctl -u server-monitor -f
```

Repeat on each machine, giving each a distinct `node_name`
(e.g. `proxmox`, `truenas`, `plex`).

## Configuration

See [`config.example.yaml`](config.example.yaml). Key fields:

- `mqtt.host` / `port` / `username` / `password` / `tls`
- `node_name` — the device name in HA (defaults to hostname)
- `interval` — seconds between readings (default 60)
- `cpu.per_core`, `disks.skip_standby`, and per-source `enabled` toggles

## Test on a new host

Run a one-shot dry run that reads the sensors, prints them, and exits **without
touching MQTT** — the quickest way to confirm a host's tools work:

```bash
sudo /opt/server-monitor/venv/bin/python /opt/server-monitor/server_monitor.py --once
```

Example output:

```
Node: proxmox
  CPU Temperature           52.4 °C   [cpu_temp]
  Disk sda (WDC WD40EFRX)   38.0 °C   [disk_wd_abc123_temp]
  GPU 0 (NVIDIA RTX 3060)   51.0 °C   [gpu0_temp]

State JSON that would be published:
  {"cpu_temp": 52.4, "disk_wd_abc123_temp": 38.0, "gpu0_temp": 51.0}
```

(Use `sudo` so `smartctl` can read SMART data.) A config file is optional for
`--once`; pass one to test per-host toggles, e.g. `--once /etc/server-monitor/config.yaml`.

## Test the full pipeline (with MQTT)

```bash
pip install -r requirements.txt
LOG_LEVEL=DEBUG python server_monitor.py ./config.yaml
```

You should see `Published N readings: {...}` lines, and the sensors appear under
**Settings → Devices & Services → MQTT** in Home Assistant.

## Notes

- `smartctl` needs root to read SMART data — the systemd unit runs as root.
- All discovery/state messages are retained, so HA repopulates instantly on
  restart. Stop the service cleanly (`systemctl stop`) and it publishes
  `offline`; a crash triggers the MQTT Last-Will to do the same.
- To remove a host's entities from HA, delete the retained discovery topics
  (e.g. with `mosquitto_sub -t 'homeassistant/sensor/<node>_#'`).
