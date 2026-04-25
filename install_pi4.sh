#!/usr/bin/env bash
set -euo pipefail

STACK_DIR="/opt/meshcore-wm1302"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash install_pi4.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer expects Debian/Raspberry Pi OS (apt-get)."
  exit 1
fi

echo "[1/8] Installing OS packages"
apt-get update
apt-get install -y git build-essential pkg-config python3 python3-venv python3-pip libffi-dev libssl-dev

echo "[2/8] Enabling SPI"
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_spi 0 || true
fi

if [[ ! -d "$STACK_DIR" ]]; then
  echo "[3/8] Creating $STACK_DIR"
  mkdir -p "$STACK_DIR"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[4/8] Syncing stack files"
rsync -a --delete "$SCRIPT_DIR/" "$STACK_DIR/"

echo "[5/8] Creating Python venv"
python3 -m venv "$STACK_DIR/.venv"
"$STACK_DIR/.venv/bin/pip" install --upgrade pip

echo "[6/8] Installing SX1302 HAL"
if [[ ! -d /opt/sx1302_hal ]]; then
  git clone --depth 1 https://github.com/Lora-net/sx1302_hal.git /opt/sx1302_hal
fi

pushd /opt/sx1302_hal > /dev/null
make clean || true
make all
popd > /dev/null

echo "[7/8] Installing default bridge env"
if [[ ! -f /etc/default/meshcore-semtech-bridge ]]; then
  cp "$STACK_DIR/.env.example" /etc/default/meshcore-semtech-bridge
fi

echo "[8/8] Installing systemd units"
cp "$STACK_DIR/systemd/meshcore-semtech-bridge.service" /etc/systemd/system/
cp "$STACK_DIR/systemd/wm1302-pkt-fwd.service" /etc/systemd/system/
sed -i "s#/usr/bin/python3#$STACK_DIR/.venv/bin/python3#g" /etc/systemd/system/meshcore-semtech-bridge.service

systemctl daemon-reload
systemctl enable meshcore-semtech-bridge.service
systemctl enable wm1302-pkt-fwd.service

echo "Installation complete."
echo "Next:"
echo "1) Copy region config to /opt/meshcore-wm1302/config/global_conf.json"
echo "2) Edit /etc/default/meshcore-semtech-bridge"
echo "3) systemctl restart meshcore-semtech-bridge wm1302-pkt-fwd"
