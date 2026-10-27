#!/usr/bin/env python3
"""Camera bed occupancy checker for Klipper Bed Clear.

This is a small local HTTP service. It captures a snapshot from the printer
camera, compares it with an empty-bed baseline and answers:

    GET  /check      -> {"clear": true|false, ...}
    POST /calibrate  -> capture a new empty-bed baseline
    GET  /status     -> service and baseline status
    POST /reset      -> delete the baseline
    GET  /health     -> liveness probe

Only the Python standard library is required. Image decoding uses Pillow if it
is installed, otherwise ffmpeg, otherwise ImageMagick.
"""

import argparse
import configparser
import glob
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CONFIG = {
    'snapshot_url': '',
    'snapshot_command': '',
    'moonraker_url': 'http://127.0.0.1:7125',
    'webcam': '',
    'api_key': '',
    'listen_host': '127.0.0.1',
    'listen_port': 7126,
    'data_dir': '~/printer_data/bed_clear',
    'decode_size': [80, 60],
    'roi': [0.0, 0.0, 1.0, 1.0],
    'cell_threshold': 0.35,
    'min_component_cells': 6,
    'max_changed_fraction': 0.25,
    'snapshot_timeout': 10.0,
    'save_debug': True,
    'token': '',
    'backend': 'diff',
    'model_path': '~/printer_data/bed_clear/model.onnx',
    'model_config': '~/printer_data/bed_clear/model_config.json',
    'model_threshold': None,
}

BACKENDS = ('diff', 'model', 'both')


class ConfigError(Exception):
    pass


class SnapshotError(Exception):
    pass


class DecodeError(Exception):
    pass


class NoBaseline(Exception):
    pass


class ModelError(Exception):
    pass


def _expand(path):
    return os.path.expandvars(os.path.expanduser(str(path)))


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in ('1', 'true', 'yes', 'on')


def _parse_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return [part.strip() for part in str(value).split(',') if part.strip()]


def load_config(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path) as f:
            parser.read_file(f)
    except (IOError, OSError) as e:
        raise ConfigError("unable to read %s: %s" % (path, e))
    except (configparser.Error, UnicodeDecodeError) as e:
        raise ConfigError("invalid config in %s: %s" % (path, e))
    section = 'camera'
    if not parser.has_section(section):
        raise ConfigError("%s must contain a [camera] section" % (path,))
    config = dict(DEFAULT_CONFIG)
    config.update(parser.items(section))
    config['data_dir'] = _expand(config.get('data_dir')
                                 or '~/printer_data/bed_clear')
    for key in ('snapshot_url', 'snapshot_command', 'webcam', 'api_key',
                'token'):
        config[key] = str(config.get(key) or '').strip()
    config['moonraker_url'] = str(
        config.get('moonraker_url') or 'http://127.0.0.1:7125').rstrip('/')
    config['listen_host'] = str(
        config.get('listen_host') or '127.0.0.1').strip()
    config['backend'] = str(config.get('backend') or 'diff').strip().lower()
    if config['backend'] not in BACKENDS:
        raise ConfigError("backend must be one of: %s"
                          % (', '.join(BACKENDS),))
    config['model_path'] = _expand(config.get('model_path') or '')
    config['model_config'] = _expand(config.get('model_config') or '')
    model_threshold = config.get('model_threshold')
    if model_threshold in (None, ''):
        config['model_threshold'] = None
    else:
        try:
            config['model_threshold'] = float(model_threshold)
        except (TypeError, ValueError):
            raise ConfigError("model_threshold must be a number")
        if not 0. <= config['model_threshold'] <= 1.:
            raise ConfigError("model_threshold must be between 0 and 1")
    try:
        config['listen_port'] = int(config['listen_port'])
        config['snapshot_timeout'] = float(config['snapshot_timeout'])
        config['cell_threshold'] = float(config['cell_threshold'])
        config['min_component_cells'] = int(config['min_component_cells'])
        config['max_changed_fraction'] = float(
            config['max_changed_fraction'])
        config['decode_size'] = [int(v) for v in
                                 _parse_list(config['decode_size'])]
        config['roi'] = [float(v) for v in _parse_list(config['roi'])]
    except (TypeError, ValueError) as e:
        raise ConfigError("invalid value in %s: %s" % (path, e))
    if (len(config['decode_size']) != 2
            or min(config['decode_size']) < 8
            or max(config['decode_size']) > 1000):
        raise ConfigError(
            "decode_size must be two integers between 8 and 1000")
    roi = config['roi']
    if (len(roi) != 4 or not 0. <= roi[0] < roi[2] <= 1.
            or not 0. <= roi[1] < roi[3] <= 1.):
        raise ConfigError(
            "roi must be [x0, y0, x1, y1] with 0 <= x0 < x1 <= 1 "
            "and 0 <= y0 < y1 <= 1")
    if config['cell_threshold'] <= 0.:
        raise ConfigError("cell_threshold must be greater than 0")
    if config['min_component_cells'] < 1:
        raise ConfigError("min_component_cells must be at least 1")
    if not 0. < config['max_changed_fraction'] <= 1.:
        raise ConfigError("max_changed_fraction must be between 0 and 1")
    if not 1 <= config['listen_port'] <= 65535:
        raise ConfigError("listen_port must be between 1 and 65535")
    config['save_debug'] = _as_bool(config['save_debug'], True)
    return config


def atomic_write(path, data):
    tmp_path = path + '.tmp'
    with open(tmp_path, 'wb') as f:
        f.write(data)
    os.replace(tmp_path, path)


def atomic_json(path, payload):
    atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True)
                        + '\n').encode('utf-8'))


def image_extension(data):
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data.startswith(b'BM'):
        return '.bmp'
    return '.img'


def crop_roi(pixels, width, height, roi):
    x0 = max(0, min(width - 1, int(round(roi[0] * width))))
    y0 = max(0, min(height - 1, int(round(roi[1] * height))))
    x1 = max(x0 + 1, min(width, int(round(roi[2] * width))))
    y1 = max(y0 + 1, min(height, int(round(roi[3] * height))))
    out = []
    for y in range(y0, y1):
        start = y * width + x0
        out.extend(pixels[start:start + (x1 - x0)])
    return out, x1 - x0, y1 - y0


def normalize_pixels(pixels):
    if not pixels:
        raise ValueError("empty image")
    count = float(len(pixels))
    mean = sum(pixels) / count
    variance = sum((p - mean) ** 2 for p in pixels) / count
    scale = max(variance ** 0.5, 8.0)
    return [(p - mean) / scale for p in pixels]


def find_components(mask, width, height):
    """Return [(cell_count, (x0, y0, x1, y1)), ...] sorted largest first."""
    visited = bytearray(len(mask))
    components = []
    for start, changed in enumerate(mask):
        if not changed or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        size = 0
        min_x, min_y = width, height
        max_x = max_y = -1
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            size += 1
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            if x > 0:
                nxt = index - 1
                if mask[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if x + 1 < width:
                nxt = index + 1
                if mask[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y > 0:
                nxt = index - width
                if mask[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y + 1 < height:
                nxt = index + width
                if mask[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
        components.append((size, (min_x, min_y, max_x, max_y)))
    components.sort(key=lambda item: item[0], reverse=True)
    return components


def analyse(base_pixels, current_pixels, width, height, roi,
            cell_threshold, min_component_cells, max_changed_fraction):
    if len(base_pixels) != width * height:
        raise ValueError("baseline size mismatch")
    if len(current_pixels) != width * height:
        raise ValueError("snapshot size mismatch")
    base_crop, crop_w, crop_h = crop_roi(base_pixels, width, height, roi)
    cur_crop, _, _ = crop_roi(current_pixels, width, height, roi)
    base_norm = normalize_pixels(base_crop)
    cur_norm = normalize_pixels(cur_crop)
    diffs = [abs(a - b) for a, b in zip(base_norm, cur_norm)]
    mask = [d > cell_threshold for d in diffs]
    changed_cells = sum(mask)
    total_cells = max(1, len(mask))
    changed_fraction = changed_cells / float(total_cells)
    components = find_components(mask, crop_w, crop_h)
    largest = components[0][0] if components else 0
    regions = []
    for size, (x0, y0, x1, y1) in components[:3]:
        regions.append({
            'cells': size,
            'box': [round(x0 / float(crop_w), 3),
                    round(y0 / float(crop_h), 3),
                    round((x1 + 1) / float(crop_w), 3),
                    round((y1 + 1) / float(crop_h), 3)],
        })
    blocked = (largest >= min_component_cells
               or changed_fraction >= max_changed_fraction)
    if blocked:
        reason = ("camera found a change on the bed (%d changed cells, "
                  "largest region %d cells)"
                  % (changed_cells, largest))
    else:
        reason = "camera found no change on the bed"
    return {
        'clear': not blocked,
        'reason': reason,
        'changed_cells': changed_cells,
        'changed_fraction': round(changed_fraction, 4),
        'largest_component': largest,
        'max_diff': round(max(diffs), 4),
        'mean_diff': round(sum(diffs) / float(total_cells), 4),
        'regions': regions,
        '_diff': diffs,
        '_diff_width': crop_w,
        '_diff_height': crop_h,
    }


class ImageDecoder:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self._order = ['pil', 'ffmpeg', 'magick']

    def decode(self, data):
        errors = []
        for name in list(self._order):
            try:
                pixels = getattr(self, '_decode_' + name)(data)
            except DecodeError as e:
                errors.append('%s: %s' % (name, e))
                continue
            self._order = [name] + [n for n in self._order if n != name]
            return pixels
        raise DecodeError(
            "unable to decode camera snapshot (%s); install python3-pil or "
            "ffmpeg" % ('; '.join(errors),))

    def _decode_pil(self, data):
        try:
            from PIL import Image
        except ImportError:
            raise DecodeError("Pillow is not installed")
        try:
            image = Image.open(io.BytesIO(data)).convert('L')
            image = image.resize((self.width, self.height), Image.BILINEAR)
            return list(image.tobytes())
        except Exception as e:
            raise DecodeError("Pillow could not decode the image: %s" % (e,))

    def _decode_ffmpeg(self, data):
        binary = shutil.which('ffmpeg')
        if not binary:
            raise DecodeError("ffmpeg is not installed")
        command = [binary, '-hide_banner', '-loglevel', 'error',
                   '-i', 'pipe:0',
                   '-vf', 'scale=%d:%d' % (self.width, self.height),
                   '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
        try:
            proc = subprocess.run(command, input=data,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=20)
        except subprocess.TimeoutExpired:
            raise DecodeError("ffmpeg timed out")
        if proc.returncode != 0:
            detail = proc.stderr.decode('utf-8', 'replace').strip()[:200]
            raise DecodeError("ffmpeg failed: %s" % (detail,))
        expected = self.width * self.height
        if len(proc.stdout) != expected:
            raise DecodeError("ffmpeg returned %d bytes, expected %d"
                              % (len(proc.stdout), expected))
        return list(proc.stdout)

    def _decode_magick(self, data):
        binary = shutil.which('convert')
        if not binary:
            raise DecodeError("ImageMagick convert is not installed")
        command = [binary, '-', '-resize', '%dx%d!' % (self.width,
                                                       self.height),
                   '-colorspace', 'Gray', '-depth', '8', 'gray:-']
        try:
            proc = subprocess.run(command, input=data,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=20)
        except subprocess.TimeoutExpired:
            raise DecodeError("ImageMagick timed out")
        if proc.returncode != 0:
            detail = proc.stderr.decode('utf-8', 'replace').strip()[:200]
            raise DecodeError("ImageMagick failed: %s" % (detail,))
        expected = self.width * self.height
        if len(proc.stdout) != expected:
            raise DecodeError("ImageMagick returned %d bytes, expected %d"
                              % (len(proc.stdout), expected))
        return list(proc.stdout)


class SnapshotSource:
    def __init__(self, config):
        self.config = config
        self._resolved_url = None

    def _headers(self):
        headers = {}
        if self.config['api_key']:
            headers['X-Api-Key'] = self.config['api_key']
        return headers

    def _moonraker_json(self, path):
        url = self.config['moonraker_url'] + path
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(
                request, timeout=self.config['snapshot_timeout']) as response:
            return json.loads(response.read().decode('utf-8'))

    def resolve_url(self):
        if self._resolved_url:
            return self._resolved_url
        url = self.config['snapshot_url']
        if not url:
            try:
                payload = self._moonraker_json('/server/webcams/list')
            except Exception as e:
                raise SnapshotError(
                    "snapshot_url is empty and Moonraker webcam discovery "
                    "failed: %s" % (e,))
            result = payload.get('result', {})
            webcams = result.get('webcams', [])
            wanted = self.config['webcam']
            match = None
            for webcam in webcams:
                if not wanted or webcam.get('name') == wanted:
                    match = webcam
                    break
            if match is None:
                if wanted:
                    message = "no webcam named '%s' found in Moonraker" % (
                        wanted,)
                else:
                    message = "no webcams found in Moonraker"
                raise SnapshotError(message)
            url = match.get('snapshot_url') or ''
            if not url:
                raise SnapshotError("the Moonraker webcam has no snapshot_url")
            if url.startswith('/'):
                # Moonraker returns webcam paths relative to the web frontend
                # (nginx), not to its API port. Drop the API port.
                parts = urllib.parse.urlsplit(self.config['moonraker_url'])
                host = parts.hostname or '127.0.0.1'
                if ':' in host:
                    host = '[%s]' % (host,)
                url = '%s://%s%s' % (parts.scheme or 'http', host, url)
        self._resolved_url = url
        return url

    def capture(self):
        timeout = self.config['snapshot_timeout']
        command = self.config['snapshot_command']
        if command:
            try:
                proc = subprocess.run(command, shell=True,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise SnapshotError("snapshot_command timed out")
            if proc.returncode != 0:
                detail = proc.stderr.decode('utf-8', 'replace').strip()[:200]
                raise SnapshotError("snapshot_command failed: %s" % (detail,))
            data = proc.stdout
        else:
            url = self.resolve_url()
            request = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(
                        request, timeout=timeout) as response:
                    data = response.read()
            except urllib.error.HTTPError as e:
                raise SnapshotError("camera returned HTTP %s from %s"
                                    % (e.code, url))
            except (urllib.error.URLError, OSError) as e:
                raise SnapshotError("unable to fetch snapshot %s: %s"
                                    % (url, e))
        if not data:
            raise SnapshotError("camera returned an empty snapshot")
        return data


def crop_roi_image(image, roi):
    width, height = image.size
    x0 = max(0, min(width - 1, int(round(roi[0] * width))))
    y0 = max(0, min(height - 1, int(round(roi[1] * height))))
    x1 = max(x0 + 1, min(width, int(round(roi[2] * width))))
    y1 = max(y0 + 1, min(height, int(round(roi[3] * height))))
    return image.crop((x0, y0, x1, y1))


class ModelChecker:
    def __init__(self, config):
        self.config = config
        self.path = config.get('model_path') or ''
        self.config_path = config.get('model_config') or ''
        self.session = None
        self.input_name = None
        self.classes = []
        self.not_clear_index = 1
        self.threshold = 0.5
        self.threshold_override = config.get('model_threshold')
        self.roi = tuple(config['roi'])
        self.size = tuple(config['decode_size'])
        self.mean = 0.5
        self.std = 0.5
        self.error = None
        self._load()

    def _load(self):
        try:
            if not self.path or not os.path.isfile(self.path):
                raise ModelError("model file not found: %s"
                                 % (self.path or '(unset)',))
            if not self.config_path or not os.path.isfile(self.config_path):
                raise ModelError("model config not found: %s"
                                 % (self.config_path or '(unset)',))
            try:
                import numpy  # noqa: F401
                from PIL import Image  # noqa: F401
            except ImportError as e:
                raise ModelError("Pillow and numpy are required for the model "
                                 "backend: %s" % (e,))
            with open(self.config_path) as f:
                meta = json.load(f)
            classes = meta.get('classes') or []
            if len(classes) != 2 or 'not_clear' not in classes:
                raise ModelError("model config must contain two classes "
                                 "including not_clear")
            self.classes = [str(name) for name in classes]
            self.not_clear_index = self.classes.index('not_clear')
            image_meta = meta.get('image') or {}
            roi = image_meta.get('roi')
            if roi:
                if (len(roi) != 4
                        or not 0. <= roi[0] < roi[2] <= 1.
                        or not 0. <= roi[1] < roi[3] <= 1.):
                    raise ModelError("invalid roi in model config")
                self.roi = tuple(float(value) for value in roi)
            size = image_meta.get('size')
            if size:
                if len(size) != 2 or min(size) < 1:
                    raise ModelError("invalid image size in model config")
                self.size = (int(size[0]), int(size[1]))
            if int(image_meta.get('channels', 1)) != 1:
                raise ModelError("only single-channel grayscale models are "
                                 "supported")
            mean = image_meta.get('normalize_mean', [0.5])
            std = image_meta.get('normalize_std', [0.5])
            if not isinstance(mean, (list, tuple)) or len(mean) != 1:
                raise ModelError("model normalization must be grayscale")
            if not isinstance(std, (list, tuple)) or len(std) != 1:
                raise ModelError("model normalization must be grayscale")
            self.mean = float(mean[0])
            self.std = float(std[0])
            if self.std == 0.:
                raise ModelError("model normalization std must not be zero")
            threshold = self.threshold_override
            if threshold is None:
                threshold = meta.get('threshold', 0.5)
            self.threshold = float(threshold)
            if not 0. <= self.threshold <= 1.:
                raise ModelError("model threshold must be between 0 and 1")
            if self.threshold > 0.9:
                logging.warning(
                    "model threshold %.3f is high; this can miss not-clear "
                    "frames. Consider model_threshold: 0.5", self.threshold)
            import onnxruntime as ort
            self.session = ort.InferenceSession(
                self.path, providers=['CPUExecutionProvider'])
            inputs = self.session.get_inputs()
            if not inputs:
                raise ModelError("ONNX model has no inputs")
            self.input_name = inputs[0].name
            logging.info("loaded model %s (threshold %.3f, input %dx%d)",
                         self.path, self.threshold,
                         self.size[0], self.size[1])
        except Exception as e:
            self.session = None
            self.error = str(e)
            logging.warning("model backend unavailable: %s", e)

    def status(self):
        return {
            'loaded': self.session is not None,
            'error': self.error,
            'path': self.path,
            'config': self.config_path,
            'threshold': self.threshold,
            'roi': list(self.roi),
            'size': list(self.size),
            'classes': self.classes,
        }

    def _tensor(self, data):
        try:
            from PIL import Image
            import numpy as np
        except ImportError as e:
            raise ModelError("Pillow and numpy are required for the model "
                             "backend: %s" % (e,))
        try:
            image = Image.open(io.BytesIO(data)).convert('L')
        except Exception as e:
            raise ModelError("could not decode snapshot for model: %s" % (e,))
        image = crop_roi_image(image, self.roi)
        image = image.resize(self.size, Image.BILINEAR)
        array = np.asarray(image, dtype=np.float32) / 255.
        array = (array - self.mean) / self.std
        return array[None, None, :, :]

    def check(self, data):
        if self.session is None:
            raise ModelError(self.error or "model is not loaded")
        try:
            import numpy as np
            tensor = self._tensor(data)
            outputs = self.session.run(None, {self.input_name: tensor})
            logits = np.asarray(outputs[0], dtype=np.float64)
            if logits.ndim != 2 or logits.shape[1] != 2:
                raise ModelError("ONNX model must return two logits; got %s"
                                 % (logits.shape,))
            values = logits[0]
            values = np.exp(values - np.max(values))
            probabilities = values / values.sum()
            probability = float(probabilities[self.not_clear_index])
        except ModelError:
            raise
        except Exception as e:
            raise ModelError("model inference failed: %s" % (e,))
        threshold = self.threshold
        clear = probability < threshold
        reason = ("model p(not_clear)=%.3f %s threshold %.3f"
                  % (probability, '<' if clear else '>=', threshold))
        return {
            'clear': clear,
            'reason': reason,
            'backend': 'model',
            'probability': probability,
            'threshold': threshold,
            'model': {'path': self.path, 'classes': self.classes},
        }


class BedClearChecker:
    def __init__(self, config):
        self.config = config
        self.source = SnapshotSource(config)
        self.decoder = ImageDecoder(*config['decode_size'])
        self.backend = config['backend']
        self.lock = threading.Lock()
        self.baseline = None
        self.last = None
        self.model = None
        os.makedirs(config['data_dir'], exist_ok=True)
        if self.backend in ('model', 'both'):
            self.model = ModelChecker(config)
        self._load_baseline()

    def _data_path(self, name):
        return os.path.join(self.config['data_dir'], name)

    def _baseline_images(self):
        return [path for path in glob.glob(self._data_path('baseline.*'))
                if not path.endswith('.json')]

    def _load_baseline(self):
        candidates = self._baseline_images()
        if not candidates:
            return
        path = max(candidates, key=os.path.getmtime)
        try:
            with open(path, 'rb') as f:
                data = f.read()
            pixels = self.decoder.decode(data)
            created = os.path.getmtime(path)
            meta_path = self._data_path('baseline_meta.json')
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                created = float(meta.get('created', created))
            self.baseline = {'pixels': pixels, 'created': created,
                             'path': path}
            logging.info("loaded baseline from %s", path)
        except Exception as e:
            logging.warning("could not load baseline %s: %s", path, e)

    def _save_baseline(self, data, created):
        extension = image_extension(data)
        path = self._data_path('baseline' + extension)
        atomic_write(path, data)
        for old_path in self._baseline_images():
            if old_path != path:
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        meta = {
            'created': created,
            'format': extension,
            'decode_size': self.config['decode_size'],
            'roi': self.config['roi'],
            'source': ('command' if self.config['snapshot_command']
                       else 'url'),
        }
        atomic_json(self._data_path('baseline_meta.json'), meta)
        return path

    def calibrate(self):
        with self.lock:
            data = self.source.capture()
            pixels = self.decoder.decode(data)
            created = time.time()
            path = self._save_baseline(data, created)
            self.baseline = {'pixels': pixels, 'created': created,
                             'path': path}
            self.last = None
            logging.info("captured new baseline (%d pixels)", len(pixels))
            return {
                'ok': True,
                'baseline_created': created,
                'decode_size': list(self.config['decode_size']),
                'pixels': len(pixels),
            }

    def _run_diff(self, data):
        roi = (self.model.roi
               if self.backend == 'both' and self.model is not None
               else self.config['roi'])
        current = self.decoder.decode(data)
        result = analyse(
            self.baseline['pixels'], current,
            self.config['decode_size'][0], self.config['decode_size'][1],
            roi,
            self.config['cell_threshold'],
            self.config['min_component_cells'],
            self.config['max_changed_fraction'])
        diff = result.pop('_diff')
        diff_width = result.pop('_diff_width')
        diff_height = result.pop('_diff_height')
        result['backend'] = 'diff'
        return result, diff, diff_width, diff_height

    def _run_model(self, data):
        if self.model is None:
            raise ModelError("model backend is not configured")
        return self.model.check(data)

    @staticmethod
    def _combine(diff_result, model_result):
        result = dict(diff_result)
        result['backend'] = 'both'
        result['model_clear'] = model_result['clear']
        result['model_reason'] = model_result['reason']
        result['probability'] = model_result.get('probability')
        result['threshold'] = model_result.get('threshold')
        result['clear'] = bool(diff_result['clear'] and model_result['clear'])
        if result['clear']:
            result['reason'] = 'diff and model both report clear'
        else:
            reasons = []
            if not diff_result['clear']:
                reasons.append('diff: %s' % (diff_result['reason'],))
            if not model_result['clear']:
                reasons.append('model: %s' % (model_result['reason'],))
            result['reason'] = '; '.join(reasons)
        return result

    def check(self):
        with self.lock:
            if self.backend == 'model':
                data = self.source.capture()
                result = self._run_model(data)
                result['timestamp'] = time.time()
                if self.config['save_debug']:
                    self._save_model_debug(data, result)
                self.last = result
                logging.info("check[model]: clear=%s p=%.3f threshold=%.3f",
                             result['clear'], result['probability'],
                             result['threshold'])
                return result
            if self.baseline is None:
                raise NoBaseline("no camera baseline; run BED_CLEAR_CALIBRATE")
            data = self.source.capture()
            diff_result, diff, diff_width, diff_height = self._run_diff(data)
            if self.backend == 'both':
                model_result = self._run_model(data)
                result = self._combine(diff_result, model_result)
            else:
                result = diff_result
            result['timestamp'] = time.time()
            result['baseline_created'] = self.baseline['created']
            if self.config['save_debug']:
                self._save_debug(data, result, diff, diff_width, diff_height)
            self.last = result
            if 'probability' in result:
                logging.info(
                    "check[%s]: clear=%s p=%.3f changed=%.2f%%",
                    self.backend, result['clear'], result['probability'],
                    result['changed_fraction'] * 100.)
            else:
                logging.info(
                    "check[%s]: clear=%s changed=%.2f%% largest=%d "
                    "max_diff=%.2f", self.backend, result['clear'],
                    result['changed_fraction'] * 100.,
                    result['largest_component'], result['max_diff'])
            return result

    def _save_model_roi(self, data):
        if self.model is None:
            return
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(data)).convert('L')
            crop = crop_roi_image(image, self.model.roi)
            crop.save(self._data_path('last_model_roi.png'))
        except Exception as e:
            logging.warning("could not save model ROI image: %s", e)

    def _save_debug(self, data, result, diff, width, height):
        try:
            extension = image_extension(data)
            atomic_write(self._data_path('last_snapshot' + extension), data)
            atomic_json(self._data_path('last_result.json'), result)
            header = ('P5\n%d %d\n255\n' % (width, height)).encode('ascii')
            body = bytes(max(0, min(255, int(round(value * 255.))))
                         for value in diff)
            atomic_write(self._data_path('last_diff.pgm'), header + body)
            self._save_model_roi(data)
        except (IOError, OSError) as e:
            logging.warning("could not save debug images: %s", e)

    def _save_model_debug(self, data, result):
        try:
            extension = image_extension(data)
            atomic_write(self._data_path('last_snapshot' + extension), data)
            atomic_json(self._data_path('last_result.json'), result)
            stale_diff = self._data_path('last_diff.pgm')
            if os.path.exists(stale_diff):
                os.remove(stale_diff)
            self._save_model_roi(data)
        except (IOError, OSError) as e:
            logging.warning("could not save model debug images: %s", e)

    def status(self):
        with self.lock:
            return {
                'ok': True,
                'backend': self.backend,
                'baseline': self.baseline is not None,
                'baseline_created': (self.baseline['created']
                                     if self.baseline else 0.),
                'decode_size': list(self.config['decode_size']),
                'roi': list(self.config['roi']),
                'cell_threshold': self.config['cell_threshold'],
                'min_component_cells': self.config['min_component_cells'],
                'max_changed_fraction': self.config['max_changed_fraction'],
                'source': ('command' if self.config['snapshot_command']
                           else 'url'),
                'model': (self.model.status()
                          if self.model is not None else None),
                'last': self.last,
            }

    def reset(self):
        with self.lock:
            for path in self._baseline_images():
                try:
                    os.remove(path)
                except OSError:
                    pass
            meta_path = self._data_path('baseline_meta.json')
            if os.path.exists(meta_path):
                try:
                    os.remove(meta_path)
                except OSError:
                    pass
            self.baseline = None
            self.last = None
            logging.info("baseline deleted")
            return {'ok': True}


class CheckerHandler(BaseHTTPRequestHandler):
    checker = None
    server_version = 'BedClearCamera/1.0'

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.checker.config['token']
        if not token:
            return True
        return self.headers.get('X-Bed-Clear-Token') == token

    def _handle(self, method):
        if not self._authorized():
            self._send_json(401, {'error': 'unauthorized'})
            return
        path = urllib.parse.urlparse(self.path).path
        if path != '/':
            path = path.rstrip('/')
        if method == 'GET' and path == '/health':
            self._send_json(200, {'ok': True})
            return
        if method == 'GET' and path == '/status':
            self._send_json(200, self.checker.status())
            return
        if method == 'GET' and path == '/check':
            try:
                result = self.checker.check()
            except NoBaseline as e:
                self._send_json(409, {'error': str(e),
                                      'code': 'no_baseline'})
            except (SnapshotError, DecodeError, ModelError) as e:
                self._send_json(502, {'error': str(e)})
            except Exception as e:
                logging.exception("check failed")
                self._send_json(500, {'error': str(e)})
            else:
                self._send_json(200, result)
            return
        if method == 'POST' and path == '/calibrate':
            try:
                result = self.checker.calibrate()
            except (SnapshotError, DecodeError) as e:
                self._send_json(502, {'error': str(e)})
            except Exception as e:
                logging.exception("calibration failed")
                self._send_json(500, {'error': str(e)})
            else:
                self._send_json(200, result)
            return
        if method == 'POST' and path == '/reset':
            self._send_json(200, self.checker.reset())
            return
        if path in ('/health', '/status', '/check', '/calibrate', '/reset'):
            self._send_json(405, {'error': 'method not allowed'})
        else:
            self._send_json(404, {'error': 'not found'})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def log_message(self, fmt, *args):
        logging.debug("%s - %s", self.address_string(), fmt % args)


class CheckerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(
        description="Camera bed occupancy checker for Klipper Bed Clear")
    parser.add_argument(
        '--config',
        default=os.path.expanduser(
            '~/printer_data/config/bed_clear_camera.cfg'),
        help="path to the camera checker config file")
    parser.add_argument('--verbose', action='store_true',
                        help="enable debug logging")
    parser.add_argument('--check-config', action='store_true',
                        help="validate the config and model, then exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = load_config(args.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 1
    try:
        checker = BedClearChecker(config)
    except OSError as e:
        logging.error("unable to prepare data directory: %s", e)
        return 1
    if args.check_config:
        status = checker.status()
        print(json.dumps(status, indent=2, sort_keys=True))
        model = status.get('model') or {}
        if (status.get('backend') in ('model', 'both')
                and not model.get('loaded')):
            return 1
        return 0
    CheckerHandler.checker = checker
    address = (config['listen_host'], config['listen_port'])
    try:
        server = CheckerServer(address, CheckerHandler)
    except OSError as e:
        logging.error("unable to listen on %s:%s: %s",
                      address[0], address[1], e)
        return 1
    logging.info("bed_clear camera checker listening on http://%s:%d",
                 address[0], address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
