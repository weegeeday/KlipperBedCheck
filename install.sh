#!/usr/bin/env bash
# Install the Klipper bed_clear extra, the camera checker service and configs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

KLIPPER_DIR="${KLIPPER_DIR:-$RUN_HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$RUN_HOME/printer_data/config}"
DATA_DIR="${DATA_DIR:-$RUN_HOME/printer_data/bed_clear}"
PYTHON="${PYTHON:-$(command -v python3 || true)}"
EXTRAS_DIR="$KLIPPER_DIR/klippy/extras"

SERVICE_NAME="bed_clear_camera"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"

if [[ -z "$RUN_HOME" ]]; then
    echo "Could not determine the home directory for user $RUN_USER" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$EXTRAS_DIR/bed_clear.py"
    rm -f "$DATA_DIR/camera_checker.py"
    if [[ $EUID -eq 0 ]]; then
        systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
        rm -f "$UNIT_PATH"
        systemctl daemon-reload 2>/dev/null || true
        echo "Service removed."
    else
        echo "Run these as root to remove the service:"
        echo "  sudo systemctl disable --now $SERVICE_NAME"
        echo "  sudo rm -f $UNIT_PATH"
        echo "  sudo systemctl daemon-reload"
    fi
    echo "Removed the Klipper extra and checker."
    echo "Configs, baseline and state were kept in $CONFIG_DIR and $DATA_DIR."
    exit 0
fi

if [[ ! -d "$EXTRAS_DIR" ]]; then
    echo "Klipper not found at $KLIPPER_DIR" >&2
    echo "Set KLIPPER_DIR, for example:" >&2
    echo "  KLIPPER_DIR=/home/pi/klipper bash install.sh" >&2
    exit 1
fi
if [[ ! -d "$CONFIG_DIR" ]]; then
    echo "Config directory not found at $CONFIG_DIR" >&2
    echo "Set CONFIG_DIR, for example:" >&2
    echo "  CONFIG_DIR=/home/pi/klipper_config bash install.sh" >&2
    exit 1
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
    echo "python3 not found; set PYTHON=/path/to/python3" >&2
    exit 1
fi

install -d -m 0755 "$DATA_DIR"
install -m 0644 "$HERE/bed_clear.py" "$EXTRAS_DIR/bed_clear.py"
install -m 0755 "$HERE/camera_checker.py" "$DATA_DIR/camera_checker.py"

if [[ ! -f "$CONFIG_DIR/bed_clear.cfg" ]]; then
    install -m 0644 "$HERE/config/bed_clear.cfg" "$CONFIG_DIR/bed_clear.cfg"
    CFG_NOTE="created"
else
    CFG_NOTE="kept existing"
fi
if [[ ! -f "$CONFIG_DIR/bed_clear_camera.cfg" ]]; then
    install -m 0644 "$HERE/config/bed_clear_camera.cfg" \
        "$CONFIG_DIR/bed_clear_camera.cfg"
    CAM_NOTE="created"
else
    CAM_NOTE="kept existing"
fi

if [[ $EUID -eq 0 ]]; then
    chown -R "$RUN_USER" "$DATA_DIR"
fi

UNIT_TMP="$(mktemp)"
sed -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__CHECKER__|$DATA_DIR/camera_checker.py|g" \
    -e "s|__CAMERA_CONFIG__|$CONFIG_DIR/bed_clear_camera.cfg|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    "$HERE/systemd/bed_clear_camera.service.in" > "$UNIT_TMP"

echo "Installed:"
echo "  $EXTRAS_DIR/bed_clear.py"
echo "  $DATA_DIR/camera_checker.py"
echo "  $CONFIG_DIR/bed_clear.cfg ($CFG_NOTE)"
echo "  $CONFIG_DIR/bed_clear_camera.cfg ($CAM_NOTE)"

if [[ $EUID -eq 0 ]]; then
    install -m 0644 "$UNIT_TMP" "$UNIT_PATH"
    rm -f "$UNIT_TMP"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    if systemctl restart "$SERVICE_NAME"; then
        echo "  $UNIT_PATH (started)"
    else
        echo "  $UNIT_PATH (failed to start; check journalctl -u $SERVICE_NAME)"
    fi
else
    install -m 0644 "$UNIT_TMP" "$CONFIG_DIR/$SERVICE_NAME.service"
    rm -f "$UNIT_TMP"
    echo "  $CONFIG_DIR/$SERVICE_NAME.service"
    echo
    echo "Run these as root to enable the checker service:"
    echo "  sudo cp $CONFIG_DIR/$SERVICE_NAME.service $UNIT_PATH"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now $SERVICE_NAME"
fi

if "$PYTHON" -c 'import PIL' 2>/dev/null; then
    echo
    echo "Image decoder: Pillow"
elif command -v ffmpeg >/dev/null 2>&1; then
    echo
    echo "Image decoder: ffmpeg"
elif command -v convert >/dev/null 2>&1; then
    echo
    echo "Image decoder: ImageMagick"
else
    echo
    echo "WARNING: no image decoder found."
    echo "Install one of:"
    echo "  sudo apt install python3-pil"
    echo "  sudo apt install ffmpeg"
fi

if "$PYTHON" -c 'import onnxruntime' 2>/dev/null; then
    echo "Model backend: onnxruntime available"
else
    echo
    echo "Model backend (optional): onnxruntime is not installed."
    echo "  $PYTHON -m pip install onnxruntime"
    echo "Without it the checker still works in diff mode."
fi

echo
echo "Next:"
echo "  1. Set snapshot_url (or webcam name) in $CONFIG_DIR/bed_clear_camera.cfg"
echo "  2. Add [include bed_clear.cfg] to printer.cfg"
echo "  3. Restart Klipper and the checker service"
echo "  4. Empty the bed, then run BED_CLEAR_CALIBRATE"
echo "  5. Check the service log: journalctl -u $SERVICE_NAME -f"
echo "  6. Optional model backend: copy model.onnx and model_config.json to"
echo "     $DATA_DIR and set backend: both in bed_clear_camera.cfg"
