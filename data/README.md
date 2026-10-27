# Frame data

This directory holds the training frames. The frames themselves are ignored by
git; only this README is tracked.

```
data/
  frames/
    clear/       *.jpg
    not_clear/   *.jpg
```

The training scripts also accept the original folder names
`BedClearframes/` and `NotClearFrames/`, so either layout works.

## Adding frames

Copy images into `clear/` or `not_clear/`. Keep the original filenames or use
sequential names; the audit and training scripts sort by filename and split by
contiguous blocks.

Do not mix frames from different sessions into the same folder without keeping
the session/date in the metadata or filename. Splitting and accuracy checks
need to know which frames came from the same recording.

## Zip for Colab

From the repository root:

```bash
cd data/frames
zip -qr ../../frames.zip clear not_clear
```

Upload that `frames.zip` in the Colab notebook. Alternatively, mount Google
Drive and point `--data` at the extracted folder.

## Before training

Run the audit:

```bash
python training/audit_dataset.py --data data/frames --out training/reports
```

It reports the effective number of unique frames. Sequential video frames are
often 99% duplicates; if the unique count is low, collect more scenes rather
than training on the duplicates.
