# Server Monitor → MQTT → Home Assistant

A small Python agent that reads **CPU / hard-drive / NVIDIA GPU temperatures**
plus **system load metrics** from a Linux host and publishes them to MQTT using
**Home Assistant MQTT Discovery**. Run it on each machine (Proxmox,
TrueNAS SCALE, Plex); each host shows up as its own *device* in Home Assistant.

## How it works

| Metric | Source | Notes |
|--------|--------|-------|
| CPU temp | `sensors -j` (lm-sensors) | Falls back to `/sys/class/hwmon` if lm-sensors isn't installed. One entity per socket; optional per-core entities. |
| Disk temps | `smartctl` (smartmontools) | One entity per drive (SATA, SAS, NVMe). Skips drives in **standby** so it won't wake sleeping disks. |
| GPU temps | `nvidia-smi` | One entity per NVIDIA GPU. Silently skipped when no GPU/driver is present. |
| CPU usage % | `/proc/stat` | Utilisation averaged over the polling interval. |
| IO wait % | `/proc/stat` | Share of CPU time spent waiting on I/O. |
| Memory used | `/proc/meminfo` | Used %, plus used/total in GB. |
| Load average | `os.getloadavg()` | 1m / 5m / 15m (unitless). |
| Uptime | `/proc/uptime` | Days since boot. |
| Network throughput | `/proc/net/dev` | Per-interface in/out in Mbit/s (rate over the interval). |
| Link speed | `/sys/class/net/*/speed` | Negotiated interface speed (Mbit/s). |
| Filesystem usage | `os.statvfs` | Used % + free GB per configured mountpoint. |

System/network/filesystem metrics come from the kernel's `/proc` and `/sys`
(no extra dependencies), toggled by the `system`, `network` and `filesystems`
config blocks. Network excludes virtual interfaces (loopback, docker, veth,
bridges, etc.) by default; filesystems default to monitoring `/`.

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
sudo nano /etc/server-monitor/config.yaml   # <-- EDIT THE LIVE FILE (see below)
sudo systemctl restart server-monitor
journalctl -u server-monitor -f
```

> **⚠️ Edit `/etc/server-monitor/config.yaml`, not the repo's `config.yaml`.**
> `install.sh` copies the template to `/etc/server-monitor/config.yaml`, and the
> systemd service reads **only** that file. Editing the cloned repo's
> `config.yaml` has no effect on the service. If you prefer to keep your edits in
> the repo, copy them over the live file and restart:
>
> ```bash
> sudo cp config.yaml /etc/server-monitor/config.yaml
> sudo chmod 600 /etc/server-monitor/config.yaml   # keep the MQTT password root-only
> sudo systemctl restart server-monitor
> ```
>
> Symptom of editing the wrong file: the log shows `Monitoring node 'proxmox'`
> (the template default) and `Could not connect to MQTT broker: timed out`.

Repeat on each machine, giving each a distinct `node_name`
(e.g. `proxmox`, `plex`, `arrthings`).

## TrueNAS SCALE (Docker / Custom App)

TrueNAS SCALE can't run the systemd installer, so use a container. The image
bundles `lm-sensors` and `smartmontools`, so nothing is installed on the host.

There are two ways to get the image. **Common to both:**

- **Config on a dataset**: create a dedicated **folder** e.g.
  `/mnt/<POOL>/apps/server-monitor/` and put your edited `config.yaml` there
  (copy from `config.example.yaml`, set `node_name: truenas`). The compose
  mounts this **folder** to `/etc/server-monitor` (not the file directly) —
  single-file bind mounts are unreliable on TrueNAS and can show up as an empty
  directory inside the container, which makes the agent fall back to
  `mqtt://localhost`. Keep only `config.yaml` in that folder.
- The compose runs the container `privileged` with host `/dev` and `/sys`
  mounted so `smartctl` can read disk SMART data. CPU temps work too **if** the
  host has the relevant `hwmon`/`coretemp` modules loaded; disk temps work
  regardless. No GPU temps (no NVIDIA runtime) — fine, the NAS has no GPU.

### Option A — build the image on TrueNAS (no registry needed) — default

`docker-compose.truenas.yml` is already set up for this (`image: server-monitor:local`,
`pull_policy: never`). From a root shell on TrueNAS:

```bash
cd /mnt/<POOL>/apps
git clone https://github.com/rudutoitnuhome/Server-monitoring.git
cd Server-monitoring
docker build -t server-monitor:local .
```

Rebuild and restart the app to pick up new versions after a `git pull`.

### Option B — pull from GHCR (needs GitHub Actions working)

The workflow publishes `ghcr.io/rudutoitnuhome/server-monitor:latest` on every
push. After the first successful run, set the package visibility to **Public**
(`github.com/users/rudutoitnuhome/packages/container/server-monitor` → *Package
settings*) so TrueNAS can pull without credentials. Then, in the compose, set
`image: ghcr.io/rudutoitnuhome/server-monitor:latest` and remove `pull_policy`.

### Install the Custom App

TrueNAS UI → **Apps → Discover Apps → Custom App → Install via YAML**, paste
[`docker-compose.truenas.yml`](docker-compose.truenas.yml), and replace `<POOL>`
with your pool name.

Check logs from the app's shell or:

```bash
docker logs -f server-monitor
```

## Fan control (Dell iDRAC, IPMI)

`fan_controller.py` is an optional companion that drives a server's chassis fans
from the temperatures published over MQTT. It runs on the Proxmox host whose
fans you want to control and:

- subscribes to the `server-monitor` temps that matter to that chassis —
  including **GPU temps published by a VM running on the host** (you name the
  VM's monitor node in the config, so e.g. a GPU VM's temp drives the host fans);
- applies two configurable curves — **CPU/GPU** and **HDD** (max wins);
- sets the fans via Dell iDRAC raw IPMI commands (takes manual control and
  disables the third-party-PCIe 100% response, like the old startup script);
- publishes the BMC's **ambient/inlet/exhaust** temps + the computed target %
  to Home Assistant, and exposes a **Fan override %** number;
- **fails safe**: restores the BMC's automatic fan control on exit, on stale
  temperature data, or after repeated IPMI errors.

Default curves (from the config):

| Temp | CPU/GPU | HDD |
|------|--------:|----:|
| < 45 °C | 10% | 20% |
| 45–55 | 20% | 30% |
| 55–65 | 45% | 45% |
| 65–70 | 70% | 70% |
| > 70 | 100% | 100% |

Setup on the host:

```bash
sudo ./install.sh                 # also copies fan_controller.py into /opt
sudo mkdir -p /etc/fan-controller
sudo cp config.fan.example.yaml /etc/fan-controller/config.yaml
sudo nano /etc/fan-controller/config.yaml   # MQTT, iDRAC creds, sources, curves
# Test WITHOUT touching the fans first:
sudo /opt/server-monitor/venv/bin/python /opt/server-monitor/fan_controller.py --once
sudo /opt/server-monitor/venv/bin/python /opt/server-monitor/fan_controller.py --dry-run
# Then enable the service:
sudo cp systemd/fan-controller.service /etc/systemd/system/
sudo systemctl enable --now fan-controller
journalctl -u fan-controller -f
```

> `--dry-run` computes and publishes everything but never sends a fan command —
> use it to confirm the curve picks sane values before handing over real control.
> Needs `ipmitool` on the host (`apt install ipmitool`).

> **Huawei** uses a different OEM command set (iBMC); the temperature readout
> works, but its manual fan-control commands are stubbed pending the next phase.

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
