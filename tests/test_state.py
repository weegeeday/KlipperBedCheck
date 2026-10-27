import importlib.util
import os
import pathlib
import tempfile
import time
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'bed_clear.py'
SPEC = importlib.util.spec_from_file_location('bed_clear_state', MODULE_PATH)
bed_clear = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bed_clear)


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


class FakeGcmd:
    error = RuntimeError


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

    def __init__(self, printer, values=None):
        self.printer = printer
        self.values = values or {}

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

    def getlists(self, name, default=None, **kwargs):
        return self.values.get(name, default)

    def getsection(self, name):
        raise KeyError(name)


class StateTest(unittest.TestCase):
    def make(self, tmp, values=None, print_stats=None):
        printer = FakePrinter()
        printer.print_stats = print_stats
        config = FakeConfig(printer, values)
        config.values.setdefault(
            'data_file', os.path.join(tmp, 'bed_clear.json'))
        obj = bed_clear.BedClear(config)
        obj._handle_ready()
        return obj, printer

    def test_unknown_blocks_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp, {'method': 'manual'})
            self.assertEqual(obj.state, 'unknown')
            self.assertFalse(obj.get_status(0.)['clear'])
            with self.assertRaises(RuntimeError):
                obj._require_start_allowed(FakeGcmd())

    def test_confirm_clears_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, printer = self.make(tmp, {'method': 'manual'})
            obj._set_state(bed_clear.STATE_CLEAR, 'cleared',
                           method='manual', checked=True)
            self.assertTrue(obj.get_status(0.)['clear'])
            self.assertTrue(any(e[0] == 'bed_clear:changed'
                                for e in printer.events))

            obj2, _ = self.make(tmp, {'method': 'manual'})
            self.assertEqual(obj2.state, 'clear')
            self.assertTrue(obj2.get_status(0.)['clear'])

    def test_max_age_expires_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, _ = self.make(tmp, {'method': 'manual', 'max_age': 60})
            obj._set_state(bed_clear.STATE_CLEAR, 'cleared',
                           method='manual', checked=True)
            obj.last_check_at = time.time() - 120
            clear, reason = obj._verified_clear()
            self.assertFalse(clear)
            self.assertIn('expired', reason)

    def test_print_active_hides_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = FakePrintStats()
            obj, _ = self.make(tmp, {'method': 'manual'}, stats)
            obj._set_state(bed_clear.STATE_CLEAR, 'cleared',
                           method='manual', checked=True)
            stats.state = 'paused'
            self.assertFalse(obj.get_status(0.)['clear'])
            self.assertTrue(obj._verified_clear()[0])

    def test_print_cycle_marks_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = FakePrintStats()
            obj, _ = self.make(tmp, {'method': 'manual'}, stats)
            obj._set_state(bed_clear.STATE_CLEAR, 'cleared',
                           method='manual', checked=True)
            stats.state = 'printing'
            obj._poll_print_state(0.)
            self.assertTrue(obj.print_in_progress)
            self.assertTrue(obj.get_status(0.)['clear'] is False)
            stats.state = 'complete'
            obj._poll_print_state(0.)
            self.assertFalse(obj.print_in_progress)
            self.assertEqual(obj.state, 'dirty')

    def test_interrupted_print_becomes_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = FakePrintStats()
            obj, _ = self.make(tmp, {'method': 'manual'}, stats)
            obj._set_state(bed_clear.STATE_CLEAR, 'cleared',
                           method='manual', checked=True)
            obj.print_in_progress = True
            obj._save_data()

            obj2, _ = self.make(tmp, {'method': 'manual'})
            self.assertEqual(obj2.state, 'dirty')
            self.assertFalse(obj2.print_in_progress)
            self.assertIn('restarted', obj2.reason)


if __name__ == '__main__':
    unittest.main()
