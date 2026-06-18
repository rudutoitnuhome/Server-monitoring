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
# Proxmox (Debian-based)
apt install lm-sensors smartmontools
sensors-detect --auto        # one-time, for CPU sensors
# NVIDIA driver provides nvidia-smi automatically
```

> **TrueNAS SCALE is different** — it's a locked-down appliance: `apt` is
> disabled, the root filesystem is immutable, and custom `systemd` units don't
> survive updates. Do **not** use `install.sh` there. Run it as a container
> instead — see [TrueNAS SCALE](#truenas-scale-docker--custom-app) below.

## Install (Proxmox / Plex / any normal Linux host)

```bash
git clone git@github.com:rudutoitnuhome/Server-monitoring.git
cd Server-monitoring
sudo ./install.sh
sudo nano /etc/server-monitor/config.yaml   # set MQTT broker + node_name
sudo systemctl start server-monitor
journalctl -u server-monitor -f
```

Repeat on each machine, giving each a distinct `node_name`
(e.g. `proxmox`, `plex`).

## TrueNAS SCALE (Docker / Custom App)

TrueNAS SCALE can't run the systemd installer, so use a container. The image
bundles `lm-sensors` and `smartmontools`, so nothing is installed on the host.

There are two ways to get the image. **Common to both:**

- **Config on a dataset**: create e.g. `/mnt/<POOL>/apps/server-monitor/` and
  put your edited `config.yaml` there (copy from `config.example.yaml`, set
  `node_name: truenas`). `chmod 600` it — it holds the MQTT password.
- The compose runs the container `privileged` with host `/dev` and `/sys`
  mounted so `smartctl` can read disk SMART data. CPU temps work too **if** the
  host has the relevant `hwmon`/`coretemp` modules loaded; disk temps work
  regardless. No GPU temps (no NVIDIA runtime) — fine, the NAS has no GPU.

### Option A — build the image on TrueNAS (no registry needed)

Works with no external dependencies. From a root shell on TrueNAS:

```bash
cd /mnt/<POOL>/apps
git clone https://github.com/rudutoitnuhome/Server-monitoring.git
cd Server-monitoring
docker build -t server-monitor:local .
```

Then install the Custom App with
[`docker-compose.truenas.yml`](docker-compose.truenas.yml), but change the image
line to the locally-built one so Docker doesn't try to pull it:

```yaml
    image: server-monitor:local
    pull_policy: never
```

### Option B — pull from GHCR (needs GitHub Actions working)

The workflow publishes `ghcr.io/rudutoitnuhome/server-monitor:latest` on every
push. After the first successful run, set the package visibility to **Public**
(`github.com/users/rudutoitnuhome/packages/container/server-monitor` → *Package
settings*) so TrueNAS can pull without credentials, then use the compose as-is.

### Install the Custom App

TrueNAS UI → **Apps → Discover Apps → Custom App → Install via YAML**, paste
[`docker-compose.truenas.yml`](docker-compose.truenas.yml), and replace `<POOL>`
with your pool name (plus the image tweak above if you built locally).

Check logs from the app's shell or:

```bash
docker logs -f server-monitor
```

## Configuration

> **⚠️ The systemd service only reads `/etc/server-monitor/config.yaml`.**
> Editing the `config.yaml` inside the cloned repo has **no effect** on the
> running service — that copy is only used when you run the script by hand from
> the repo directory. Always edit (or copy your changes to) the `/etc` file:
>
> ```bash
> sudo nano /etc/server-monitor/config.yaml          # edit the live config directly
> # …or, if you prefer to keep edits in the repo:
> sudo cp config.yaml /etc/server-monitor/config.yaml
> sudo chmod 600 /etc/server-monitor/config.yaml     # keep credentials root-only
> sudo systemctl restart server-monitor              # apply changes
> ```

See [`config.example.yaml`](config.example.yaml). Key fields:

- `mqtt.host` / `port` / `username` / `password` / `tls`
- `node_name` — the device name in HA (defaults to hostname)
- `interval` — seconds between readings (default 60)
- `cpu.per_core`, `disks.skip_standby`, and per-source `enabled` toggles

Changes to the config require a `sudo systemctl restart server-monitor` to take
effect.

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
