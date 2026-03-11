#!/bin/bash

set -e

echo "========================================"
echo "  Raspberry Pi Zero 2 W Setup Script"
echo "========================================"
echo ""

# ── Package Installation ──────────────────────────────────────────────────────

echo "[1/5] Installing apt packages..."
sudo apt update -y

sudo apt install -y swig
echo "  ✓ swig"

sudo apt install -y liblgpio-dev
echo "  ✓ liblgpio-dev"

sudo apt install -y libcap-dev
echo "  ✓ libcap-dev"

sudo apt install -y python3-picamera2
echo "  ✓ python3-picamera2"

# ── Install uv ────────────────────────────────────────────────────────────────

echo ""
echo "[2/5] Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | sh
echo "  ✓ uv installed"

# Source uv into current shell so it's usable immediately if needed
export PATH="$HOME/.local/bin:$PATH"

# ── Enable SPI ────────────────────────────────────────────────────────────────

echo ""
echo "[3/5] Enabling SPI..."

CONFIG_FILE="/boot/firmware/config.txt"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "  ✗ ERROR: $CONFIG_FILE not found. Is this a Raspberry Pi?"
    exit 1
fi

# Check if SPI is already enabled (uncommented)
if grep -qE "^dtparam=spi=on" "$CONFIG_FILE"; then
    echo "  ✓ SPI already enabled, skipping"
else
    # Uncomment the line if it's commented out
    if grep -qE "^#dtparam=spi=on" "$CONFIG_FILE"; then
        sudo sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' "$CONFIG_FILE"
        echo "  ✓ SPI enabled (uncommented existing line)"
    else
        # Append if the line doesn't exist at all
        echo "dtparam=spi=on" | sudo tee -a "$CONFIG_FILE" > /dev/null
        echo "  ✓ SPI enabled (appended to config)"
    fi
fi

# ── Clone DuckWild Repo ───────────────────────────────────────────────────────

echo ""
echo "[4/5] Cloning DuckWild repository..."

DUCK_DIR="$HOME/DuckWild"

if [ -d "$DUCK_DIR" ]; then
    echo "  ! ~/DuckWild already exists, skipping clone"
else
    git clone https://github.com/michaelalbertslo/DuckWild "$DUCK_DIR"
    echo "  ✓ Cloned into ~/DuckWild"
fi

# ── Create & Enable systemd Service ──────────────────────────────────────────

echo ""
echo "[5/5] Creating wildduck systemd service..."

# Resolve paths from the current user's environment
SERVICE_USER="$(whoami)"
USER_DIR="/home/${SERVICE_USER}"
DUCKWILD_DIR="${USER_DIR}/DuckWild"
UV_PATH="${USER_DIR}/.local/bin/uv"
STARTUP_SCRIPT="${DUCKWILD_DIR}/wild_duck.py"
SERVICE_FILE="/etc/systemd/system/wildduck.service"
FLAGS="--no-group dev"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=WildDuck Boot
After=network.target
Wants=network.target
After=multi-user.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${DUCKWILD_DIR}
ExecStart=${UV_PATH} run ${FLAGS} ${STARTUP_SCRIPT}

# Restart only when the program crashes or exits non-zero. Wait 2 seconds before restarting.
Restart=on-failure
RestartSec=2

# Label the log so it's easily found
SyslogIdentifier=wildduck

[Install]
WantedBy=multi-user.target
EOF

echo "  ✓ Service file written to $SERVICE_FILE"

sudo systemctl daemon-reload
echo "  ✓ systemd daemon reloaded"

sudo systemctl enable wildduck.service
echo "  ✓ wildduck.service enabled"

sudo systemctl restart wildduck.service
echo "  ✓ wildduck.service started"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  NOTE: A reboot is required for SPI"
echo "  to take effect."
echo ""
echo "  Run: sudo reboot"
echo "========================================"
