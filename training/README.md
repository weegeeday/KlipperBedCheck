# Training

A tiny PyTorch CNN that classifies a bed image as `clear` or `not_clear`.

## Layout

```
training/
  audit_dataset.py    # duplicate/variation report and contact sheet
  train.py            # training, evaluation, ONNX export
  requirements.txt
  colab.ipynb         # Colab workflow
```

Frames live under `data/frames/clear` and `data/frames/not_clear` and are
gitignored.

## 1. Audit first

```bash
python training/audit_dataset.py --data data/frames --out training/reports
```

This writes `training/reports/audit.json` and `contact_sheet.jpg`, and prints
the effective unique counts.

**The current batch is still small.** The latest Colab audit reported
approximately:

```
clear      211 files -> 52 unique
not_clear  295 files -> 27 unique (21 unique within a bed-only crop)
```

That is enough to exercise the pipeline, but not enough to trust accuracy. The
training split had roughly 36 clear and 19 not_clear images, and the validation
and test sets had only 12 images each. A jump to 100% validation accuracy on
12 images is not meaningful evidence.

The not-clear set still has relatively little bed variation; much of its
difference is the toolhead/gantry moving. Capture the same toolhead positions
for both classes, or park it identically for both. Add varied parts, failed
prints, purge lines and multiple arrangements.

Target at least 20–30 unique images per class before calling a result
meaningful, and preferably hundreds across several sessions.

## Drawing the ROI

```bash
python training/draw_roi.py
```

Drag a box around the bed, then press:

- `s` — print the ROI and close
- `n` — next image
- `r` — reset the selection
- `q` — quit

It prints the normalised values in the format needed by `train.py` and the
camera checker config:

```
ROI: 0.0300, 0.5200, 0.8500, 0.9900
training: python training/train.py ... --roi 0.03,0.52,0.85,0.99
camera cfg: roi: 0.03, 0.52, 0.85, 0.99
```

Useful options:

```bash
python training/draw_roi.py --label both
python training/draw_roi.py --image data/frames/clear/00001.jpg
python training/draw_roi.py --out roi.json
```

A graphical display is required. On a headless printer host, run it on a
desktop machine or use X forwarding.

## Inspecting the ROI

Before training, check that the ROI actually covers the bed:

```bash
python training/show_roi.py --roi 0.03,0.52,0.85,0.99 --count 6
```

It opens a window with six random frames, a red ROI rectangle, and an inset of
the cropped region the model will see.

Useful options:

- `--label clear` or `--label not_clear` to sample one class
- `--seed 42` for a reproducible sample
- `--save roi.jpg` to keep the preview
- `--no-show` for a headless/SSH session; it saves
  `training/reports/roi_preview.jpg` instead

Use the same ROI in `train.py --roi ...` and in the deployment checker.

## 2. Train locally

```bash
python training/train.py \
  --data data/frames \
  --out training/runs/run1 \
  --epochs 40 \
  --img-size 128 \
  --batch-size 32
```

The script:

- deduplicates near-identical frames (`--dedupe-threshold`, default `0.005`)
- splits each class by contiguous filename blocks into train/val/test
- refuses to train with fewer than `--min-unique` unique images per class
  (`--force` bypasses the count check for pipeline testing only)
- trains a ~146k-parameter CNN at `--width 16`
- reports accuracy, `not_clear` precision/recall, ROC AUC and a confusion matrix
- chooses a threshold on validation to meet `--target-recall` (default `0.98`)
- exports `best.pt`, `model.onnx`, `config.json`, `metrics.json`,
  `confusion_matrix.png` and `samples.png`

`--roi x0,y0,x1,y1` crops the frames before resizing. Use the same ROI the
deployment checker will use. The values are normalised image coordinates.

The `not_clear` recall matters most: a false “clear” is the dangerous error.
A false “not clear” only costs a manual confirmation.

## 3. Train in Colab

Open `training/colab.ipynb` in Colab. It:

1. clones this repo
2. installs the requirements
3. accepts an uploaded `frames.zip`
4. runs the audit
5. runs training
6. displays metrics and lets you download the run

Create the zip with:

```bash
cd data/frames
zip -qr ../../frames.zip clear not_clear
```

## 4. Read the results honestly

Randomly splitting sequential video frames inflates accuracy. This script uses
contiguous blocks to reduce that, but a single recording is still one session.
For a trustworthy result:

- collect at least several sessions/days and lighting states
- keep a held-out session that the model has never seen
- check `test_at_deployment` in `metrics.json`, especially
  `not_clear_recall` and the confusion matrix
- compare `test_at_deployment` with `test_at_recommended`; the recommended
  threshold is informational and is not written to `config.json`
- look at `samples.png` for false clear/false not-clear examples

Once the model is acceptable, deploy the ONNX model through the checker
service. Keep the image-diff result as a fallback: if the diff shows a large
change, do not allow a `clear` prediction to override it.

## 5. Deploy to the checker

No model is included in this repository; deploy the run you trained.

```bash
scp training/runs/run1/model.onnx \
  pi@printer:~/printer_data/bed_clear/model.onnx
scp training/runs/run1/config.json \
  pi@printer:~/printer_data/bed_clear/model_config.json
```

ONNX Runtime is only needed on the printer, not for training. Install it in
the Python environment that runs the checker (the same `PYTHON` used by the
systemd unit):

```bash
python3 -m pip install onnxruntime
```

Then edit `~/printer_data/config/bed_clear_camera.cfg`:

```ini
[camera]
backend: both
```

`both` requires the diff and the model to agree that the bed is clear. If you
use `both`, run `BED_CLEAR_CALIBRATE` once for the diff baseline. `model` mode
does not need a baseline.

Check the `threshold` in the run's `config.json`. Current training runs export
`0.5`. If you are deploying an older run whose config says `0.98`, either
retrain with the current script or add `model_threshold: 0.5` to
`bed_clear_camera.cfg`.

Restart the checker and verify that the model loaded:

```bash
sudo systemctl restart bed_clear_camera
curl -s http://127.0.0.1:7126/status | jq '.model'
```

In Klipper, `BED_CLEAR_STATUS` shows the backend, model state and the last
`p(not_clear)`.

## Model run notes

The current model uses 146,130 parameters and exports a 588 KB ONNX file. The
latest dataset has 263 unique clear and 328 unique not-clear frames, split into
415 train / 88 validation / 88 test.

On the 88-image test split:

| Threshold | Accuracy | not-clear recall | Missed not-clear |
| --- | ---: | ---: | ---: |
| `0.5` | 100% | 100% | 0 |
| `0.9` | 100% | 100% | 0 |
| `0.95` | 98.9% | 98.0% | 1 |
| `0.98` | 95.5% | 91.8% | 4 |

The auto-suggested threshold (`0.98`) is too aggressive: it was chosen to meet
98% recall on validation, but it missed four test frames that were just below
the cutoff. The exported `config.json` now uses the explicit `--threshold`
value, not the auto suggestion. The default is `0.5`; the recommendation is
still recorded for reference.

On the current unique local frames the model separates cleanly:

```
clear      max p(not_clear) = 0.020
not_clear  min p(not_clear) = 0.934
```

That is a strong margin at `0.5`, but it is not an independent test: the frames
are from the same recording/session. Keep a separate session for the final
evaluation.

## Current limitation

The current ROI (`0.2891, 0.5501, 0.9964, 0.9242`) excludes most of the toolhead
and shows the bed clearly. Keep the camera fixed and use the same ROI for
training and deployment. If the camera moves, redraw the ROI with
`training/draw_roi.py` and retrain.

The model still cannot see what the camera does not show: an object hidden
behind the gantry or outside the ROI is invisible. Use a separate session/day
for the final accuracy check.
