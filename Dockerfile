# Container image for server_monitor — bundles the sensor tools so nothing
# needs to be installed on the host. Primarily for TrueNAS SCALE, where the
# host is an appliance (no apt, immutable root), but works anywhere Docker runs.
FROM python:3.12-slim

# lm-sensors -> CPU temps, smartmontools -> disk temps.
# (NVIDIA GPU temps need the NVIDIA container runtime; not included here.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        lm-sensors \
        smartmontools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server_monitor.py .

# Config is mounted in at runtime (see docker-compose.truenas.yml).
ENTRYPOINT ["python", "/app/server_monitor.py"]
CMD ["/etc/server-monitor/config.yaml"]
