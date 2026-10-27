import importlib.util
import os
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'bed_clear.py'
SPEC = importlib.util.spec_from_file_location('bed_clear_camera',
                                              MODULE_PATH)
bed_clear = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bed_clear)


class FakeGcmd:
    error = RuntimeError

    def __init__(self, params=None):
        self.params = params or {}
        self.messages = []

    def respond_info(self, message, log=True):
        self.messages.append(message)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=0, minval=None):
        return int(self.params.get(name, default))


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []

    def register_command(self, name, func, **kwargs):
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)


class FakeReactor:
    NOW = 0.

    def monotonic(self):
        return 100.

    def register_timer(self, callback, when):
        self.timer = callback


class FakePrintStats:
    def __init__(self):
        self.state = 'standby'

    def get_status(self, eventtime):
        return {'state': self.state}


class FakePrinter:
    command_error = RuntimeError

    def __init__(self):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.print_stats = None
        self.handlers = {}
        self.events = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == 'gcode':
            return self.gcode
        if name == 'print_stats':
            return self.print_stats
        return default

    def register_event_handler(self, event, callback):
        self.handlers[event] = callback

    def send_event(self, event, *args):
        self.events.append((event, args))


class FakeConfig:
    error = ValueError

    def __init__(self, printer, values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'bed_clear'

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getchoice(self, name, choices, default):
        return self.values.get(name, default)

    def getfloat(self, name, default, above=None, minval=None):
        return float(self.values.get(name, default))

    def getint(self, name, default, minval=None):
        return int(self.values.get(name, default))

    def getboolean(self, name, default):
        return bool(self.values.get(name, default))


CLEAR_RESULT = {
    'clear': True,
    'reason': 'camera found no change on the bed',
    'changed_cells': 2,
    'changed_fraction': 0.0004,
    'largest_component': 2,
    'max_diff': 0.12,
    'mean_diff': 0.03,
    'regions': [],
}

BLOCKED_RESULT = {
    'clear': False,
    'reason': 'camera found a change on the bed',
    'changed_cells': 42,
    'changed_fraction': 0.0088,
    'largest_component': 30,
    'max_diff': 2.4,
    'mean_diff': 0.21,
    'regions': [{'cells': 30, 'box': [0.2, 0.3, 0.5, 0.6]}],
}

MODEL_RESULT = {
    'clear': False,
    'reason': 'model p(not_clear)=0.930 >= threshold 0.500',
    'backend': 'model',
    'probability': 0.93,
    'threshold': 0.5,
}

BOTH_CLEAR = {
    **CLEAR_RESULT,
    'backend': 'both',
    'probability': 0.02,
    'threshold': 0.5,
}


class CameraCheckTest(unittest.TestCase):
    def make(self, tmp, values=None):
        printer = FakePrinter()
        options = {
            'method': 'camera',
            'checker_url': 'http://127.0.0.1:7126',
            'data_file': os.path.join(tmp, 'bed_clear.json'),
        }
        options.update(values or {})
        obj = bed_clear.BedClear(FakeConfig(printer, options))
        obj._handle_ready()
        return obj, printer

    def fake_request(self, obj, response):
        calls = []

        def request(path, method='GET', timeout=None):
            calls.append((path, method))
            if isinstance(response, Exception):
                raise response
            return response

        obj._request = request
        return calls

    def test_check_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, printer = self.make(tmp)
            calls = self.fake_request(obj, CLEAR_RESULT)
            gcmd = FakeGcmd()
            obj.cmd_BED_CLEAR_CHECK(gcmd)
            self.assertEqual(calls, [('/check', 'GET')])
            self.assertEqual(obj.state, 'clear')
            self.assertEqual(obj.metrics['changed_cells'], 2)
            self.assertTrue(obj.get_status(0.)['clear'])
            self.assertTrue(any('CLEAR' in message for message in gcmd.messages))
            self.assertIn('G4 P500', printer.gcode.scripts)

    def test_check_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            self.fake_request(obj, BLOCKED_RESULT)
            with self.assertRaises(RuntimeError):
                obj.cmd_BED_CLEAR_CHECK(FakeGcmd())
            self.assertEqual(obj.state, 'blocked')
            self.assertEqual(len(obj.metrics['regions']), 1)
            self.assertFalse(obj.get_status(0.)['clear'])
            with self.assertRaises(RuntimeError):
                obj._require_start_allowed(FakeGcmd())

    def test_model_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            self.fake_request(obj, MODEL_RESULT)
            with self.assertRaises(RuntimeError):
                obj.cmd_BED_CLEAR_CHECK(FakeGcmd())
            self.assertEqual(obj.state, 'blocked')
            status = obj.get_status(0.)
            self.assertEqual(status['backend'], 'model')
            self.assertAlmostEqual(status['probability'], 0.93)
            self.assertAlmostEqual(status['threshold'], 0.5)
            self.assertIn('p(not_clear)=0.930', obj._summary())

    def test_both_backend_clear_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            self.fake_request(obj, BOTH_CLEAR)
            obj.cmd_BED_CLEAR_CHECK(FakeGcmd())
            self.assertEqual(obj.state, 'clear')
            status = obj.get_status(0.)
            self.assertEqual(status['backend'], 'both')
            self.assertAlmostEqual(status['probability'], 0.02)
            summary = obj._summary()
            self.assertIn('p(not_clear)=0.020', summary)
            self.assertIn('changed cells', summary)

    def test_checker_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            self.fake_request(obj, bed_clear.CheckerError('connection refused'))
            with self.assertRaises(RuntimeError):
                obj.cmd_BED_CLEAR_CHECK(FakeGcmd())
            self.assertEqual(obj.state, 'error')
            self.assertIn('connection refused', obj.reason)
            self.assertFalse(obj.get_status(0.)['clear'])

    def test_calibrate_sets_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            calls = self.fake_request(obj, {
                'ok': True,
                'decode_size': [80, 60],
                'pixels': 4800,
                'baseline_created': 123.,
            })
            obj.cmd_BED_CLEAR_CALIBRATE(FakeGcmd())
            self.assertEqual(calls, [('/calibrate', 'POST')])
            self.assertEqual(obj.state, 'clear')
            self.assertTrue(obj.get_status(0.)['clear'])

    def test_park_gcode_and_settle(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, printer = self.make(tmp, {
                'park_gcode': 'BED_CLEAR_PARK',
                'settle_ms': 250,
            })
            self.fake_request(obj, CLEAR_RESULT)
            obj.cmd_BED_CLEAR_CHECK(FakeGcmd())
            self.assertEqual(printer.gcode.scripts,
                             ['BED_CLEAR_PARK', 'G4 P250'])

    def test_reset_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)
            calls = self.fake_request(obj, {'ok': True})
            obj.cmd_BED_CLEAR_RESET(FakeGcmd({'BASELINE': 1}))
            self.assertEqual(calls, [('/reset', 'POST')])
            self.assertEqual(obj.state, 'unknown')

    def test_auto_check_on_start_allows_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, printer = self.make(tmp, {'auto_check_on_start': True})
            calls = self.fake_request(obj, CLEAR_RESULT)
            gcmd = FakeGcmd()
            obj._require_start_allowed(gcmd)
            self.assertEqual(calls, [('/check', 'GET')])
            self.assertEqual(obj.state, 'clear')
            self.assertIn('G4 P500', printer.gcode.scripts)

    def test_auto_check_on_start_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp, {'auto_check_on_start': True})
            self.fake_request(obj, BLOCKED_RESULT)
            with self.assertRaises(RuntimeError):
                obj._require_start_allowed(FakeGcmd())
            self.assertEqual(obj.state, 'blocked')

    def test_no_auto_check_does_not_call_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp)

            def request(path, method='GET', timeout=None):
                raise AssertionError("checker should not be called")

            obj._request = request
            with self.assertRaises(RuntimeError):
                obj._require_start_allowed(FakeGcmd())

    def test_manual_mode_does_not_call_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp, {'method': 'manual'})

            def request(path, method='GET', timeout=None):
                raise AssertionError("checker should not be called")

            obj._request = request
            obj.cmd_BED_CLEAR_CONFIRM(FakeGcmd())
            self.assertTrue(obj.get_status(0.)['clear'])
            obj.cmd_BED_CLEAR_CHECK(FakeGcmd())


if __name__ == '__main__':
    unittest.main()
