#!/usr/bin/env bash
# Install the Klipper bed_clear extra, the camera checker venv and the service.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

KLIPPER_DIR="${KLIPPER_DIR:-$RUN_HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$RUN_HOME/printer_data/config}"
DATA_DIR="${DATA_DIR:-$RUN_HOME/printer_data/bed_clear}"
BASE_PYTHON="${PYTHON:-$(command -v python3 || true)}"
VENV_DIR="${VENV_DIR:-$DATA_DIR/venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
EXTRAS_DIR="$KLIPPER_DIR/klippy/extras"

SERVICE_NAME="bed_clear_camera"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
REQUIREMENTS="$HERE/requirements-checker.txt"

if [[ "${1:-}" == "--uninstall" ]]; then
    shift
    exec bash "$HERE/uninstall.sh" "$@"
fi

if [[ -z "$RUN_HOME" ]]; then
    echo "Could not determine the home directory for user $RUN_USER" >&2
    exit 1
fi
if [[ -z "$DATA_DIR" || "$DATA_DIR" == "/" ]]; then
    echo "Refusing to use DATA_DIR=$DATA_DIR" >&2
    exit 1
fi
if [[ -z "$VENV_DIR" || "$VENV_DIR" == "/" ]]; then
    echo "Refusing to use VENV_DIR=$VENV_DIR" >&2
    exit 1
fi
if [[ ! -d "$EXTRAS_DIR" ]]; then
    echo "Klipper not found at $KLIPPER_DIR" >&2
    echo "Set KLIPPER_DIR, for example:" >&2
    echo "  KLIPPER_DIR=/home/pi/klipper sudo bash install.sh" >&2
    exit 1
fi
if [[ ! -d "$CONFIG_DIR" ]]; then
    echo "Config directory not found at $CONFIG_DIR" >&2
    echo "Set CONFIG_DIR, for example:" >&2
    echo "  CONFIG_DIR=/home/pi/klipper_config sudo bash install.sh" >&2
    exit 1
fi
if [[ -z "$BASE_PYTHON" || ! -x "$BASE_PYTHON" ]]; then
    echo "python3 not found; set PYTHON=/path/to/python3" >&2
    exit 1
fi
if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "Missing $REQUIREMENTS" >&2
    exit 1
fi

# Create the data directory first so the venv can live inside it.
if [[ $EUID -eq 0 ]]; then
    install -d -m 0755 "$DATA_DIR"
    chown "$RUN_USER" "$DATA_DIR"
else
    install -d -m 0755 "$DATA_DIR"
fi

if [[ -e "$VENV_DIR" && ! -x "$VENV_PYTHON" ]]; then
    echo "$VENV_DIR exists but is not a usable virtual environment." >&2
    echo "Move it aside or set VENV_DIR to a new path." >&2
    exit 1
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Creating virtual environment at $VENV_DIR"
    if ! "$BASE_PYTHON" -m venv "$VENV_DIR"; then
        echo "Could not create a virtual environment." >&2
        echo "On Raspberry Pi OS install the venv package:" >&2
        echo "  sudo apt install python3-venv" >&2
        exit 1
    fi
fi
if [[ $EUID -eq 0 ]]; then
    chown -R "$RUN_USER" "$VENV_DIR"
fi

"$VENV_PYTHON" -m pip install --disable-pip-version-check \
    --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check \
        --no-cache-dir -r "$REQUIREMENTS"; then
    echo "WARNING: full checker dependency install failed." >&2
    if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check \
            --no-cache-dir "Pillow>=9.0" "numpy>=1.21"; then
        echo "ERROR: Pillow/numpy could not be installed." >&2
        echo "The checker needs an image decoder." >&2
        echo "Install python3-pil or ffmpeg." >&2
    fi
fi
if [[ $EUID -eq 0 ]]; then
    chown -R "$RUN_USER" "$VENV_DIR"
fi

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

# Validate the config and model. Missing model files are a warning: the
# service can still be installed and calibrated in diff mode.
if CHECK_OUTPUT="$("$VENV_PYTHON" "$DATA_DIR/camera_checker.py" \
        --config "$CONFIG_DIR/bed_clear_camera.cfg" --check-config 2>&1)"; then
    echo "Checker config: OK"
else
    echo "$CHECK_OUTPUT" >&2
    echo "WARNING: checker config/model check failed." >&2
    echo "The service will still be installed; checks fail until this is fixed." >&2
    echo "Check BED_CLEAR_STATUS and journalctl -u $SERVICE_NAME." >&2
fi

UNIT_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP"' EXIT
sed -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__PYTHON__|$VENV_PYTHON|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    -e "s|__CHECKER__|$DATA_DIR/camera_checker.py|g" \
    -e "s|__CAMERA_CONFIG__|$CONFIG_DIR/bed_clear_camera.cfg|g" \
    "$HERE/systemd/bed_clear_camera.service.in" > "$UNIT_TMP"

echo "Installed:"
echo "  $EXTRAS_DIR/bed_clear.py"
echo "  $DATA_DIR/camera_checker.py"
echo "  $VENV_DIR (checker virtual environment)"
echo "  $CONFIG_DIR/bed_clear.cfg ($CFG_NOTE)"
echo "  $CONFIG_DIR/bed_clear_camera.cfg ($CAM_NOTE)"

if [[ $EUID -eq 0 ]]; then
    install -m 0644 "$UNIT_TMP" "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    if systemctl restart "$SERVICE_NAME" \
            && systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "  $UNIT_PATH (running)"
    else
        echo "  $UNIT_PATH (failed to start)" >&2
        echo "  journalctl -u $SERVICE_NAME -n 50 --no-pager" >&2
    fi
else
    install -m 0644 "$UNIT_TMP" "$CONFIG_DIR/$SERVICE_NAME.service"
    echo "  $CONFIG_DIR/$SERVICE_NAME.service"
    echo
    echo "Run these as root to enable the checker service:"
    echo "  sudo cp $CONFIG_DIR/$SERVICE_NAME.service $UNIT_PATH"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now $SERVICE_NAME"
fi

if "$VENV_PYTHON" -c 'import PIL' 2>/dev/null; then
    echo "Image decoder: Pillow (venv)"
elif command -v ffmpeg >/dev/null 2>&1; then
    echo "Image decoder: ffmpeg (system binary)"
else
    echo "WARNING: no image decoder found." >&2
    echo "  $VENV_PYTHON -m pip install Pillow" >&2
    echo "  or: sudo apt install ffmpeg" >&2
fi

if "$VENV_PYTHON" -c 'import onnxruntime' 2>/dev/null; then
    echo "Model backend: onnxruntime available (venv)"
else
    echo "Model backend (optional): onnxruntime is not available in the venv."
    echo "  $VENV_PYTHON -m pip install onnxruntime"
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
