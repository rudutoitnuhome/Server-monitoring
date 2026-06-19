#!/usr/bin/env python3
"""
server_monitor.py — hardware temperature monitor for Home Assistant via MQTT.

Reads CPU, hard drive and NVIDIA GPU temperatures from the local machine and
publishes them to an MQTT broker using Home Assistant MQTT Discovery, so the
sensors appear automatically in Home Assistant grouped as one device per host.

Designed to run on Linux hosts (Proxmox, TrueNAS SCALE, Plex). Each sensor
source is optional and degrades gracefully when the underlying tool or hardware
is not present.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise


log = logging.getLogger("server_monitor")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
        "tls": False,
        "client_id": None,  # defaults to server-monitor-<node>
    },
    "node_name": None,            # defaults to the machine hostname
    "interval": 60,              # seconds between readings
    "discovery_prefix": "homeassistant",
    "base_topic": "server-monitor",
    "cpu": {"enabled": True, "per_core": False},
    "disks": {"enabled": True, "skip_standby": True},
    "gpu": {"enabled": True},
    "system": {"enabled": True},  # CPU usage, IO wait, memory, load average, uptime
    "network": {
        "enabled": True,
        # Interfaces whose name equals or starts with any of these are skipped.
        "exclude": [
            "lo", "docker", "veth", "br-", "virbr", "vnet", "tap",
            "fwbr", "fwln", "fwpr", "dummy", "kube", "cni", "flannel",
        ],
    },
    "filesystems": {"enabled": True, "mounts": ["/"]},  # used % + free GB per mount
}

CONFIG_SEARCH_PATHS = [
    "/etc/server-monitor/config.yaml",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
]


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None) -> dict:
    candidates = [path] if path else CONFIG_SEARCH_PATHS
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            log.info("Loading config from %s", candidate)
            with open(candidate) as fh:
                user_cfg = yaml.safe_load(fh) or {}
            return deep_merge(DEFAULT_CONFIG, user_cfg)
    log.warning("No config file found, using defaults (mqtt://localhost:1883)")
    return deep_merge(DEFAULT_CONFIG, {})


def slugify(value: str) -> str:
    """Make a string safe for use in MQTT topics and HA object ids."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


# --------------------------------------------------------------------------- #
# Sensor readers — each returns a list of Reading objects
# --------------------------------------------------------------------------- #

class Reading:
    """A single measurement.

    key:          stable identifier used as the JSON field + HA object id suffix
    name:         human-friendly entity name shown in Home Assistant
    value:        the measured value (float)
    unit:         unit of measurement, or None for unitless (e.g. load average)
    device_class: HA device class, or None
    state_class:  HA state class (default "measurement")
    icon:         optional mdi icon

    Defaults describe a temperature in °C, so the temperature readers don't need
    to pass anything extra.
    """

    __slots__ = ("key", "name", "value", "unit", "device_class", "state_class", "icon")

    def __init__(
        self,
        key: str,
        name: str,
        value: float,
        unit: str | None = "°C",
        device_class: str | None = "temperature",
        state_class: str | None = "measurement",
        icon: str | None = None,
    ):
        self.key = key
        self.name = name
        self.value = value
        self.unit = unit
        self.device_class = device_class
        self.state_class = state_class
        self.icon = icon


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("Command failed %s: %s", " ".join(cmd), exc)
        return None


# ---- CPU ------------------------------------------------------------------ #

def read_cpu(per_core: bool) -> list[Reading]:
    readings = _read_cpu_sensors(per_core)
    if not readings:
        readings = _read_cpu_hwmon()
    return readings


def _read_cpu_sensors(per_core: bool) -> list[Reading]:
    """Parse `sensors -j` output from lm-sensors."""
    if not shutil.which("sensors"):
        return []
    proc = _run(["sensors", "-j"])
    if not proc or proc.returncode != 0 or not proc.stdout:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # sensors occasionally emits warnings before the JSON; try to recover
        match = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    # Each physical socket appears as its own CPU chip (e.g. coretemp-isa-0000
    # and coretemp-isa-0001 on a dual-socket board, or two k10temp chips).
    # Collect one package temperature per socket, plus its cores if requested.
    sockets: list[dict] = []  # [{"temp": float, "cores": [(label, temp), ...]}]

    for chip, features in data.items():
        if not isinstance(features, dict):
            continue
        if not _is_cpu_chip(chip):
            continue
        package_temp: float | None = None
        cores: list[tuple[str, float]] = []
        for label, values in features.items():
            if not isinstance(values, dict):
                continue
            temp = _first_temp_input(values)
            if temp is None:
                continue
            lower = label.lower()
            if package_temp is None and any(
                tag in lower for tag in ("package", "tctl", "tdie")
            ):
                package_temp = temp
            elif lower.startswith("core"):
                cores.append((label, temp))
        if package_temp is not None:
            sockets.append({"temp": package_temp, "cores": cores})

    readings = _sockets_to_readings(sockets, per_core)
    if readings:
        return readings

    # No recognisable CPU package sensor — fall back to the hottest temp seen.
    hottest = _hottest_temp(data)
    if hottest is not None:
        return [Reading("cpu_temp", "CPU Temperature", hottest)]
    return []


def _is_cpu_chip(chip: str) -> bool:
    lower = chip.lower()
    return (
        lower.startswith(("coretemp", "k10temp", "zenpower"))
        or "cpu" in lower
    )


def _sockets_to_readings(sockets: list[dict], per_core: bool) -> list[Reading]:
    """Turn detected sockets into readings.

    Single socket keeps the stable ``cpu_temp`` key (backwards compatible with
    single-CPU hosts); multiple sockets are numbered ``cpu0_temp``, ``cpu1_temp``.
    """
    readings: list[Reading] = []
    if len(sockets) == 1:
        sock = sockets[0]
        readings.append(Reading("cpu_temp", "CPU Temperature", sock["temp"]))
        if per_core:
            for label, temp in sock["cores"]:
                readings.append(
                    Reading(f"cpu_{slugify(label)}_temp", f"CPU {label}", temp)
                )
    else:
        for i, sock in enumerate(sockets):
            readings.append(
                Reading(f"cpu{i}_temp", f"CPU {i} Temperature", sock["temp"])
            )
            if per_core:
                for label, temp in sock["cores"]:
                    readings.append(
                        Reading(
                            f"cpu{i}_{slugify(label)}_temp",
                            f"CPU {i} {label}",
                            temp,
                        )
                    )
    return readings


def _first_temp_input(values: dict) -> float | None:
    for sub_key, sub_val in values.items():
        if sub_key.endswith("_input") and isinstance(sub_val, (int, float)):
            return float(sub_val)
    return None


def _hottest_temp(data: dict) -> float | None:
    hottest: float | None = None
    for features in data.values():
        if not isinstance(features, dict):
            continue
        for values in features.values():
            if not isinstance(values, dict):
                continue
            temp = _first_temp_input(values)
            if temp is not None and (hottest is None or temp > hottest):
                hottest = temp
    return hottest


def _read_cpu_hwmon() -> list[Reading]:
    """Fallback: read CPU temps directly from /sys/class/hwmon.

    Each matching hwmon device is treated as one socket (so dual-socket boards
    report both). Per device we use the hottest temp as that socket's value.
    """
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return []
    socket_temps: list[float] = []
    # Sort for stable socket numbering across runs.
    for entry in sorted(os.listdir(base)):
        hwmon = os.path.join(base, entry)
        name = _read_text(os.path.join(hwmon, "name"))
        if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        device_hottest: float | None = None
        for fname in os.listdir(hwmon):
            if re.fullmatch(r"temp\d+_input", fname):
                raw = _read_text(os.path.join(hwmon, fname))
                if raw and raw.lstrip("-").isdigit():
                    temp = int(raw) / 1000.0
                    if device_hottest is None or temp > device_hottest:
                        device_hottest = temp
        if device_hottest is not None:
            socket_temps.append(device_hottest)
    if not socket_temps:
        return []
    if len(socket_temps) == 1:
        return [Reading("cpu_temp", "CPU Temperature", socket_temps[0])]
    return [
        Reading(f"cpu{i}_temp", f"CPU {i} Temperature", temp)
        for i, temp in enumerate(socket_temps)
    ]


def _read_text(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


# ---- Disks ---------------------------------------------------------------- #

def read_disks(skip_standby: bool) -> list[Reading]:
    """Read drive temperatures via smartctl for every detected device."""
    if not shutil.which("smartctl"):
        return []
    scan = _run(["smartctl", "--scan-open", "-j"])
    if not scan or not scan.stdout:
        return []
    try:
        devices = json.loads(scan.stdout).get("devices", [])
    except json.JSONDecodeError:
        return []

    readings: list[Reading] = []
    for dev in devices:
        name = dev.get("name")
        if not name:
            continue
        dev_type = dev.get("type", "auto")
        reading = _read_one_disk(name, dev_type, skip_standby)
        if reading is not None:
            readings.append(reading)
    return readings


def _read_one_disk(name: str, dev_type: str, skip_standby: bool) -> Reading | None:
    cmd = ["smartctl", "-j", "-A", "-i", "-d", dev_type]
    if skip_standby:
        # -n standby: exit without spinning the drive up if it is asleep
        cmd += ["-n", "standby"]
    cmd.append(name)
    proc = _run(cmd)
    if not proc or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    # Drive is in standby — skip silently so we don't wake it.
    if skip_standby and _is_standby(data):
        log.debug("%s is in standby, skipping", name)
        return None

    temp = data.get("temperature", {}).get("current")
    if temp is None:
        return None

    serial = data.get("serial_number") or slugify(name)
    model = data.get("model_name") or data.get("device", {}).get("name", "")
    short = os.path.basename(name)
    key = f"disk_{slugify(str(serial))}_temp"
    label = f"Disk {short}"
    if model:
        label = f"Disk {short} ({model})"
    return Reading(key, label, float(temp))


def _is_standby(data: dict) -> bool:
    messages = data.get("smartctl", {}).get("messages", [])
    for msg in messages:
        text = str(msg.get("string", "")).lower()
        if "standby" in text or "in low-power mode" in text:
            return True
    return False


# ---- GPU ------------------------------------------------------------------ #

def read_gpu() -> list[Reading]:
    """Read NVIDIA GPU temperatures via nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return []
    proc = _run([
        "nvidia-smi",
        "--query-gpu=index,name,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    if not proc or proc.returncode != 0 or not proc.stdout:
        return []

    readings: list[Reading] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        index, name, temp_str = parts[0], parts[1], parts[2]
        try:
            temp = float(temp_str)
        except ValueError:
            continue
        readings.append(
            Reading(f"gpu{index}_temp", f"GPU {index} ({name})", temp)
        )
    return readings


# ---- System (CPU usage, IO wait, memory, load) ---------------------------- #

# Previous /proc/stat snapshot, used to compute usage/iowait over the interval.
_prev_cpu_times: tuple[int, int, int] | None = None


def read_system() -> list[Reading]:
    """Read CPU usage, IO wait, memory, load average and uptime from /proc."""
    readings: list[Reading] = []
    readings += _read_proc_stat()
    readings += _read_memory()
    readings += _read_loadavg()
    readings += _read_uptime()
    return readings


def _read_proc_stat() -> list[Reading]:
    """CPU usage % and IO wait % from /proc/stat deltas between cycles."""
    global _prev_cpu_times
    if not os.path.exists("/proc/stat"):
        return []
    try:
        with open("/proc/stat") as fh:
            first = fh.readline()
    except OSError:
        return []
    parts = first.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return []
    try:
        nums = [int(x) for x in parts[1:]]
    except ValueError:
        return []

    # fields: user nice system idle iowait irq softirq steal ...
    total = sum(nums)
    idle_all = nums[3] + nums[4]   # idle + iowait
    iowait = nums[4]

    readings: list[Reading] = []
    if _prev_cpu_times is not None:
        prev_total, prev_idle, prev_iowait = _prev_cpu_times
        dt = total - prev_total
        if dt > 0:
            usage = (1 - (idle_all - prev_idle) / dt) * 100
            iowait_pct = (iowait - prev_iowait) / dt * 100
            readings.append(Reading(
                "cpu_usage", "CPU Usage", round(max(0.0, usage), 1),
                unit="%", device_class=None, icon="mdi:cpu-64-bit",
            ))
            readings.append(Reading(
                "iowait", "IO Wait", round(max(0.0, iowait_pct), 1),
                unit="%", device_class=None, icon="mdi:timer-sand",
            ))
    _prev_cpu_times = (total, idle_all, iowait)
    return readings


def _read_memory() -> list[Reading]:
    """Memory used % from /proc/meminfo."""
    if not os.path.exists("/proc/meminfo"):
        return []
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                fields = rest.split()
                if fields:
                    info[key] = int(fields[0])  # value in kB
    except (OSError, ValueError):
        return []
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if not total or available is None:
        return []
    used_pct = (1 - available / total) * 100
    used_gb = (total - available) / 1024 / 1024   # kB -> GiB
    total_gb = total / 1024 / 1024
    return [
        Reading("memory_used", "Memory Used", round(used_pct, 1),
                unit="%", device_class=None, icon="mdi:memory"),
        Reading("memory_used_gb", "Memory Used (GB)", round(used_gb, 1),
                unit="GB", device_class="data_size", icon="mdi:memory"),
        Reading("memory_total_gb", "Memory Total (GB)", round(total_gb, 1),
                unit="GB", device_class="data_size", icon="mdi:memory"),
    ]


def _read_loadavg() -> list[Reading]:
    """1/5/15-minute load averages via os.getloadavg()."""
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        return []
    return [
        Reading("load_1m", "CPU Load (1m)", round(load1, 2),
                unit=None, device_class=None, icon="mdi:gauge"),
        Reading("load_5m", "CPU Load (5m)", round(load5, 2),
                unit=None, device_class=None, icon="mdi:gauge"),
        Reading("load_15m", "CPU Load (15m)", round(load15, 2),
                unit=None, device_class=None, icon="mdi:gauge"),
    ]


def _read_uptime() -> list[Reading]:
    """System uptime in days from /proc/uptime."""
    raw = _read_text("/proc/uptime")
    if not raw:
        return []
    try:
        seconds = float(raw.split()[0])
    except (ValueError, IndexError):
        return []
    return [Reading(
        "uptime_days", "Uptime", round(seconds / 86400, 2),
        unit="d", device_class="duration", icon="mdi:clock-outline",
    )]


# ---- Network -------------------------------------------------------------- #

# Previous /proc/net/dev counters per interface: name -> (rx_bytes, tx_bytes, monotonic)
_prev_net: dict[str, tuple[int, int, float]] = {}


def read_network(cfg_net: dict) -> list[Reading]:
    """Per-interface throughput (Mbit/s) and negotiated link speed."""
    if not os.path.exists("/proc/net/dev"):
        return []
    exclude = cfg_net.get("exclude", [])
    try:
        with open("/proc/net/dev") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    now = time.monotonic()
    readings: list[Reading] = []
    for line in lines[2:]:  # first two lines are headers
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name or _iface_excluded(name, exclude):
            continue
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            rx_bytes, tx_bytes = int(fields[0]), int(fields[8])
        except ValueError:
            continue

        slug = slugify(name)
        speed = _iface_speed(name)
        if speed is not None:
            readings.append(Reading(
                f"net_{slug}_speed", f"{name} Link Speed", float(speed),
                unit="Mbit/s", device_class="data_rate", icon="mdi:ethernet",
            ))

        prev = _prev_net.get(name)
        _prev_net[name] = (rx_bytes, tx_bytes, now)
        if prev is not None:
            prx, ptx, pt = prev
            dt = now - pt
            # Guard against counter resets (iface down, reboot).
            if dt > 0 and rx_bytes >= prx and tx_bytes >= ptx:
                rx_mbps = (rx_bytes - prx) * 8 / 1e6 / dt
                tx_mbps = (tx_bytes - ptx) * 8 / 1e6 / dt
                readings.append(Reading(
                    f"net_{slug}_rx", f"{name} In", round(rx_mbps, 2),
                    unit="Mbit/s", device_class="data_rate",
                    icon="mdi:download-network",
                ))
                readings.append(Reading(
                    f"net_{slug}_tx", f"{name} Out", round(tx_mbps, 2),
                    unit="Mbit/s", device_class="data_rate",
                    icon="mdi:upload-network",
                ))
    return readings


def _iface_excluded(name: str, patterns: list[str]) -> bool:
    return any(name == p or name.startswith(p) for p in patterns)


def _iface_speed(name: str) -> int | None:
    """Negotiated link speed in Mbit/s, or None for virtual/down interfaces."""
    raw = _read_text(f"/sys/class/net/{name}/speed")
    if raw and raw.lstrip("-").isdigit():
        value = int(raw)
        return value if value > 0 else None
    return None


# ---- Filesystems ---------------------------------------------------------- #

def read_filesystems(mounts: list[str]) -> list[Reading]:
    """Used % and free space (GB) for each configured mountpoint."""
    readings: list[Reading] = []
    for mount in mounts:
        try:
            st = os.statvfs(mount)
        except OSError:
            log.debug("statvfs failed for %s", mount)
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            continue
        used_pct = (total - free) / total * 100
        slug = slugify(mount) or "root"
        readings.append(Reading(
            f"fs_{slug}_used", f"Disk {mount} Used", round(used_pct, 1),
            unit="%", device_class=None, icon="mdi:harddisk",
        ))
        readings.append(Reading(
            f"fs_{slug}_free_gb", f"Disk {mount} Free", round(free / 1024**3, 1),
            unit="GB", device_class="data_size", icon="mdi:harddisk",
        ))
    return readings


# --------------------------------------------------------------------------- #
# MQTT publisher with Home Assistant discovery
# --------------------------------------------------------------------------- #

class Publisher:
    def __init__(self, cfg: dict, node: str):
        self.cfg = cfg
        self.node = node
        self.node_slug = slugify(node)
        self.base = f"{cfg['base_topic']}/{self.node_slug}"
        self.state_topic = f"{self.base}/state"
        self.avail_topic = f"{self.base}/availability"
        self.discovery_prefix = cfg["discovery_prefix"]
        self._announced: set[str] = set()

        mqtt_cfg = cfg["mqtt"]
        client_id = mqtt_cfg.get("client_id") or f"server-monitor-{self.node_slug}"
        self.client = mqtt.Client(client_id=client_id)
        if mqtt_cfg.get("username"):
            self.client.username_pw_set(
                mqtt_cfg["username"], mqtt_cfg.get("password")
            )
        if mqtt_cfg.get("tls"):
            self.client.tls_set()
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("Connected to MQTT broker")
            client.publish(self.avail_topic, "online", qos=1, retain=True)
            # Force re-announce of discovery configs after a reconnect.
            self._announced.clear()
        else:
            log.error("MQTT connection failed with code %s", rc)

    def connect(self):
        mqtt_cfg = self.cfg["mqtt"]
        self.client.connect(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=60)
        self.client.loop_start()

    def disconnect(self):
        try:
            self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass

    @property
    def device_block(self) -> dict:
        return {
            "identifiers": [f"server_monitor_{self.node_slug}"],
            "name": self.node,
            "model": "Server Monitor",
            "manufacturer": "server_monitor.py",
        }

    def announce(self, reading: Reading):
        """Publish a Home Assistant discovery config for one sensor (once)."""
        if reading.key in self._announced:
            return
        object_id = f"{self.node_slug}_{reading.key}"
        topic = f"{self.discovery_prefix}/sensor/{object_id}/config"
        payload = {
            "name": reading.name,
            # object_id pins the entity_id to sensor.<node>_<key> (deterministic
            # and collision-free across hosts); has_entity_name groups the
            # friendly name under the device.
            "object_id": object_id,
            "has_entity_name": True,
            "unique_id": f"server_monitor_{object_id}",
            "state_topic": self.state_topic,
            "availability_topic": self.avail_topic,
            "value_template": f"{{{{ value_json.{reading.key} }}}}",
            "device": self.device_block,
        }
        if reading.unit is not None:
            payload["unit_of_measurement"] = reading.unit
        if reading.device_class is not None:
            payload["device_class"] = reading.device_class
        if reading.state_class is not None:
            payload["state_class"] = reading.state_class
        if reading.icon is not None:
            payload["icon"] = reading.icon
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self._announced.add(reading.key)
        log.debug("Announced %s", object_id)

    def publish(self, readings: list[Reading]):
        for reading in readings:
            self.announce(reading)
        state = {r.key: round(r.value, 1) for r in readings}
        self.client.publish(self.state_topic, json.dumps(state), qos=0, retain=True)
        log.info("Published %d readings: %s", len(readings), state)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def collect(cfg: dict) -> list[Reading]:
    readings: list[Reading] = []
    if cfg["cpu"]["enabled"]:
        readings += read_cpu(cfg["cpu"].get("per_core", False))
    if cfg["disks"]["enabled"]:
        readings += read_disks(cfg["disks"].get("skip_standby", True))
    if cfg["gpu"]["enabled"]:
        readings += read_gpu()
    if cfg["system"]["enabled"]:
        readings += read_system()
    if cfg["network"]["enabled"]:
        readings += read_network(cfg["network"])
    if cfg["filesystems"]["enabled"]:
        readings += read_filesystems(cfg["filesystems"].get("mounts", ["/"]))
    return readings


_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("Received signal %s, shutting down", signum)
    _running = False


def _print_readings(node: str, readings: list[Reading]):
    """Pretty-print one collection cycle for --once dry runs."""
    print(f"\nNode: {node}")
    if not readings:
        print("  (no readings — are lm-sensors / smartmontools / nvidia-smi installed?)")
        return
    width = max(len(r.name) for r in readings)
    for r in readings:
        unit = f" {r.unit}" if r.unit else ""
        print(f"  {r.name:<{width}}  {r.value:>8.1f}{unit}   [{r.key}]")
    state = {r.key: round(r.value, 1) for r in readings}
    print(f"\nState JSON that would be published:\n  {json.dumps(state)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hardware temperature monitor -> MQTT -> Home Assistant",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to config.yaml (defaults to /etc/server-monitor/config.yaml "
        "or ./config.yaml). Not required with --once.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect sensors once, print the readings, and exit. "
        "Does not connect to MQTT — handy for testing a new host.",
    )
    return parser.parse_args(argv)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    cfg = load_config(args.config)

    node = cfg.get("node_name") or socket.gethostname()

    if args.once:
        log.info("Dry run for node '%s' (no MQTT)", node)
        # Rate metrics (CPU usage, IO wait, network throughput) need two samples
        # to compute a delta, so prime the baselines, wait briefly, then print.
        collect(cfg)
        time.sleep(1.0)
        _print_readings(node, collect(cfg))
        return

    log.info("Monitoring node '%s'", node)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    publisher = Publisher(cfg, node)
    try:
        publisher.connect()
    except Exception as exc:
        log.error("Could not connect to MQTT broker: %s", exc)
        sys.exit(1)

    interval = int(cfg["interval"])
    while _running:
        start = time.monotonic()
        try:
            readings = collect(cfg)
            if readings:
                publisher.publish(readings)
            else:
                log.warning("No readings collected this cycle")
        except Exception as exc:  # keep the service alive on transient errors
            log.exception("Error during collection: %s", exc)

        elapsed = time.monotonic() - start
        sleep_for = max(1.0, interval - elapsed)
        # Sleep in short slices so signals are handled promptly.
        while _running and sleep_for > 0:
            time.sleep(min(1.0, sleep_for))
            sleep_for -= 1.0

    publisher.disconnect()
    log.info("Stopped")


if __name__ == "__main__":
    main()
