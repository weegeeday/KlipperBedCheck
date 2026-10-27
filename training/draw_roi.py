#!/usr/bin/env python3
"""Draw a normalised ROI on a bed frame and print the coordinates.

Usage:
    python training/draw_roi.py

Drag a rectangle with the mouse. Keys:
    s  save/print the ROI and close
    n  next image
    r  reset the current selection
    q  quit

The printed values can be copied into train.py --roi and the camera checker
config.
"""

import argparse
import json
import os
import pathlib
import random
import sys

import matplotlib


HEADLESS = (sys.platform.startswith('linux')
            and not os.environ.get('DISPLAY'))

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


def normalized_roi(x0, y0, x1, y1, width, height):
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0 = max(0., min(float(width), x0))
    x1 = max(0., min(float(width), x1))
    y0 = max(0., min(float(height), y0))
    y1 = max(0., min(float(height), y1))
    if x1 - x0 < 2. or y1 - y0 < 2.:
        return None
    return (round(x0 / width, 4), round(y0 / height, 4),
            round(x1 / width, 4), round(y1 / height, 4))


def emit(roi, out_path):
    values = ', '.join('%.4f' % value for value in roi)
    print()
    print('ROI: %s' % (values,))
    print('training: python training/train.py ... --roi %s'
          % (values.replace(' ', ''),))
    print('camera cfg: roi: %s' % (values,))
    if out_path:
        path = pathlib.Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'roi': list(roi)}, f, indent=2)
            f.write('\n')
        print('Wrote %s' % (path,))


class RoiDrawer:
    def __init__(self, images, out_path=None):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import RectangleSelector

        self.plt = plt
        self.RectangleSelector = RectangleSelector
        self.images = images
        self.out_path = out_path
        self.index = -1
        self.roi = None
        self.image = None
        self.selector = None
        self._emitted = False
        self.figure, self.axis = plt.subplots(figsize=(13, 8))
        self.figure.canvas.mpl_connect('key_press_event', self.on_key)
        self.figure.canvas.mpl_connect('close_event', self.on_close)
        print('Drag a rectangle. s=save/print, n=next, r=reset, q=quit')
        self.load_next()
        plt.show()

    def load_next(self):
        self.load(self.index + 1)

    def load(self, index):
        self.index = index % len(self.images)
        path = self.images[self.index]
        from PIL import Image
        self.axis.clear()
        with Image.open(path) as image:
            self.image = image.convert('RGB')
        self.width, self.height = self.image.size
        self.axis.imshow(self.image)
        self.axis.set_title(
            '%s\nDrag a box. s=save, n=next, r=reset, q=quit' % (path.name,),
            fontsize=9)
        self.axis.set_xticks([])
        self.axis.set_yticks([])
        self.roi = None
        self._emitted = False
        if self.selector is not None:
            self.selector.disconnect_events()
        self.selector = self.RectangleSelector(
            self.axis, self.on_select, useblit=False, button=[1],
            minspanx=2, minspany=2, spancoords='pixels', interactive=True,
            props={'facecolor': 'red', 'edgecolor': 'red', 'alpha': 0.18,
                   'fill': True})
        self.figure.canvas.draw_idle()

    def on_select(self, eclick, erelease):
        if (eclick.xdata is None or eclick.ydata is None
                or erelease.xdata is None or erelease.ydata is None):
            return
        roi = normalized_roi(eclick.xdata, eclick.ydata,
                             erelease.xdata, erelease.ydata,
                             self.width, self.height)
        if roi is None:
            return
        self.roi = roi
        self.axis.set_title(
            '%s\nROI %s  (s=save, n=next, r=reset, q=quit)'
            % (pathlib.Path(self.images[self.index]).name,
               ', '.join('%.4f' % value for value in roi)),
            fontsize=9)
        self.figure.canvas.draw_idle()

    def on_key(self, event):
        if event.key in ('q', 'escape'):
            self.plt.close(self.figure)
        elif event.key == 'n':
            self.load_next()
        elif event.key == 'r':
            self.load(self.index)
        elif event.key == 's':
            if self.roi is None:
                print('Draw a rectangle first.')
                return
            self._emitted = True
            emit(self.roi, self.out_path)
            self.plt.close(self.figure)

    def on_close(self, event):
        if self.roi is not None and not self._emitted:
            self._emitted = True
            emit(self.roi, self.out_path)


def pick_images(data_dir, image_path, label):
    if image_path:
        path = pathlib.Path(image_path)
        if not path.is_file():
            print('Image not found: %s' % (path,), file=sys.stderr)
            return []
        return [path]

    labels = (['clear', 'not_clear'] if label == 'both' else [label])
    paths = []
    for name in labels:
        folder = find_class_dir(data_dir, name)
        if folder is None:
            print('Missing %s folder in %s' % (name, data_dir),
                  file=sys.stderr)
            return []
        paths.extend(image_files(folder))
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/frames',
                        help='folder containing clear/ and not_clear/')
    parser.add_argument('--image', default=None,
                        help='specific image to draw on')
    parser.add_argument('--label', choices=['clear', 'not_clear', 'both'],
                        default='clear')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--out', default=None,
                        help='optional JSON output path for {"roi": [...]}')
    args = parser.parse_args()

    if HEADLESS:
        print('No graphical display detected. Run this on a machine with a '
              'desktop, or use X forwarding.', file=sys.stderr)
        return 1

    paths = pick_images(args.data, args.image, args.label)
    if not paths:
        return 1
    random.Random(args.seed).shuffle(paths)

    try:
        import matplotlib.pyplot as plt  # noqa: F401
    except ImportError:
        print('matplotlib is required', file=sys.stderr)
        return 1
    if matplotlib.get_backend().lower() == 'agg':
        print('Matplotlib is using the Agg backend. Set MPLBACKEND=TkAgg or '
              'use a desktop session.', file=sys.stderr)
        return 1

    RoiDrawer(paths, args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
