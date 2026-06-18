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
    """A single temperature measurement.

    key:   stable identifier used as the JSON field + HA object id suffix
    name:  human-friendly entity name shown in Home Assistant
    value: temperature in degrees Celsius (float)
    """

    __slots__ = ("key", "name", "value")

    def __init__(self, key: str, name: str, value: float):
        self.key = key
        self.name = name
        self.value = value


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

    readings: list[Reading] = []
    package_found = False

    for chip, features in data.items():
        if not isinstance(features, dict):
            continue
        for label, values in features.items():
            if not isinstance(values, dict):
                continue
            temp = _first_temp_input(values)
            if temp is None:
                continue
            lower = label.lower()
            is_package = any(
                tag in lower for tag in ("package", "tctl", "tdie", "composite")
            )
            is_core = lower.startswith("core")
            if is_package and not package_found:
                readings.insert(0, Reading("cpu_temp", "CPU Temperature", temp))
                package_found = True
            elif per_core and is_core:
                idx = slugify(label)
                readings.append(
                    Reading(f"cpu_{idx}_temp", f"CPU {label}", temp)
                )

    # No explicit package sensor — fall back to the hottest core/temp seen.
    if not package_found:
        hottest = _hottest_temp(data)
        if hottest is not None:
            readings.insert(0, Reading("cpu_temp", "CPU Temperature", hottest))

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
    """Fallback: read CPU temp directly from /sys/class/hwmon."""
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return []
    hottest: float | None = None
    for entry in os.listdir(base):
        hwmon = os.path.join(base, entry)
        name = _read_text(os.path.join(hwmon, "name"))
        if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        for fname in os.listdir(hwmon):
            if re.fullmatch(r"temp\d+_input", fname):
                raw = _read_text(os.path.join(hwmon, fname))
                if raw and raw.isdigit():
                    temp = int(raw) / 1000.0
                    if hottest is None or temp > hottest:
                        hottest = temp
    if hottest is None:
        return []
    return [Reading("cpu_temp", "CPU Temperature", hottest)]


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
            "unique_id": f"server_monitor_{object_id}",
            "state_topic": self.state_topic,
            "availability_topic": self.avail_topic,
            "value_template": f"{{{{ value_json.{reading.key} }}}}",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "device": self.device_block,
        }
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
        print(f"  {r.name:<{width}}  {r.value:5.1f} °C   [{r.key}]")
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
