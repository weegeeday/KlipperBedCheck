#!/usr/bin/env bash
# Remove the bed_clear Klipper extra, checker service and venv.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

KLIPPER_DIR="${KLIPPER_DIR:-$RUN_HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$RUN_HOME/printer_data/config}"
DATA_DIR="${DATA_DIR:-$RUN_HOME/printer_data/bed_clear}"
VENV_DIR="${VENV_DIR:-$DATA_DIR/venv}"
EXTRAS_DIR="$KLIPPER_DIR/klippy/extras"

SERVICE_NAME="bed_clear_camera"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"

PURGE=0
DRY_RUN=0

usage() {
    cat <<EOF
Usage: sudo bash uninstall.sh [--purge] [--dry-run]

  --purge    also remove configs, model, baseline, state and debug data
  --dry-run  print what would be removed without changing anything

Without --purge, the service, Klipper extra, checker and venv are removed.
Configs, model.onnx, model_config.json, baseline and debug data are kept.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

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
if [[ "$EXTRAS_DIR" != */klippy/extras ]]; then
    echo "Refusing to remove from unexpected extras path: $EXTRAS_DIR" >&2
    exit 1
fi
if [[ -z "$CONFIG_DIR" || "$CONFIG_DIR" == "/" ]]; then
    echo "Refusing to use CONFIG_DIR=$CONFIG_DIR" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo so the systemd service can be stopped:" >&2
    echo "  sudo bash uninstall.sh $*" >&2
    exit 1
fi

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

rm_file() {
    if [[ -e "$1" ]]; then
        run rm -f -- "$1"
    fi
}

rm_dir() {
    if [[ -d "$1" ]]; then
        run rm -rf -- "$1"
    fi
}

if [[ $DRY_RUN -eq 0 ]]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload 2>/dev/null || true
else
    echo "[dry-run] systemctl disable --now $SERVICE_NAME"
    echo "[dry-run] rm -f $UNIT_PATH"
    echo "[dry-run] systemctl daemon-reload"
fi

rm_file "$EXTRAS_DIR/bed_clear.py"
rm_file "$DATA_DIR/camera_checker.py"
rm_file "$CONFIG_DIR/$SERVICE_NAME.service"
rm_dir "$VENV_DIR"

if [[ $PURGE -eq 1 ]]; then
    rm_file "$CONFIG_DIR/bed_clear.cfg"
    rm_file "$CONFIG_DIR/bed_clear_camera.cfg"
    rm_file "$CONFIG_DIR/bed_clear.json"
    rm_dir "$DATA_DIR"
    echo "Removed configs and data, including any model, baseline and state."
else
    echo "Kept configs, model, baseline and debug data:"
    echo "  $CONFIG_DIR/bed_clear.cfg"
    echo "  $CONFIG_DIR/bed_clear_camera.cfg"
    echo "  $DATA_DIR"
fi

echo
echo "Remaining manual step:"
echo "  remove [include bed_clear.cfg] from printer.cfg and restart Klipper."
if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry run complete; nothing was changed."
fi
