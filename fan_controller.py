#!/usr/bin/env python3
"""
fan_controller.py — temperature-driven IPMI fan control for Dell (and later
Huawei) servers, fed by the MQTT temperature data published by server_monitor.py.

Runs on a Proxmox host. It:
  * subscribes to the server-monitor MQTT state topics for the temperatures that
    matter to this chassis (its own CPU/disk temps, plus GPU temps published by
    a VM running on it),
  * applies two fan curves (one for CPU/GPU, one for HDD), max-wins,
  * sets the chassis fans over IPMI (Dell iDRAC raw commands),
  * publishes ambient/inlet/exhaust temps + the computed target % to Home
    Assistant via MQTT discovery, and exposes a "fan override %" number,
  * fails safe: restores the BMC's automatic fan control on exit, on stale
    temperature data, or on repeated IPMI errors.

Curves and temperature sources are defined in the config file; nothing is
hardcoded per host. See config.fan.example.yaml.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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

log = logging.getLogger("fan_controller")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "mqtt": {"host": "localhost", "port": 1883, "username": None, "password": None,
             "tls": False, "client_id": None},
    "node_name": None,            # HA device name for this chassis (defaults to hostname)
    "base_topic": "server-monitor",   # where server_monitor publishes temps
    "fan_topic": "server-fan",        # where this controller publishes its own state
    "discovery_prefix": "homeassistant",
    "interval": 30,               # control loop seconds
    "ipmi": {
        "vendor": "dell",         # dell | huawei
        "host": None,             # BMC/iDRAC IP; null = local (-I open)
        "username": None,
        "password": None,
        "interface": "lanplus",
    },
    # Huawei iBMC uses Redfish (HTTPS on the BMC) for fan control, not raw IPMI.
    # The actions below are config-driven so they can match your firmware — run
    # `fan_controller.py --probe` and confirm the schema. {chassis} is filled
    # from chassis_id; a value of exactly "{percent}" becomes the integer %.
    "redfish": {
        "host": None,
        "username": None,
        "password": None,
        "verify_tls": False,      # BMCs ship self-signed certs
        "chassis_id": "1",
        "thermal_path": "/redfish/v1/Chassis/{chassis}/Thermal",
        "manual_mode": {
            "method": "PATCH",
            "path": "/redfish/v1/Chassis/{chassis}/Thermal",
            "payload": {"Oem": {"Huawei": {"FanSpeedAdjustmentMode": "Manual"}}},
        },
        "set_speed": {
            "method": "PATCH",
            "path": "/redfish/v1/Chassis/{chassis}/Thermal",
            "payload": {"Oem": {"Huawei": {"FanSpeedAdjustmentMode": "Manual",
                                           "FanSpeedLevelPercents": "{percent}"}}},
        },
        "auto_mode": {
            "method": "PATCH",
            "path": "/redfish/v1/Chassis/{chassis}/Thermal",
            "payload": {"Oem": {"Huawei": {"FanSpeedAdjustmentMode": "Automatic"}}},
        },
    },
    # Temperature inputs. Each source names a server-monitor node and either an
    # explicit list of state-JSON keys or a regex over keys. (Keys are the values
    # in the [brackets] of `server_monitor.py --once`, e.g. cpu0_temp, gpu0_temp,
    # disk_<id>_temp.)
    "sources": {
        "cpu_gpu": [],            # list of {node, keys|key_regex}
        "hdd": [],
    },
    # Fan curves: ascending bands. For a temperature t, the first band with
    # t < below applies. Last band should be a high `below` to act as the cap.
    "curves": {
        "cpu_gpu": [
            {"below": 45, "percent": 10},
            {"below": 55, "percent": 20},
            {"below": 65, "percent": 45},
            {"below": 70, "percent": 70},
            {"below": 999, "percent": 100},
        ],
        "hdd": [
            {"below": 45, "percent": 20},
            {"below": 55, "percent": 30},
            {"below": 65, "percent": 45},
            {"below": 70, "percent": 70},
            {"below": 999, "percent": 100},
        ],
    },
    "ambient": {"enabled": True},  # publish ipmitool sdr temperatures to HA
    "safety": {
        "stale_seconds": 180,      # temps older than this are ignored
        "min_percent": 20,         # never command below this in manual mode
        "ipmi_failures_before_auto": 3,  # consecutive IPMI errors -> restore auto
    },
}

CONFIG_SEARCH_PATHS = [
    "/etc/fan-controller/config.yaml",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.fan.yaml"),
]


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None) -> dict:
    for candidate in ([path] if path else CONFIG_SEARCH_PATHS):
        if candidate and os.path.isfile(candidate):
            log.info("Loading config from %s", candidate)
            with open(candidate) as fh:
                return deep_merge(DEFAULT_CONFIG, yaml.safe_load(fh) or {})
    log.warning("No config file found, using defaults")
    return deep_merge(DEFAULT_CONFIG, {})


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())
    return re.sub(r"_+", "_", value).strip("_")


# --------------------------------------------------------------------------- #
# IPMI back-ends
# --------------------------------------------------------------------------- #

class IpmiError(Exception):
    pass


class DellIpmi:
    """Dell iDRAC (7/8, R720-era) raw-command fan control."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _base(self) -> list[str]:
        cmd = ["ipmitool"]
        host = self.cfg.get("host")
        if host:
            cmd += ["-I", self.cfg.get("interface", "lanplus"), "-H", host,
                    "-U", self.cfg.get("username") or "",
                    "-P", self.cfg.get("password") or ""]
        else:
            cmd += ["-I", "open"]   # local BMC, no auth
        return cmd

    def _run(self, args: list[str], timeout: int = 20) -> str:
        proc = subprocess.run(self._base() + args, capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            raise IpmiError(f"ipmitool {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    # ---- fan control ----
    def begin_manual(self):
        """Take manual control and stop the third-party-PCIe 100% response."""
        self._run(["raw", "0x30", "0x30", "0x01", "0x00"])              # manual mode
        self._run(["raw", "0x30", "0xce", "0x00", "0x16", "0x05", "0x00",
                   "0x00", "0x00", "0x05", "0x00", "0x01", "0x00", "0x00"])  # PCIe resp off

    def set_percent(self, percent: int):
        percent = max(0, min(100, int(percent)))
        self._run(["raw", "0x30", "0x30", "0x02", "0xff", f"0x{percent:02x}"])

    def restore_auto(self):
        self._run(["raw", "0x30", "0x30", "0x01", "0x01"])             # automatic mode

    # ---- temperature readout ----
    def read_temperatures(self) -> list[tuple[str, float]]:
        """Parse `sdr type temperature` into (name, °C) pairs."""
        out = self._run(["sdr", "type", "temperature"])
        results: list[tuple[str, float]] = []
        seen: dict[str, int] = {}
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            name, sensor_id, status, reading = parts[0], parts[1], parts[2], parts[4]
            if status.lower() != "ok":
                continue
            m = re.search(r"(-?\d+(?:\.\d+)?)", reading)
            if not m or "degrees" not in reading.lower():
                continue
            # Disambiguate duplicate names (e.g. two "Temp" CPU sensors).
            if name in seen or name == "Temp":
                name = f"{name} {sensor_id}".strip()
            seen[name] = seen.get(name, 0) + 1
            results.append((name, float(m.group(1))))
        return results

    def probe(self) -> str:
        return self._run(["sdr", "type", "temperature"]) + "\n" + \
               self._run(["sdr", "type", "fan"])


def _subst(obj, percent: int):
    """Replace {percent} placeholders in a payload; exact "{percent}" -> int."""
    if isinstance(obj, str):
        if obj == "{percent}":
            return percent
        return obj.replace("{percent}", str(percent))
    if isinstance(obj, dict):
        return {k: _subst(v, percent) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, percent) for v in obj]
    return obj


class HuaweiRedfish:
    """Huawei iBMC fan control via the Redfish API (config-driven actions)."""

    def __init__(self, cfg: dict):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise IpmiError("Huawei (Redfish) needs the 'requests' package")
        import requests
        import urllib3
        urllib3.disable_warnings()  # BMCs use self-signed certs

        self.cfg = cfg
        host = cfg.get("host")
        if not host:
            raise IpmiError("redfish.host is required for the huawei vendor")
        self.base = host if host.startswith("http") else f"https://{host}"
        self.chassis = str(cfg.get("chassis_id", "1"))
        self.session = requests.Session()
        self.session.verify = bool(cfg.get("verify_tls", False))
        self.session.auth = (cfg.get("username") or "", cfg.get("password") or "")
        self.session.headers.update({"Content-Type": "application/json"})

    def _path(self, p: str) -> str:
        return self.base + p.replace("{chassis}", self.chassis)

    def _get(self, path: str) -> dict:
        r = self.session.get(self._path(path), timeout=20)
        r.raise_for_status()
        return r.json()

    def _action(self, action: dict, percent: int | None = None):
        method = (action.get("method") or "PATCH").upper()
        path = self._path(action["path"])
        payload = action.get("payload")
        if percent is not None:
            payload = _subst(payload, percent)
        r = self.session.request(method, path, json=payload, timeout=20)
        if r.status_code >= 400:
            raise IpmiError(f"redfish {method} {path} -> {r.status_code}: {r.text[:300]}")

    # ---- interface parity with DellIpmi ----
    def begin_manual(self):
        self._action(self.cfg["manual_mode"])

    def set_percent(self, percent: int):
        percent = max(0, min(100, int(percent)))
        self._action(self.cfg["set_speed"], percent=percent)

    def restore_auto(self):
        self._action(self.cfg["auto_mode"])

    def read_temperatures(self) -> list[tuple[str, float]]:
        data = self._get(self.cfg["thermal_path"])
        results: list[tuple[str, float]] = []
        for t in data.get("Temperatures", []):
            name, val = t.get("Name"), t.get("ReadingCelsius")
            if name and isinstance(val, (int, float)):
                results.append((str(name), float(val)))
        return results

    def probe(self) -> str:
        chassis = self._get(f"/redfish/v1/Chassis/{self.chassis}")
        thermal = self._get(self.cfg["thermal_path"])
        return ("=== Chassis ===\n" + json.dumps(chassis, indent=2) +
                "\n\n=== Thermal ===\n" + json.dumps(thermal, indent=2))


def make_ipmi(cfg: dict):
    """Build the BMC backend from the full config (vendor under ipmi.vendor)."""
    vendor = (cfg.get("ipmi", {}).get("vendor") or "dell").lower()
    if vendor == "dell":
        return DellIpmi(cfg["ipmi"])
    if vendor == "huawei":
        return HuaweiRedfish(cfg["redfish"])
    raise IpmiError(f"unknown vendor '{vendor}'")


# --------------------------------------------------------------------------- #
# Temperature cache (MQTT subscriber)
# --------------------------------------------------------------------------- #

class TempCache:
    """Caches the latest server-monitor state JSON per topic with a timestamp."""

    def __init__(self):
        self._data: dict[str, tuple[dict, float]] = {}

    def update(self, topic: str, payload: str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self._data[topic] = (data, time.monotonic())

    def values(self, topic: str, keys: list[str] | None,
               key_regex: str | None, stale_seconds: float) -> list[float]:
        entry = self._data.get(topic)
        if not entry:
            return []
        data, ts = entry
        if time.monotonic() - ts > stale_seconds:
            return []
        out: list[float] = []
        if keys:
            for k in keys:
                v = data.get(k)
                if isinstance(v, (int, float)):
                    out.append(float(v))
        if key_regex:
            pat = re.compile(key_regex)
            for k, v in data.items():
                if pat.search(k) and isinstance(v, (int, float)):
                    out.append(float(v))
        return out


def curve_percent(temp: float, curve: list[dict]) -> int:
    for band in curve:
        if temp < band["below"]:
            return int(band["percent"])
    return int(curve[-1]["percent"]) if curve else 100


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

class FanController:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.node = cfg.get("node_name") or socket.gethostname()
        self.node_slug = slugify(self.node)
        self.base_topic = cfg["base_topic"]
        self.fan_topic = f"{cfg['fan_topic']}/{self.node_slug}"
        self.discovery_prefix = cfg["discovery_prefix"]
        self.stale = float(cfg["safety"]["stale_seconds"])
        self.min_percent = int(cfg["safety"]["min_percent"])
        self.fail_threshold = int(cfg["safety"]["ipmi_failures_before_auto"])

        self.ipmi = make_ipmi(cfg)
        self.cache = TempCache()
        self.override: int = 0           # 0 = auto/curve; 1-100 = forced
        self._announced: set[str] = set()
        self._ipmi_failures = 0
        self._manual_active = False
        self._last_percent: int | None = None

        # Resolve the set of topics we must subscribe to. Sources without a
        # node (e.g. a removed GPU source on a host with no GPU) are skipped.
        self._topics: set[str] = set()
        for group in ("cpu_gpu", "hdd"):
            for src in cfg["sources"].get(group, []):
                node = src.get("node")
                if not node:
                    log.warning("Ignoring %s source with no 'node': %s", group, src)
                    continue
                self._topics.add(self._topic_for(node))

        client_id = cfg["mqtt"].get("client_id") or f"fan-controller-{self.node_slug}"
        self.client = mqtt.Client(client_id=client_id)
        if cfg["mqtt"].get("username"):
            self.client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))
        if cfg["mqtt"].get("tls"):
            self.client.tls_set()
        self.avail_topic = f"{self.fan_topic}/availability"
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _topic_for(self, node: str) -> str:
        return f"{self.base_topic}/{slugify(node)}/state"

    @property
    def override_cmd_topic(self) -> str:
        return f"{self.fan_topic}/override/set"

    # ---- MQTT ----
    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed rc=%s", rc)
            return
        log.info("Connected to MQTT broker")
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        for t in self._topics:
            client.subscribe(t)
            log.info("Subscribed to %s", t)
        client.subscribe(self.override_cmd_topic)
        self._announced.clear()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "ignore")
        if topic == self.override_cmd_topic:
            try:
                self.override = max(0, min(100, int(float(payload))))
                log.info("Override set to %s%%", self.override)
                client.publish(f"{self.fan_topic}/override/state", self.override,
                               qos=1, retain=True)
            except ValueError:
                log.warning("Bad override payload: %r", payload)
        elif topic in self._topics:
            self.cache.update(topic, payload)

    def connect(self):
        self.client.connect(self.cfg["mqtt"]["host"], int(self.cfg["mqtt"]["port"]), keepalive=60)
        self.client.loop_start()

    # ---- discovery ----
    @property
    def device_block(self) -> dict:
        return {"identifiers": [f"fan_controller_{self.node_slug}"],
                "name": f"{self.node} Fans", "model": "Fan Controller",
                "manufacturer": "fan_controller.py"}

    def _announce_sensor(self, key: str, name: str, unit: str | None,
                         device_class: str | None, icon: str | None):
        if key in self._announced:
            return
        object_id = f"{self.node_slug}_fan_{key}"
        topic = f"{self.discovery_prefix}/sensor/{object_id}/config"
        payload = {
            "name": name, "object_id": object_id, "has_entity_name": True,
            "unique_id": f"fan_controller_{object_id}",
            "state_topic": f"{self.fan_topic}/state",
            "availability_topic": self.avail_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "state_class": "measurement", "device": self.device_block,
        }
        if unit: payload["unit_of_measurement"] = unit
        if device_class: payload["device_class"] = device_class
        if icon: payload["icon"] = icon
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self._announced.add(key)

    def _announce_override(self):
        if "__override" in self._announced:
            return
        object_id = f"{self.node_slug}_fan_override"
        topic = f"{self.discovery_prefix}/number/{object_id}/config"
        payload = {
            "name": "Fan override %", "object_id": object_id, "has_entity_name": True,
            "unique_id": f"fan_controller_{object_id}",
            "command_topic": self.override_cmd_topic,
            "state_topic": f"{self.fan_topic}/override/state",
            "availability_topic": self.avail_topic,
            "min": 0, "max": 100, "step": 5, "mode": "slider",
            "unit_of_measurement": "%", "icon": "mdi:fan",
            "device": self.device_block,
        }
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self.client.publish(f"{self.fan_topic}/override/state", self.override,
                            qos=1, retain=True)
        self._announced.add("__override")

    # ---- one control cycle ----
    def compute(self) -> dict:
        """Return the decision dict for one cycle (no IPMI side effects)."""
        result: dict[str, Any] = {"override": self.override}
        group_pct: dict[str, int] = {}
        any_fresh = False
        for group in ("cpu_gpu", "hdd"):
            temps: list[float] = []
            for src in self.cfg["sources"].get(group, []):
                node = src.get("node")
                if not node:
                    continue
                vals = self.cache.values(self._topic_for(node),
                                         src.get("keys"), src.get("key_regex"),
                                         self.stale)
                temps += vals
            if temps:
                any_fresh = True
                gmax = max(temps)
                pct = curve_percent(gmax, self.cfg["curves"][group])
                group_pct[group] = pct
                result[f"{group}_temp"] = round(gmax, 1)
                result[f"{group}_percent"] = pct
        result["any_fresh"] = any_fresh
        if self.override > 0:
            target = self.override
            result["mode"] = "override"
        elif group_pct:
            target = max(group_pct.values())
            result["mode"] = "curve"
        else:
            target = None
            result["mode"] = "stale"
        if target is not None and self.override == 0:
            target = max(target, self.min_percent)
        result["target_percent"] = target
        return result

    def apply(self, decision: dict):
        target = decision.get("target_percent")
        if not decision.get("any_fresh") and self.override == 0:
            # No usable temps: hand control back to the BMC.
            log.warning("No fresh temperatures — restoring automatic fan control")
            self._safe_auto()
            return
        if target is None:
            return
        if self.dry_run:
            log.info("[dry-run] would set fans to %s%%", target)
            return
        try:
            if not self._manual_active:
                self.ipmi.begin_manual()
                self._manual_active = True
                log.info("Manual fan control engaged")
            if target != self._last_percent:
                self.ipmi.set_percent(target)
                self._last_percent = target
                log.info("Fans -> %s%% (%s)", target, decision.get("mode"))
            self._ipmi_failures = 0
        except (IpmiError, subprocess.SubprocessError) as exc:
            self._ipmi_failures += 1
            log.error("IPMI error (%d/%d): %s", self._ipmi_failures,
                      self.fail_threshold, exc)
            if self._ipmi_failures >= self.fail_threshold:
                self._safe_auto()

    def publish_state(self, decision: dict):
        state = {k: v for k, v in decision.items()
                 if isinstance(v, (int, float)) and k != "any_fresh"}
        # ambient temps
        if self.cfg["ambient"]["enabled"] and not self.dry_run:
            try:
                for name, value in self.ipmi.read_temperatures():
                    key = slugify(name)
                    state[key] = round(value, 1)
                    self._announce_sensor(key, name, "°C", "temperature", None)
            except (IpmiError, subprocess.SubprocessError) as exc:
                log.debug("ambient read failed: %s", exc)
        # discovery for the computed values
        self._announce_sensor("target_percent", "Fan Target", "%", None, "mdi:fan")
        if "cpu_gpu_temp" in state:
            self._announce_sensor("cpu_gpu_temp", "CPU/GPU Temp", "°C", "temperature", None)
        if "hdd_temp" in state:
            self._announce_sensor("hdd_temp", "HDD Temp", "°C", "temperature", None)
        self._announce_override()
        self.client.publish(f"{self.fan_topic}/state", json.dumps(state),
                            qos=0, retain=True)
        log.info("State: %s", state)

    def _safe_auto(self):
        """Restore BMC automatic fan control (fail-safe)."""
        if self.dry_run:
            return
        try:
            self.ipmi.restore_auto()
            self._manual_active = False
            self._last_percent = None
            log.info("Automatic fan control restored")
        except (IpmiError, subprocess.SubprocessError) as exc:
            log.error("Could not restore automatic fan control: %s", exc)

    def shutdown(self):
        log.info("Shutting down — restoring automatic fan control")
        self._safe_auto()
        try:
            self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("Received signal %s", signum)
    _running = False


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="IPMI fan controller driven by MQTT temps")
    p.add_argument("config", nargs="?", default=None,
                   help="Path to config.yaml (default /etc/fan-controller/config.yaml)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and publish, but never send fan-control IPMI commands")
    p.add_argument("--once", action="store_true",
                   help="Wait briefly for temps, print one decision, and exit")
    p.add_argument("--probe", action="store_true",
                   help="Dump the BMC's temperature/fan schema and exit "
                   "(read-only; use it to confirm the Huawei Redfish payloads)")
    return p.parse_args(argv)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    cfg = load_config(args.config)

    if args.probe:
        try:
            print(make_ipmi(cfg).probe())
        except Exception as exc:
            log.error("Probe failed: %s", exc)
            sys.exit(1)
        return

    controller = FanController(cfg, dry_run=args.dry_run or args.once)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        controller.connect()
    except Exception as exc:
        log.error("Could not connect to MQTT broker: %s", exc)
        sys.exit(1)

    if args.once:
        time.sleep(min(10, cfg["interval"]))  # let retained temps arrive
        decision = controller.compute()
        print(json.dumps(decision, indent=2))
        controller.client.loop_stop()
        return

    interval = int(cfg["interval"])
    while _running:
        start = time.monotonic()
        try:
            decision = controller.compute()
            controller.apply(decision)
            controller.publish_state(decision)
        except Exception as exc:  # never let the loop die silently
            log.exception("Control cycle error: %s", exc)
        sleep_for = max(1.0, interval - (time.monotonic() - start))
        while _running and sleep_for > 0:
            time.sleep(min(1.0, sleep_for))
            sleep_for -= 1.0

    controller.shutdown()
    log.info("Stopped")


if __name__ == "__main__":
    main()
