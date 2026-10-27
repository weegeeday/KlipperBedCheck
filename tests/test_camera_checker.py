import importlib.util
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'camera_checker.py'
SPEC = importlib.util.spec_from_file_location('camera_checker', MODULE_PATH)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def gradient(width, height):
    return [(x * 7 + y * 3) % 120 + 30
            for y in range(height) for x in range(width)]


class AnalyseTest(unittest.TestCase):
    def test_identical_is_clear(self):
        base = gradient(20, 20)
        result = checker.analyse(base, list(base), 20, 20, (0., 0., 1., 1.),
                                 0.35, 6, 0.25)
        self.assertTrue(result['clear'])
        self.assertEqual(result['changed_cells'], 0)
        self.assertEqual(result['largest_component'], 0)

    def test_brightness_shift_is_clear(self):
        base = gradient(20, 20)
        current = [value + 40 for value in base]
        result = checker.analyse(base, current, 20, 20, (0., 0., 1., 1.),
                                 0.35, 6, 0.25)
        self.assertTrue(result['clear'])

    def test_contrast_shift_is_clear(self):
        base = gradient(20, 20)
        current = [value * 1.4 for value in base]
        result = checker.analyse(base, current, 20, 20, (0., 0., 1., 1.),
                                 0.35, 6, 0.25)
        self.assertTrue(result['clear'])

    def test_patch_is_blocked(self):
        base = [100] * 400
        current = list(base)
        for y in range(10, 13):
            for x in range(10, 13):
                current[y * 20 + x] = 200
        result = checker.analyse(base, current, 20, 20, (0., 0., 1., 1.),
                                 0.35, 6, 0.25)
        self.assertFalse(result['clear'])
        self.assertEqual(result['changed_cells'], 9)
        self.assertEqual(result['largest_component'], 9)
        self.assertEqual(len(result['regions']), 1)
        box = result['regions'][0]['box']
        self.assertAlmostEqual(box[0], 0.5, places=2)
        self.assertAlmostEqual(box[1], 0.5, places=2)

    def test_small_component_is_ignored(self):
        base = [100] * 400
        current = list(base)
        for y in range(10, 12):
            for x in range(10, 12):
                current[y * 20 + x] = 200
        result = checker.analyse(base, current, 20, 20, (0., 0., 1., 1.),
                                 0.35, 6, 0.25)
        self.assertTrue(result['clear'])
        self.assertEqual(result['largest_component'], 4)

    def test_roi_excludes_change(self):
        base = [100] * 400
        current = list(base)
        for y in range(1, 5):
            for x in range(1, 5):
                current[y * 20 + x] = 220
        result = checker.analyse(base, current, 20, 20, (0.3, 0.3, 1., 1.),
                                 0.35, 6, 0.25)
        self.assertTrue(result['clear'])

    def test_large_fraction_is_blocked(self):
        base = [100] * 400
        current = [200 if (i % 2) else 100 for i in range(400)]
        result = checker.analyse(base, current, 20, 20, (0., 0., 1., 1.),
                                 0.35, 1000, 0.25)
        self.assertFalse(result['clear'])
        self.assertEqual(result['changed_cells'], 400)


class ComponentTest(unittest.TestCase):
    def test_components(self):
        width, height = 6, 4
        mask = [False] * (width * height)
        for x, y in ((0, 0), (1, 0), (3, 2)):
            mask[y * width + x] = True
        components = checker.find_components(mask, width, height)
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0][0], 2)
        self.assertEqual(components[1][0], 1)

    def test_crop_roi(self):
        pixels = list(range(20))
        crop, width, height = checker.crop_roi(pixels, 5, 4,
                                               (0.2, 0.25, 0.8, 0.75))
        self.assertEqual((width, height), (3, 2))
        self.assertEqual(crop, [6, 7, 8, 11, 12, 13])


class ConfigTest(unittest.TestCase):
    def test_defaults_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'camera.cfg'
            path.write_text(
                "[camera]\n"
                "data_dir: %s\n"
                "roi: 0.1, 0.2, 0.9, 0.8\n"
                "min_component_cells: 4\n" % (tmp,))
            config = checker.load_config(str(path))
            self.assertEqual(config['listen_port'], 7126)
            self.assertEqual(config['min_component_cells'], 4)
            self.assertEqual(config['roi'], [0.1, 0.2, 0.9, 0.8])
            self.assertEqual(config['decode_size'], [80, 60])
            self.assertEqual(config['data_dir'], tmp)

    def test_bad_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'camera.cfg'
            path.write_text("[camera]\nroi: 0.5, 0.1, 0.4, 0.9\n")
            with self.assertRaises(checker.ConfigError):
                checker.load_config(str(path))

    def test_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'camera.cfg'
            path.write_text("[camera\nroi: 0.1, 0.1, 0.9, 0.9\n")
            with self.assertRaises(checker.ConfigError):
                checker.load_config(str(path))

    def test_missing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'camera.cfg'
            path.write_text("[other]\nroi: 0.1, 0.1, 0.9, 0.9\n")
            with self.assertRaises(checker.ConfigError):
                checker.load_config(str(path))


if __name__ == '__main__':
    unittest.main()
