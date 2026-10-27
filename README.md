# Klipper Bed Clear

A camera-based bed occupancy check for Klipper.

It compares a snapshot of the build plate with an empty-bed baseline, or runs a
small optional ONNX model, and answers a single question: **is the bed clear?**
The result is exposed as `printer.bed_clear` so a print queue can decide
whether to start the next job. Klipper can also refuse `M24` /
`SDCARD_PRINT_FILE` until the bed is verified, which stops a queue before the
first move.

```
   Klipper                         checker service                  camera
+-------------+   HTTP   +-------------------------------+   HTTP   +--------+
| bed_clear.py|<-------->| camera_checker.py             |<-------->| webcam |
| state/gate  |  local   | diff and/or ONNX model        | snapshot |        |
+-------------+          +-------------------------------+          +--------+
       |
       +--> printer.bed_clear.clear  -> queue controller
       +--> M24 / SDCARD_PRINT_FILE guard
```

The image work lives in a small companion service because Klipper's Python
environment is not the right place to fetch and decode camera frames. The diff
backend uses no third-party Python packages; the optional model backend needs
Pillow, numpy and ONNX Runtime.

## How it works

There are three detection backends:

| Backend | Behaviour |
| --- | --- |
| `diff` | Compare the snapshot with an empty-bed baseline. No model, no extra Python packages. |
| `model` | Run a small ONNX classifier trained from this repo's `training/` pipeline. |
| `both` | Require both the diff and the model to report clear. Recommended when deploying a model. |

- `BED_CLEAR_CALIBRATE` captures an empty-bed snapshot and stores it as the
  diff baseline. In `model` mode this is optional; in `diff` and `both` it is
  required.
- `BED_CLEAR_CHECK` captures a new snapshot and runs the configured backend.
- The diff finds changed regions; the model classifies the ROI as clear or
  not-clear. In `both` mode either one can block the job.
- After every SD-card print ends (complete, cancel or error), the bed is marked
  `dirty` until it is verified again.
- The state survives Klipper restarts. If Klipper restarts during a print, the
  bed is marked dirty because the job was interrupted.
- `block_print_start: True` gates `M24` and `SDCARD_PRINT_FILE`, so a queue
  stops before the next job can begin.
- `auto_check_on_start: True` takes a camera snapshot automatically when a
  guarded print is about to start, so a queue can run without a manual check
  between jobs.

## Diff detection algorithm

Every diff check is a comparison between two images: the saved empty-bed
baseline and the new snapshot.

1. **Capture.** The checker fetches one still frame from the snapshot URL or
   runs `snapshot_command`.
2. **Decode and shrink.** The frame is converted to grayscale and resized to
   the `decode_size` grid (80 x 60 by default). Shrinking averages away camera
   noise and makes the comparison fast in pure Python.
3. **Crop.** Only the configured `roi` is compared, so the gantry, background,
   parked toolhead or a status LED cannot trigger the check.
4. **Normalise each image separately.** Every pixel is replaced by
   `(value - mean) / max(std, 8)`. This removes global brightness and contrast
   changes: a cloud passing over the window or a small exposure change does not
   look like an object. The `8` floor stops a nearly uniform image from
   amplifying compression noise.
5. **Difference.** For every grid cell, take the absolute difference between
   the normalised baseline and the normalised snapshot.
6. **Threshold.** Cells with a difference above `cell_threshold` (0.35 by
   default) are marked as changed.
7. **Group.** Changed cells are grouped into 4-connected blobs using flood
   fill. The size of the largest blob is the `largest_component`; the largest
   three blobs are reported as `regions` so you can see where the change is.
8. **Decide.** The bed is **not clear** when either:
   - the largest blob is at least `min_component_cells` (6 cells by default),
     which catches a normal printed part, or
   - the changed fraction is at least `max_changed_fraction` (25% by default),
     which catches a large or spread-out change that never forms one blob.

The result also includes `changed_cells`, `changed_fraction`, `max_diff` and
`mean_diff` for tuning. A clear result means "this looks like the empty-bed
baseline", not "there is definitely no object anywhere on the plate" - see the
limitations below.

Because the comparison is normalised, a perfectly uniform global brightness or
contrast change is ignored. Changing the build sheet changes the surface
texture and edges, so recalibrate after swapping it.

## Model backend

Train a model with the pipeline in `training/` (see
[training/README.md](training/README.md)). The run produces `model.onnx` and
`config.json`. Copy them to the printer:

```bash
scp training/runs/run1/model.onnx pi@printer:~/printer_data/bed_clear/model.onnx
scp training/runs/run1/config.json pi@printer:~/printer_data/bed_clear/model_config.json
```

The installer already installs ONNX Runtime into the checker venv. If you need
to add it later:

```bash
~/printer_data/bed_clear/venv/bin/pip install onnxruntime
```

Then set the backend in `bed_clear_camera.cfg`:

```ini
[camera]
backend: both
```

`model` uses only the classifier; `both` requires the diff and the model to
agree that the bed is clear. `both` is the safer choice.

The checker reads the ROI, input size, normalisation and threshold from the
model's `config.json`. A `model_threshold` in the camera cfg overrides the
stored threshold. New training runs export `0.5`; if you deploy an older run
with a very high threshold, set `model_threshold: 0.5` or retrain. The diff
backend keeps using the camera cfg's `roi`.

The model result appears in `printer.bed_clear` as `backend`, `probability`
and `threshold`. A model that fails to load is reported in
`BED_CLEAR_STATUS` and blocks checks rather than silently falling back.

## Requirements

- A camera that can return a still snapshot over HTTP, or a command that writes
  an image to stdout.
- Python 3 on the printer host.
- An image decoder on the host: **Pillow** (`python3-pil`), **ffmpeg**, or
  ImageMagick. The install script checks for one.
- For the optional model backend: **Pillow**, **numpy** and
  **ONNX Runtime**. The installer puts these in
  `~/printer_data/bed_clear/venv` (see `requirements-checker.txt`). The diff
  backend works without ONNX Runtime.
- Klipper with the standard `virtual_sdcard` / `print_stats` support. This is
  what Moonraker and Mainsail use.
- `block_print_start` needs a Klipper version that allows chaining gcode
  command handlers. On older versions the plugin logs a warning and leaves
  `BED_CLEAR_GATE` available.

## Install

One-line install on the printer:

```bash
git clone https://github.com/weegeeday/KlipperBedCheck.git ~/KlipperBedCheck && sudo bash ~/KlipperBedCheck/install.sh
```

If you already cloned the repo, run the installer from it:

```bash
sudo bash install.sh
```

Run it with `sudo` from your normal printer user, not from a root shell, so the
installer knows which home directory and service user to use.

The installer copies:

| Destination | File |
| --- | --- |
| `~/klipper/klippy/extras/bed_clear.py` | Klipper extra |
| `~/printer_data/bed_clear/camera_checker.py` | Checker service |
| `~/printer_data/bed_clear/venv` | Checker virtual environment |
| `~/printer_data/config/bed_clear.cfg` | Klipper config |
| `~/printer_data/config/bed_clear_camera.cfg` | Camera checker config |
| `/etc/systemd/system/bed_clear_camera.service` | Service unit |

The checker always runs from its own virtual environment. The installer never
uses system `pip`, so PEP 668 / "externally-managed-environment" does not
apply. It installs the packages from `requirements-checker.txt` into the venv.
Override the location with `VENV_DIR=/path/to/venv` if needed.

If you run the installer without root it writes the unit next to the config and
prints the `sudo` commands instead.

Then add this to `printer.cfg` and restart Klipper:

```ini
[include bed_clear.cfg]
```

If Klipper lives somewhere else:

```bash
sudo KLIPPER_DIR=/home/pi/klipper CONFIG_DIR=/home/pi/klipper_config \
  bash install.sh
```

## Configure the camera

Open `~/printer_data/config/bed_clear_camera.cfg`.

The simplest setup leaves `snapshot_url` empty. The checker then asks Moonraker
for the webcam list and uses the first webcam:

```ini
[camera]
snapshot_url:
moonraker_url: http://127.0.0.1:7125
webcam:
```

Set `webcam` to the Moonraker webcam name if there is more than one. If
Moonraker authentication is enabled, add the API key:

```ini
[camera]
webcam: bed
api_key: YOUR_MOONRAKER_API_KEY
```

You can also point directly at a snapshot URL:

```ini
[camera]
snapshot_url: http://127.0.0.1/webcam/?action=snapshot
```

Or let a command produce the snapshot on stdout:

```ini
[camera]
snapshot_command: libcamera-still -o - -t 200 --nopreview --width 640 --height 480
```

The snippets show only the relevant keys; keep them in the single `[camera]`
section of the file.

`snapshot_command` is run through the shell, so pipes are allowed. It takes
precedence over `snapshot_url`.

Check that the service can see the camera:

```bash
curl -s http://127.0.0.1:7126/status | jq
```

`baseline` is `false` until you calibrate. If the service reports a snapshot
error, fix the URL or decoder before continuing.

The service is managed by systemd:

```bash
sudo systemctl status bed_clear_camera
sudo journalctl -u bed_clear_camera -f
sudo systemctl restart bed_clear_camera
```

## First calibration

The baseline must be captured with the bed empty and with the toolhead in the
same position it will be in for later checks. `park_gcode` handles that.

1. Set the bed temperature and lighting to what you normally use.
2. Set a parking macro in `bed_clear.cfg`:

   ```ini
   [bed_clear]
   park_gcode: BED_CLEAR_PARK
   settle_ms: 500

   [gcode_macro BED_CLEAR_PARK]
   gcode:
       G1 Z30 F600
       G1 X10 Y10 F6000
   ```

   Adjust the coordinates and Z height for your printer. The move should keep
   the toolhead out of the bed area, or at least in the same place every time.
   If the toolhead is outside the camera's view you can leave `park_gcode`
   empty, but consistency matters.

3. Home the printer and make sure the plate is completely empty.
4. Run:

   ```gcode
   BED_CLEAR_CALIBRATE
   ```

5. Run `BED_CLEAR_STATUS` and confirm the checker says `baseline=yes`.

A clear result after calibration is an attestation, so it is safe to start the
first print immediately.

## Commands

| Command | Purpose |
| --- | --- |
| `BED_CLEAR_CHECK` | Ask the camera. Raises a gcode error when the bed is not clear. In `manual` mode it only checks the current confirmation. |
| `BED_CLEAR_CALIBRATE` | Capture a new empty-bed camera baseline (diff and both backends; optional for model-only). |
| `BED_CLEAR_CONFIRM` | Mark the bed clear after clearing it by hand. |
| `BED_CLEAR_MARK_DIRTY [REASON=...]` | Mark the bed unverified, e.g. at the end of a custom print macro. |
| `BED_CLEAR_GATE` | Raise an error unless the last check confirmed the bed is clear. Does not take a picture. |
| `BED_CLEAR_STATUS` | Print state, backend, model status, camera metrics, change regions and checker status. |
| `BED_CLEAR_RESET [BASELINE=1]` | Forget the current state; `BASELINE=1` also deletes the camera baseline. |

`BED_CLEAR_CHECK` refuses to run while `print_stats` reports `printing` or
`paused`. Run it while the printer is idle, before starting the next job. Do not
put it inside `PRINT_START`: by then the print already counts as running.

## Queue integration

Read the state through Moonraker:

```bash
curl -s 'http://localhost:7125/printer/objects/query?bed_clear' \
  | jq '.result.status.bed_clear'
```

Important fields:

| Field | Meaning |
| --- | --- |
| `clear` | `true` only when the bed is verified clear, no print is running, and the result has not expired. Use this for the queue decision. |
| `state` | `unknown`, `clear`, `dirty`, `blocked` or `error`. |
| `reason` | Human-readable explanation. |
| `in_print` | A print is currently printing or paused. |
| `last_check` / `last_check_age` | Unix timestamp and age of the last verification. |
| `backend` | `diff`, `model` or `both` from the last check. |
| `probability` | Model `p(not_clear)` when the model backend was used. |
| `threshold` | Model decision threshold. |
| `changed_fraction`, `changed_cells`, `largest_component` | Diff metrics from the last check. |
| `regions` | Up to three changed regions as normalised camera boxes. |
| `checker_url` | The local checker endpoint. |

A queue controller can do:

```python
import requests

def bed_is_clear(base="http://localhost:7125"):
    response = requests.get(
        base + "/printer/objects/query?bed_clear", timeout=5)
    response.raise_for_status()
    state = response.json()["result"]["status"]["bed_clear"]
    return state["clear"], state["reason"]

clear, reason = bed_is_clear()
if clear:
    start_next_queue_job()
else:
    pause_queue(reason)
```

### Stop the queue inside Klipper

Enable:

```ini
[bed_clear]
block_print_start: True
```

Now `M24` and `SDCARD_PRINT_FILE` fail when the state is not `clear`. Mainsail,
Moonraker's job queue and anything else that starts a file through Klipper will
be stopped before the first move. Pausing and resuming a print still works
because `M24` is allowed while the job is paused.

Typical queue flow:

1. Print A ends -> state becomes `dirty`.
2. Queue tries to start print B -> blocked.
3. Remove the part.
4. Run `BED_CLEAR_CHECK` (camera) or `BED_CLEAR_CONFIRM` (manual).
5. Resume the queue.

### Fully automatic queue check

If you want every queued job to be checked without a manual step, enable:

```ini
[bed_clear]
auto_check_on_start: True
```

When `M24` / `SDCARD_PRINT_FILE` is about to start and the state is not clear,
the plugin parks the toolhead, takes a camera snapshot, and allows the job only
if the bed is clear. A blocked or failed check raises a gcode error, so the
queue stops before the first move. This installs the same start guard as
`block_print_start`; `auto_check_on_start` is the more useful option for a
queue.

The automatic check runs before the print starts, so the printer must be idle
and the parking move must be safe with a part still on the plate. Use a
`park_gcode` that raises Z before moving in XY.

### Verify a previous check in `PRINT_START`

`BED_CLEAR_GATE` checks the result of the last `BED_CLEAR_CHECK` /
`BED_CLEAR_CONFIRM`. It does not take a picture, so it is safe inside
`PRINT_START`:

```ini
[gcode_macro PRINT_START]
gcode:
    BED_CLEAR_GATE
    G28
    # ... the rest of your normal start sequence
```

## Tuning the camera check

All camera settings live in `bed_clear_camera.cfg`. The Klipper plugin's state
is `bed_clear.json`; the model's `model_config.json` is generated by training.
Neither is meant to be edited by hand (the model threshold is the exception).

The camera settings are:

| Option | Default | Description |
| --- | --- | --- |
| `snapshot_url` | empty | Webcam still-image URL. Empty means discover from Moonraker. |
| `snapshot_command` | empty | Command that writes an image to stdout. Takes precedence over the URL. |
| `moonraker_url` | `http://127.0.0.1:7125` | Moonraker address for webcam discovery. |
| `webcam` | empty | Moonraker webcam name. Empty means the first webcam. |
| `api_key` | empty | Moonraker API key, if authentication is enabled. |
| `listen_host` | `127.0.0.1` | Checker bind address. Keep it local. |
| `listen_port` | `7126` | Checker port. |
| `data_dir` | `~/printer_data/bed_clear` | Baseline, model and debug files. |
| `backend` | `diff` | `diff`, `model` or `both`. |
| `model_path` | `~/printer_data/bed_clear/model.onnx` | ONNX model file. |
| `model_config` | `~/printer_data/bed_clear/model_config.json` | Training `config.json` copy; provides ROI, size, normalisation and threshold. |
| `model_threshold` | empty | Optional threshold override; default comes from the model config. |
| `decode_size` | `[80, 60]` | Diff grayscale grid size. Larger sees smaller objects, slower. |
| `roi` | `[0, 0, 1, 1]` | Diff region of interest. The model backend uses the ROI from its own config. |
| `cell_threshold` | `0.35` | Normalised brightness difference that counts as changed. Lower is more sensitive. |
| `min_component_cells` | `6` | Smallest connected changed blob that counts as an object. |
| `max_changed_fraction` | `0.25` | Fraction of changed cells that blocks even without a large blob. |
| `snapshot_timeout` | `10` | Snapshot fetch timeout in seconds. |
| `save_debug` | `true` | Save `last_snapshot` and `last_result.json`; `last_diff.pgm` is also saved for diff/both. |
| `token` | empty | Optional shared secret; must match `token` in `bed_clear.cfg`. |

### Set the ROI

The ROI is the part of the image the checker is allowed to look at. Exclude
background, the gantry and any area where the parked toolhead sits:

```ini
[camera]
roi: 0.10, 0.15, 0.90, 0.95
```

The coordinates are `[x0, y0, x1, y1]`, with `0,0` at the top-left and `1,1`
at the bottom-right. After a check, open
`~/printer_data/bed_clear/last_snapshot.jpg` to see what the camera sees.

### Read the debug files

After a check the checker writes:

- `last_snapshot.jpg` (or `.png`) - the raw frame it used.
- `last_diff.pgm` - the normalised difference image for the ROI. Bright areas
  are what triggered the check. Only written by diff/both.
- `last_result.json` - the full result, including model probability or diff
  metrics and regions.

If a check is wrong, look at these first. Most false positives are the toolhead
moving, a shadow, or a light change; most false negatives are the object being
outside the ROI, hidden behind something, or too small for the current
thresholds.

### Model backend tuning

- The model's ROI, input size, normalisation and threshold come from
  `model_config.json`, which is the training run's `config.json`.
- Use `model_threshold` in the camera cfg only if you intentionally want to
  override the trained threshold.
- `both` is the safest deployment: the diff and the model must both report
  clear. If either blocks, the bed is blocked.
- `model` needs no baseline. `both` does; run `BED_CLEAR_CALIBRATE` first.
- If the model fails to load, `BED_CLEAR_STATUS` shows `model: NOT LOADED` with
  the error, and checks fail rather than silently falling back to diff.

### Sensitivity

- Too many false "not clear" results: increase `min_component_cells`, increase
  `cell_threshold`, or tighten the ROI.
- Small parts are missed: decrease `min_component_cells`, decrease
  `cell_threshold`, or increase `decode_size`.
- Recalibrate after changing the camera position, resolution, ROI, lighting,
  or parking position.

### Klipper options

These live in `bed_clear.cfg`:

| Option | Default | Description |
| --- | --- | --- |
| `method` | `camera` | `camera` or `manual`. |
| `checker_url` | `http://127.0.0.1:7126` | Local checker base URL. |
| `check_timeout` | `20` | HTTP timeout for check/calibration, in seconds. |
| `token` | empty | Shared secret; must match the checker's `token`. |
| `park_gcode` | empty | Gcode or macro run before each capture. |
| `settle_ms` | `500` | Wait after parking before the snapshot. |
| `max_age` | `0` | Seconds before a clear result expires; `0` disables. |
| `block_print_start` | `False` | Guard `M24` / `SDCARD_PRINT_FILE`. |
| `auto_check_on_start` | `False` | Run a camera check automatically before a guarded print start. |
| `data_file` | `~/printer_data/config/bed_clear.json` | Klipper state and metrics. |

## Manual mode

For a printer without a camera:

```ini
[bed_clear]
method: manual
```

`BED_CLEAR_CHECK` then only verifies the current confirmation, and
`BED_CLEAR_CONFIRM` is how the bed is marked clear. The print lifecycle logic
is identical, so `block_print_start` and the queue integration still work.

## Troubleshooting

**Checker offline** - `sudo systemctl status bed_clear_camera` and
`sudo journalctl -u bed_clear_camera -f`. Check `snapshot_url`, the API key and
the decoder.

**"no camera baseline"** - Run `BED_CLEAR_CALIBRATE` with an empty bed. The
checker returns HTTP 409 until a baseline exists.

**`model: NOT LOADED`** - The checker is in `model` or `both` mode but could not
load the model. Check:

- `model_path` and `model_config` in `bed_clear_camera.cfg`
- `onnxruntime` is installed in the checker's Python environment
- the model config has two classes including `not_clear`
- the ONNX file is a two-logit classifier exported by `training/train.py`

`BED_CLEAR_STATUS` shows the actual load error. Checks fail until it is fixed;
the checker does not silently fall back to diff.

**"no camera baseline" in `both` mode** - Run `BED_CLEAR_CALIBRATE` with an
empty bed. `model` mode does not need a baseline.

**`externally-managed-environment` / PEP 668** - Do not use system
`python3 -m pip`. The checker runs from
`~/printer_data/bed_clear/venv`. Install into that venv:

```bash
~/printer_data/bed_clear/venv/bin/pip install onnxruntime
```

or re-run `sudo bash install.sh`, which installs `requirements-checker.txt`
into the venv.

If an older install failed partway through, reset it from the repo checkout:

```bash
cd ~/KlipperBedCheck
git pull
sudo bash uninstall.sh          # keeps configs, model and data
sudo bash install.sh
```

Use `sudo bash uninstall.sh --purge` for a completely clean removal.

**"unable to decode camera snapshot"** - Install Pillow or ffmpeg:

```bash
sudo apt install python3-pil
# or
sudo apt install ffmpeg
```

Then restart the checker service.

**"camera returned HTTP 404"** - The checker is reaching a web server but the
snapshot path is wrong. From the printer, check what Moonraker reports and test
the URL:

```bash
curl -s http://127.0.0.1:7125/server/webcams/list \
  | jq '.result.webcams[] | {name, snapshot_url, stream_url}'
curl -sI 'http://127.0.0.1/webcam/?action=snapshot'
```

Put the working URL in `snapshot_url`, then restart the checker. Editing
`bed_clear_camera.cfg` does not affect a running service.

```bash
sudo systemctl restart bed_clear_camera
sudo systemctl status bed_clear_camera
```

Relative Moonraker webcam URLs are resolved against the web frontend, not the
Moonraker API port.

**Snapshot fetch fails** - Open the `snapshot_url` in a browser on the printer
host. If Moonraker authentication is enabled, set `api_key`.

**False "not clear"** - The toolhead probably moved between the baseline and the
check. Set `park_gcode`, increase `settle_ms`, or exclude the toolhead with the
ROI. Also check for shadows and lighting changes.

**False "clear"** - The object is outside the ROI, hidden behind the gantry, or
smaller than the thresholds. Tighten the ROI/thresholds or move the camera.

**The checker says clear after Klipper restarts** - The state is persisted.
Use `max_age` to expire old confirmations, or run `BED_CLEAR_CHECK` after a
restart.

**`block_print_start` did not take effect** - Check `klippy.log` for a warning
about chaining gcode handlers. The installed Klipper is too old for that guard;
use `BED_CLEAR_GATE` in `PRINT_START` instead.

**Where is the data?** - `BED_CLEAR_STATUS` prints the Klipper state path.
The camera baseline and debug images are in the checker's `data_dir`
(default `~/printer_data/bed_clear`).

## Limitations

- A single camera sees one side of the bed. An object hidden behind the gantry,
  under a shadow, or outside the frame is invisible.
- A 2D check cannot detect an object that looks exactly like the empty bed from
  that angle.
- Small or very thin leftovers may be below the thresholds. Tune carefully.
- Lighting changes and camera exposure changes can affect the result. The
  brightness normalisation helps, but recalibrate if the light changes a lot.
- This is a convenience check, not a safety interlock. Do not rely on it to
  prevent injury or equipment damage.

## Uninstall

Remove the service, Klipper extra, checker and venv while keeping configs,
model and data:

```bash
sudo bash uninstall.sh
```

Remove everything, including configs, model, baseline, state and debug data:

```bash
sudo bash uninstall.sh --purge
```

Preview first with:

```bash
sudo bash uninstall.sh --dry-run
```

Then remove `[include bed_clear.cfg]` from `printer.cfg` and restart Klipper.
`uninstall.sh` is safe to run against a partial or failed install: missing
files and services are ignored.

## Model training

No model is shipped with this repo. The `training/` folder contains a tiny-CNN
pipeline for training your own and deploying it to the checker. Frames live in
`data/frames/` and are gitignored.

- `python training/audit_dataset.py` reports duplicates and variation.
- `python training/draw_roi.py` opens an interactive ROI drawer.
- `python training/show_roi.py --roi ...` previews the ROI over random frames.
- `python training/train.py` trains, evaluates and exports ONNX. If ONNX
  export fails, the checkpoint, config and metrics are still written.
- `training/colab.ipynb` is the Colab workflow.

Deploy a run:

```bash
scp training/runs/run1/model.onnx \
  pi@printer:~/printer_data/bed_clear/model.onnx
scp training/runs/run1/config.json \
  pi@printer:~/printer_data/bed_clear/model_config.json
```

Install `onnxruntime` in the checker's Python environment, set `backend: both`
(or `model`) in `bed_clear_camera.cfg`, and restart the checker service. Use the
same ROI in training and deployment. See
[training/README.md](training/README.md) and [data/README.md](data/README.md).

Treat model accuracy as preliminary until you have validated it on a separate
session/day. The test split from a single recording overstates generalisation.

## Tests

The checker, image comparison, HTTP endpoints and the Klipper state machine are
covered without needing a printer or a camera:

```bash
python -m unittest discover -s tests -v
```

The end-to-end image test uses Pillow and is skipped if it is not installed.

## License

MIT, see [LICENSE](LICENSE).
