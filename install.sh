#!/bin/bash
# Copyright 2026 David Mitchell <git@themitchells.org>
set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

echo "Installing wifi_monitor from $INSTALL_DIR"

# 1. Copy example configs if not present
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "Created .env from .env.example."
    echo "Edit $INSTALL_DIR/.env with your tokens and passwords, then re-run install.sh."
    exit 0
fi

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    cp "$INSTALL_DIR/config.yaml.example" "$INSTALL_DIR/config.yaml"
    echo ""
    echo "Created config.yaml from config.yaml.example."
    echo "Edit $INSTALL_DIR/config.yaml with your AP names and SSH key path, then re-run install.sh."
    exit 0
fi

# 2. Create Python venv and install dependencies
echo "Setting up Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/collector/requirements.txt"

# 3. Start Docker stack (InfluxDB + Grafana)
echo "Starting Docker stack..."
cd "$INSTALL_DIR"
docker compose up -d

# 4. Install systemd units (substitute INSTALL_DIR into service files)
echo "Installing systemd units..."
for unit in "$INSTALL_DIR/systemd/"*.service "$INSTALL_DIR/systemd/"*.timer; do
    unitname=$(basename "$unit")
    sed "s|INSTALL_DIR|$INSTALL_DIR|g" "$unit" > "/etc/systemd/system/$unitname"
    echo "  Installed $unitname"
done

systemctl daemon-reload
systemctl enable --now network-collector.timer

echo ""
echo "Done."
echo "  Grafana:  http://localhost:3000  (admin / value from GRAFANA_ADMIN_PASSWORD in .env)"
echo "  InfluxDB: http://localhost:8086"
echo ""
echo "Check timer status with: systemctl list-timers network-collector.timer"
