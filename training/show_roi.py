#!/usr/bin/env python3
"""Show a normalised ROI over a random sample of bed frames.

Example:
    python training/show_roi.py --roi 0.03,0.52,0.85,0.99 --count 6

The window shows the full frame with the ROI outlined in red, plus a small
inset of the cropped ROI that the model actually sees.
"""

import argparse
import os
import pathlib
import random
import sys

import matplotlib

HEADLESS = (sys.platform.startswith('linux')
            and not os.environ.get('DISPLAY'))


def parse_roi(value):
    try:
        parts = [float(v.strip()) for v in value.split(',')]
    except ValueError:
        raise argparse.ArgumentTypeError("roi must be four numbers")
    if (len(parts) != 4 or not 0. <= parts[0] < parts[2] <= 1.
            or not 0. <= parts[1] < parts[3] <= 1.):
        raise argparse.ArgumentTypeError(
            "roi must be x0,y0,x1,y1 with 0 <= x0 < x1 <= 1 "
            "and 0 <= y0 < y1 <= 1")
    return tuple(parts)


IMG_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
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


def pick_images(data_dir, labels, count, rng):
    pools = {}
    for label in labels:
        folder = find_class_dir(data_dir, label)
        if folder is None:
            print("Missing %s folder in %s" % (label, data_dir),
                  file=sys.stderr)
            return []
        files = image_files(folder)
        if not files:
            print("No images in %s" % (folder,), file=sys.stderr)
            return []
        pools[label] = list(files)

    selected = []
    while len(selected) < count and any(pools.values()):
        for label in labels:
            if not pools[label]:
                continue
            path = rng.choice(pools[label])
            pools[label].remove(path)
            selected.append((label, path))
            if len(selected) >= count:
                break
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--roi', type=parse_roi, required=True,
                        help='normalised x0,y0,x1,y1')
    parser.add_argument('--data', default='data/frames',
                        help='folder containing clear/ and not_clear/')
    parser.add_argument('--count', type=int, default=6)
    parser.add_argument('--label', choices=['both', 'clear', 'not_clear'],
                        default='both')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--save', default=None,
                        help='save the contact sheet to this path')
    parser.add_argument('--no-show', action='store_true',
                        help='do not open a window')
    args = parser.parse_args()

    rng = random.Random(args.seed)
    labels = (['clear', 'not_clear'] if args.label == 'both'
              else [args.label])
    selected = pick_images(args.data, labels, args.count, rng)
    if not selected:
        return 1

    if HEADLESS or args.no_show:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from PIL import Image

    columns = 2 if len(selected) <= 2 else 3
    rows = (len(selected) + columns - 1) // columns
    figure, axes = plt.subplots(rows, columns,
                                figsize=(5.2 * columns, 3.4 * rows))
    axes = axes.ravel() if hasattr(axes, 'ravel') else [axes]
    for axis in axes:
        axis.axis('off')

    for axis, (label, path) in zip(axes, selected):
        with Image.open(path) as image:
            image = image.convert('RGB')
            width, height = image.size
            x0 = int(round(args.roi[0] * width))
            y0 = int(round(args.roi[1] * height))
            x1 = int(round(args.roi[2] * width))
            y1 = int(round(args.roi[3] * height))
            crop = image.crop((x0, y0, x1, y1))
            axis.imshow(image)
            axis.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0,
                                     fill=False, edgecolor='red',
                                     linewidth=2))
            axis.set_title('%s  %s' % (label, path.name), fontsize=9)
            inset = axis.inset_axes([0.70, 0.03, 0.28, 0.28])
            inset.imshow(crop)
            inset.set_xticks([])
            inset.set_yticks([])
            inset.set_title('ROI', fontsize=6, pad=2)
        print('%s  %s' % (label, path))

    figure.suptitle('ROI %s' % (args.roi,), fontsize=11)
    figure.tight_layout()

    save_path = args.save
    if save_path is None and (HEADLESS or args.no_show):
        save_path = 'training/reports/roi_preview.jpg'
    if save_path:
        pathlib.Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(save_path, dpi=120, bbox_inches='tight')
        print('Saved %s' % (save_path,))
    if not (HEADLESS or args.no_show):
        plt.show()
    plt.close(figure)
    return 0


if __name__ == '__main__':
    sys.exit(main())
