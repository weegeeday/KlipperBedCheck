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
- check `test_at_recommended` in `metrics.json`, especially
  `not_clear_recall` and the confusion matrix
- look at `samples.png` for false clear/false not-clear examples

Once the model is acceptable, deploy the ONNX model through the checker
service. Keep the image-diff result as a fallback: if the diff shows a large
change, do not allow a `clear` prediction to override it.

## Current limitation

The camera frames inspected so far show the toolhead filling most of the frame
and the bed in a thin strip at the bottom. That is a poor view for occupancy
detection. Before collecting more, adjust the camera angle or set a tight ROI
around the visible bed. The training script supports `--roi`, but the model
cannot see what the camera does not show.
