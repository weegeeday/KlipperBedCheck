import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest

import numpy as np
from PIL import Image

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'camera_checker.py'
SPEC = importlib.util.spec_from_file_location('camera_checker_model',
                                              MODULE_PATH)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class FakeSession:
    def __init__(self, path, providers=None):
        self.path = path
        self.providers = providers

    def get_inputs(self):
        return [types.SimpleNamespace(name='image')]

    def run(self, output_names, feeds):
        array = feeds['image']
        score = float(np.asarray(array).mean())
        return [np.array([[0.0, 10.0 if score > 0. else -10.0]])]


def image_bytes(value):
    image = Image.new('L', (100, 100), value)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class ModelBackendTest(unittest.TestCase):
    def setUp(self):
        self.original = sys.modules.get('onnxruntime')
        fake = types.ModuleType('onnxruntime')
        fake.InferenceSession = FakeSession
        sys.modules['onnxruntime'] = fake

    def tearDown(self):
        if self.original is None:
            sys.modules.pop('onnxruntime', None)
        else:
            sys.modules['onnxruntime'] = self.original

    def make(self, tmp, threshold=None):
        model_path = pathlib.Path(tmp) / 'model.onnx'
        model_path.write_bytes(b'fake')
        config_path = pathlib.Path(tmp) / 'model_config.json'
        config_path.write_text(json.dumps({
            'classes': ['clear', 'not_clear'],
            'image': {
                'size': [64, 64],
                'roi': [0., 0., 1., 1.],
                'channels': 1,
                'normalize_mean': [0.5],
                'normalize_std': [0.5],
            },
            'threshold': 0.5,
        }))
        return checker.ModelChecker({
            'roi': [0., 0., 1., 1.],
            'decode_size': [80, 60],
            'model_path': str(model_path),
            'model_config': str(config_path),
            'model_threshold': threshold,
        })

    def test_dark_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = self.make(tmp)
            self.assertTrue(model.status()['loaded'])
            result = model.check(image_bytes(20))
            self.assertTrue(result['clear'])
            self.assertLess(result['probability'], 0.5)
            self.assertEqual(result['threshold'], 0.5)

    def test_bright_is_not_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = self.make(tmp)
            result = model.check(image_bytes(220))
            self.assertFalse(result['clear'])
            self.assertGreater(result['probability'], 0.5)
            self.assertEqual(result['backend'], 'model')

    def test_missing_model_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = checker.ModelChecker({
                'roi': [0., 0., 1., 1.],
                'decode_size': [80, 60],
                'model_path': str(pathlib.Path(tmp) / 'nope.onnx'),
                'model_config': str(pathlib.Path(tmp) / 'nope.json'),
                'model_threshold': None,
            })
            self.assertFalse(model.status()['loaded'])
            self.assertIn('not found', model.status()['error'])
            with self.assertRaises(checker.ModelError):
                model.check(image_bytes(20))

    def test_threshold_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = self.make(tmp, threshold=0.99)
            result = model.check(image_bytes(220))
            self.assertEqual(result['threshold'], 0.99)

    def make_bed_checker(self, tmp, backend):
        model_path = pathlib.Path(tmp) / 'model.onnx'
        model_path.write_bytes(b'fake')
        model_config = pathlib.Path(tmp) / 'model_config.json'
        model_config.write_text(json.dumps({
            'classes': ['clear', 'not_clear'],
            'image': {
                'size': [64, 64],
                'roi': [0., 0., 1., 1.],
                'channels': 1,
                'normalize_mean': [0.5],
                'normalize_std': [0.5],
            },
            'threshold': 0.5,
        }))
        cfg_path = pathlib.Path(tmp) / 'camera.cfg'
        cfg_path.write_text(
            "[camera]\n"
            "backend: %s\n"
            "data_dir: %s\n"
            "model_path: %s\n"
            "model_config: %s\n"
            "save_debug: true\n"
            % (backend, tmp, model_path, model_config))
        return checker.BedClearChecker(checker.load_config(str(cfg_path)))

    def test_bed_checker_model_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            bed = self.make_bed_checker(tmp, 'model')
            bed.source.capture = lambda: image_bytes(220)
            result = bed.check()
            self.assertFalse(result['clear'])
            self.assertEqual(result['backend'], 'model')
            self.assertGreater(result['probability'], 0.5)
            status = bed.status()
            self.assertEqual(status['backend'], 'model')
            self.assertTrue(status['model']['loaded'])

    def test_bed_checker_both_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            bed = self.make_bed_checker(tmp, 'both')
            bed.source.capture = lambda: image_bytes(20)
            bed.calibrate()
            clear = bed.check()
            self.assertTrue(clear['clear'])
            self.assertEqual(clear['backend'], 'both')
            bed.source.capture = lambda: image_bytes(220)
            blocked = bed.check()
            self.assertFalse(blocked['clear'])
            self.assertFalse(blocked['model_clear'])


class BackendConfigTest(unittest.TestCase):
    def test_invalid_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'camera.cfg'
            path.write_text("[camera]\nbackend: nope\n")
            with self.assertRaises(checker.ConfigError):
                checker.load_config(str(path))


if __name__ == '__main__':
    unittest.main()
