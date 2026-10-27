import importlib.util
import io
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'camera_checker.py'
SPEC = importlib.util.spec_from_file_location('camera_checker_service',
                                              MODULE_PATH)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def empty_bed_image():
    image = Image.new('L', (160, 120))
    pixels = image.load()
    for y in range(120):
        for x in range(160):
            pixels[x, y] = 60 + (x * 3 + y * 2) % 90
    return image


def occupied_bed_image():
    image = empty_bed_image()
    pixels = image.load()
    for y in range(40, 80):
        for x in range(50, 100):
            pixels[x, y] = 240
    return image


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is needed for the image test")
class ServiceTest(unittest.TestCase):
    def make_config(self, tmp):
        path = pathlib.Path(tmp) / 'camera.json'
        path.write_text(json.dumps({
            'data_dir': tmp,
            'decode_size': [80, 60],
            'roi': [0., 0., 1., 1.],
            'save_debug': True,
        }))
        return checker.load_config(str(path))

    def test_calibrate_check_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            empty = png_bytes(empty_bed_image())
            occupied = png_bytes(occupied_bed_image())
            service = checker.BedClearChecker(config)
            service.source.capture = lambda: empty
            service.calibrate()

            service.source.capture = lambda: occupied
            result = service.check()
            self.assertFalse(result['clear'])
            self.assertGreater(result['changed_cells'], 0)

            service.source.capture = lambda: empty
            result = service.check()
            self.assertTrue(result['clear'])

            self.assertTrue(pathlib.Path(tmp, 'last_diff.pgm').exists())
            self.assertTrue(pathlib.Path(tmp, 'last_result.json').exists())

            reloaded = checker.BedClearChecker(config)
            self.assertIsNotNone(reloaded.baseline)
            reloaded.source.capture = lambda: empty
            self.assertTrue(reloaded.check()['clear'])

    def test_http_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            empty = png_bytes(empty_bed_image())
            service = checker.BedClearChecker(config)
            service.source.capture = lambda: empty
            checker.CheckerHandler.checker = service
            server = checker.CheckerServer(('127.0.0.1', 0),
                                           checker.CheckerHandler)
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            base = 'http://127.0.0.1:%d' % (server.server_address[1],)
            try:
                with urllib.request.urlopen(base + '/health') as response:
                    self.assertTrue(json.loads(response.read())['ok'])

                request = urllib.request.Request(base + '/calibrate',
                                                 method='POST')
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.loads(response.read())['ok'])

                with urllib.request.urlopen(base + '/check') as response:
                    payload = json.loads(response.read())
                self.assertTrue(payload['clear'])

                with urllib.request.urlopen(base + '/status') as response:
                    status = json.loads(response.read())
                self.assertTrue(status['baseline'])

                request = urllib.request.Request(base + '/reset',
                                                 method='POST')
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.loads(response.read())['ok'])
                try:
                    urllib.request.urlopen(base + '/check')
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
