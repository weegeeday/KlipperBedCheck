#!/usr/bin/env python3
"""Audit a clear / not_clear frame dataset before training.

Sequential video frames are often near-duplicates. This reports the effective
number of unique images, class balance, brightness and a contact sheet.
"""

import argparse
import json
import pathlib
import sys

import numpy as np
from PIL import Image, ImageDraw

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
CLASSES = ('clear', 'not_clear')
ALIASES = {
    'clear': {'clear', 'bedclearframes', 'bed_clear'},
    'not_clear': {'not_clear', 'notclearframes', 'notclear', 'not clear'},
}


def find_class_dir(root, label):
    for path in sorted(pathlib.Path(root).iterdir()):
        if path.is_dir() and path.name.lower() in ALIASES[label]:
            return path
    return None


def image_files(folder):
    return sorted(path for path in pathlib.Path(folder).iterdir()
                  if path.is_file() and path.suffix.lower() in IMG_EXTS)


def fingerprint(path, size=(32, 32)):
    with Image.open(path) as image:
        return np.asarray(image.convert('L').resize(size, Image.BILINEAR),
                          dtype=np.float32) / 255.0


def dedupe(paths, threshold):
    kept_paths = []
    kept_fps = []
    for path in paths:
        fp = fingerprint(path)
        if any(np.mean(np.abs(fp - other)) < threshold for other in kept_fps):
            continue
        kept_paths.append(path)
        kept_fps.append(fp)
    return kept_paths, kept_fps


def consecutive_stats(paths):
    if len(paths) < 2:
        return None
    diffs = []
    previous = fingerprint(paths[0])
    for path in paths[1:]:
        current = fingerprint(path)
        diffs.append(float(np.mean(np.abs(current - previous))))
        previous = current
    return {
        'min': min(diffs),
        'median': float(np.median(diffs)),
        'max': max(diffs),
    }


def make_contact_sheet(paths_by_class, out_path, per_class, thumb=(320, 180)):
    rows = []
    for label in CLASSES:
        paths = paths_by_class.get(label, [])
        if not paths:
            continue
        indexes = np.linspace(0, len(paths) - 1, min(per_class, len(paths)))
        row = []
        for index in indexes.astype(int):
            with Image.open(paths[index]) as image:
                image = image.convert('RGB')
                image.thumbnail(thumb)
                tile = Image.new('RGB', (thumb[0], thumb[1] + 22), 'black')
                tile.paste(image, ((thumb[0] - image.width) // 2, 22))
                ImageDraw.Draw(tile).text(
                    (5, 4), '%s %d' % (label, index), fill='white')
                row.append(tile)
        rows.append(row)
    if not rows:
        return
    width = thumb[0] * max(len(row) for row in rows)
    height = (thumb[1] + 22) * len(rows)
    sheet = Image.new('RGB', (width, height), 'black')
    for r, row in enumerate(rows):
        for c, tile in enumerate(row):
            sheet.paste(tile, (c * thumb[0], r * (thumb[1] + 22)))
    sheet.save(out_path, quality=88)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/frames',
                        help='directory containing clear/ and not_clear/')
    parser.add_argument('--out', default='training/reports')
    parser.add_argument('--dup-threshold', type=float, default=0.005,
                        help='mean absolute difference under which two frames '
                             'are duplicates (0-1)')
    parser.add_argument('--samples', type=int, default=6)
    args = parser.parse_args()

    root = pathlib.Path(args.data)
    if not root.is_dir():
        print("Data directory not found: %s" % (root,), file=sys.stderr)
        return 1

    report = {'data': str(root), 'duplicate_threshold': args.dup_threshold,
              'classes': {}}
    paths_by_class = {}
    unique_by_class = {}
    for label in CLASSES:
        folder = find_class_dir(root, label)
        if folder is None:
            print("Missing class directory for %s in %s" % (label, root),
                  file=sys.stderr)
            return 1
        paths = image_files(folder)
        if not paths:
            print("No images in %s" % (folder,), file=sys.stderr)
            return 1
        paths_by_class[label] = paths
        unique_paths, unique_fps = dedupe(paths, args.dup_threshold)
        unique_by_class[label] = unique_fps
        sizes = set()
        brightness = []
        for path in paths:
            with Image.open(path) as image:
                sizes.add(image.size)
                brightness.append(float(np.asarray(
                    image.convert('L'), dtype=np.float32).mean() / 255.0))
        report['classes'][label] = {
            'folder': str(folder),
            'files': len(paths),
            'unique': len(unique_paths),
            'duplicate_fraction': round(
                1. - len(unique_paths) / float(len(paths)), 4),
            'sizes': sorted([list(size) for size in sizes]),
            'brightness_mean': round(float(np.mean(brightness)), 4),
            'brightness_std': round(float(np.std(brightness)), 4),
            'consecutive_diff': consecutive_stats(paths),
        }

    if all(unique_by_class.get(label) for label in CLASSES):
        clear_mean = np.mean(unique_by_class['clear'], axis=0)
        not_clear_mean = np.mean(unique_by_class['not_clear'], axis=0)
        report['class_mean_difference'] = round(
            float(np.mean(np.abs(clear_mean - not_clear_mean))), 5)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'audit.json', 'w') as f:
        json.dump(report, f, indent=2, sort_keys=True)
    make_contact_sheet(paths_by_class, out_dir / 'contact_sheet.jpg',
                       args.samples)

    print("Bed clear dataset audit")
    print("  data: %s" % (root,))
    print("  duplicate threshold: %.4f" % (args.dup_threshold,))
    for label in CLASSES:
        info = report['classes'][label]
        print("  %-9s %4d files -> %4d unique (%.1f%% duplicates)"
              % (label, info['files'], info['unique'],
                 info['duplicate_fraction'] * 100.))
        if info['consecutive_diff']:
            print("             consecutive diff median %.5f"
                  % (info['consecutive_diff']['median'],))
    if 'class_mean_difference' in report:
        print("  class mean difference: %.5f"
              % (report['class_mean_difference'],))
    print("  wrote %s" % (out_dir / 'audit.json',))
    print("  wrote %s" % (out_dir / 'contact_sheet.jpg',))

    unique_counts = [report['classes'][label]['unique']
                     for label in CLASSES]
    if min(unique_counts) < 20:
        print()
        print("WARNING: fewer than 20 unique images in at least one class.")
        print("Sequential frames are not independent samples. Collect more")
        print("scenes, lighting states and object arrangements before")
        print("trusting model accuracy.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
