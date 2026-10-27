# Bed occupancy checking for Klipper
#
# Asks a local camera checker service whether the build plate still looks like
# the empty-bed baseline. The result is exposed as printer.bed_clear and can
# gate a print queue.
#
# A small companion service (camera_checker.py) does the image capture and
# comparison; this module handles Klipper state, commands and queue gating.
#
# This file may be distributed under the terms of the MIT license.

import json
import logging
import os
import time
import urllib.error
import urllib.request
from inspect import Parameter, signature

DATA_VERSION = 1

STATE_UNKNOWN = 'unknown'
STATE_CLEAR = 'clear'
STATE_DIRTY = 'dirty'
STATE_BLOCKED = 'blocked'
STATE_ERROR = 'error'
ALL_STATES = (STATE_UNKNOWN, STATE_CLEAR, STATE_DIRTY, STATE_BLOCKED,
              STATE_ERROR)

PRINT_ACTIVE_STATES = ('printing', 'paused')


class CheckerError(Exception):
    pass


class BedClear:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()

        self.method = config.getchoice('method', ['camera', 'manual'],
                                       'camera')
        self.checker_url = str(
            config.get('checker_url', 'http://127.0.0.1:7126')).rstrip('/')
        if not (self.checker_url.startswith('http://')
                or self.checker_url.startswith('https://')):
            raise config.error(
                "bed_clear: checker_url must start with http:// or https://")
        self.checker_token = str(config.get('token', ''))
        self.check_timeout = config.getfloat('check_timeout', 20., above=0.)
        self.park_gcode = str(config.get('park_gcode', '')).strip()
        self.settle_ms = config.getint('settle_ms', 500, minval=0)
        self.max_age = config.getfloat('max_age', 0., minval=0.)
        self.block_print_start = config.getboolean('block_print_start', False)
        self.auto_check_on_start = config.getboolean('auto_check_on_start',
                                                     False)
        self.data_file = os.path.expandvars(os.path.expanduser(
            config.get('data_file', '~/printer_data/config/bed_clear.json')))

        # The checker is local; do not send it through an http_proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

        self.state = STATE_UNKNOWN
        self.reason = "no bed clear check has been run"
        self.last_check_at = 0.
        self.changed_at = 0.
        self.checked_method = ''
        self.metrics = {}
        self.print_in_progress = False
        self.print_stats = None
        self._orig_m24 = None
        self._orig_sdcard_print_file = None

        gcode = self.gcode
        gcode.register_command('BED_CLEAR_CHECK', self.cmd_BED_CLEAR_CHECK,
                               desc=self.cmd_BED_CLEAR_CHECK_help)
        gcode.register_command('BED_CLEAR_CALIBRATE',
                               self.cmd_BED_CLEAR_CALIBRATE,
                               desc=self.cmd_BED_CLEAR_CALIBRATE_help)
        gcode.register_command('BED_CLEAR_CONFIRM', self.cmd_BED_CLEAR_CONFIRM,
                               desc=self.cmd_BED_CLEAR_CONFIRM_help)
        gcode.register_command('BED_CLEAR_MARK_DIRTY',
                               self.cmd_BED_CLEAR_MARK_DIRTY,
                               desc=self.cmd_BED_CLEAR_MARK_DIRTY_help)
        gcode.register_command('BED_CLEAR_GATE', self.cmd_BED_CLEAR_GATE,
                               desc=self.cmd_BED_CLEAR_GATE_help)
        gcode.register_command('BED_CLEAR_STATUS', self.cmd_BED_CLEAR_STATUS,
                               desc=self.cmd_BED_CLEAR_STATUS_help)
        gcode.register_command('BED_CLEAR_RESET', self.cmd_BED_CLEAR_RESET,
                               desc=self.cmd_BED_CLEAR_RESET_help)

        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)

    # Lifecycle

    def _handle_ready(self):
        self.print_stats = self.printer.lookup_object('print_stats', None)
        self._load_data()
        if self.print_in_progress:
            self.print_in_progress = False
            self._set_state(
                STATE_DIRTY,
                "printer restarted during a print; bed not verified",
                method='auto')
        if self.block_print_start or self.auto_check_on_start:
            self._install_start_guard()
        if self.print_stats is not None:
            self.reactor.register_timer(self._poll_print_state,
                                        self.reactor.NOW)
        logging.info("bed_clear: method=%s checker=%s",
                     self.method, self.checker_url)

    def _poll_print_state(self, eventtime):
        state = self._print_state(eventtime)
        active = state in PRINT_ACTIVE_STATES
        if active and not self.print_in_progress:
            self.print_in_progress = True
            self._save_data()
        elif not active and self.print_in_progress:
            self.print_in_progress = False
            self._set_state(STATE_DIRTY,
                            "print ended; bed not verified", method='auto')
        return eventtime + 0.5

    # Persistence

    def _load_data(self):
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file) as f:
                data = json.load(f)
        except (IOError, OSError, ValueError) as e:
            logging.warning("bed_clear: unable to read %s: %s",
                            self.data_file, e)
            return
        if data.get('version') != DATA_VERSION:
            logging.warning("bed_clear: ignoring data file with version %s",
                            data.get('version'))
            return
        state = data.get('state') or {}
        saved_state = state.get('state')
        if saved_state in ALL_STATES:
            self.state = saved_state
            self.reason = state.get('reason') or self.reason
            self.last_check_at = self._as_float(state.get('last_check'))
            self.changed_at = self._as_float(state.get('changed_at'))
            self.checked_method = state.get('method') or ''
            metrics = state.get('metrics')
            if isinstance(metrics, dict):
                self.metrics = self._clean_metrics(metrics)
        self.print_in_progress = bool(data.get('print_in_progress', False))

    @staticmethod
    def _as_float(value, default=0.):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _clean_metrics(self, result):
        regions = result.get('regions')
        if not isinstance(regions, list):
            regions = []
        return {
            'changed_cells': self._as_int(result.get('changed_cells')),
            'changed_fraction': self._as_float(
                result.get('changed_fraction')),
            'largest_component': self._as_int(
                result.get('largest_component')),
            'max_diff': self._as_float(result.get('max_diff')),
            'mean_diff': self._as_float(result.get('mean_diff')),
            'regions': regions,
        }

    def _save_data(self):
        data = {
            'version': DATA_VERSION,
            'state': {
                'state': self.state,
                'reason': self.reason,
                'last_check': self.last_check_at,
                'changed_at': self.changed_at,
                'method': self.checked_method,
                'metrics': self.metrics,
            },
            'print_in_progress': self.print_in_progress,
        }
        try:
            self._atomic_write(data)
            return True
        except (IOError, OSError, TypeError) as e:
            logging.warning("bed_clear: unable to save %s: %s",
                            self.data_file, e)
            return False

    def _atomic_write(self, data):
        dirname = os.path.dirname(self.data_file)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp_path = self.data_file + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, self.data_file)

    # State helpers

    def _print_state(self, eventtime=None):
        if self.print_stats is None:
            return 'standby'
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        try:
            return self.print_stats.get_status(eventtime).get('state',
                                                              'standby')
        except Exception:
            logging.exception("bed_clear: unable to read print_stats")
            return 'standby'

    def _print_active(self, eventtime=None):
        return self._print_state(eventtime) in PRINT_ACTIVE_STATES

    def _set_state(self, state, reason, method='', checked=False,
                   metrics=None):
        changed = state != self.state or reason != self.reason
        self.state = state
        self.reason = reason
        self.checked_method = method
        self.changed_at = time.time()
        if checked:
            self.last_check_at = self.changed_at
        if metrics is not None:
            self.metrics = self._clean_metrics(metrics)
        if changed:
            logging.info("bed_clear: state changed to %s (%s)", state, reason)
            self.printer.send_event("bed_clear:changed", state, reason)
        return self._save_data()

    def _verified_clear(self):
        if self.state != STATE_CLEAR:
            return False, self.reason or "bed not verified"
        if self.max_age > 0. and self.last_check_at > 0.:
            age = time.time() - self.last_check_at
            if age > self.max_age:
                return False, "verification expired (%.0f s old)" % age
        return True, self.reason

    def _is_clear(self, eventtime=None):
        ok, reason = self._verified_clear()
        if not ok:
            return False, reason
        if self.print_in_progress or self._print_active(eventtime):
            return False, "a print is in progress"
        return True, reason

    def _require_idle(self, gcmd):
        state = self._print_state()
        if state in PRINT_ACTIVE_STATES:
            raise gcmd.error("bed_clear: can't check the bed while a print "
                             "is %s" % state)

    # Checker service

    def _request(self, path, method='GET', timeout=None):
        url = self.checker_url + path
        headers = {'Accept': 'application/json'}
        if self.checker_token:
            headers['X-Bed-Clear-Token'] = self.checker_token
        request = urllib.request.Request(url, headers=headers, method=method)
        if timeout is None:
            timeout = self.check_timeout
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            detail = self._error_detail(body) or str(e)
            raise CheckerError("HTTP %s: %s" % (e.code, detail))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise CheckerError(str(e))
        try:
            payload = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise CheckerError("checker returned invalid JSON")
        if not isinstance(payload, dict):
            raise CheckerError("checker returned an unexpected response")
        return payload

    @staticmethod
    def _error_detail(body):
        try:
            payload = json.loads(body.decode('utf-8'))
            return payload.get('error') or payload.get('message')
        except Exception:
            return body.decode('utf-8', 'replace')[:200]

    def _checker_status(self):
        try:
            timeout = min(self.check_timeout, 3.)
            return self._request('/status', timeout=timeout)
        except CheckerError as e:
            return {'error': str(e)}

    def _park(self):
        if self.park_gcode:
            self.gcode.run_script_from_command(self.park_gcode)
        if self.settle_ms > 0:
            self.gcode.run_script_from_command("G4 P%d" % (self.settle_ms,))

    def _summary(self):
        fraction = self._as_float(self.metrics.get('changed_fraction'))
        cells = self._as_int(self.metrics.get('changed_cells'))
        largest = self._as_int(self.metrics.get('largest_component'))
        max_diff = self._as_float(self.metrics.get('max_diff'))
        return ("%d changed cells (%.2f%%), largest region %d cells, "
                "max diff %.2f" % (cells, fraction * 100., largest, max_diff))

    # Commands

    cmd_BED_CLEAR_CHECK_help = ("Ask the camera whether the bed is clear "
                                "(or evaluate the confirmation in manual "
                                "mode)")

    def cmd_BED_CLEAR_CHECK(self, gcmd):
        self._require_idle(gcmd)
        if self.method == 'manual':
            clear, reason = self._is_clear()
            if not clear:
                raise gcmd.error(
                    "bed_clear: bed is not verified clear (%s). Clear the bed "
                    "and run BED_CLEAR_CONFIRM." % (reason,))
            gcmd.respond_info("bed_clear: %s" % (reason,))
            return
        self._run_camera_check(gcmd)

    def _run_camera_check(self, gcmd):
        try:
            self._park()
            result = self._request('/check')
        except Exception as e:
            message = "camera check failed: %s" % (e,)
            if self.state not in (STATE_BLOCKED, STATE_ERROR):
                self._set_state(STATE_ERROR, message, method='camera',
                                checked=True, metrics={})
            raise gcmd.error("bed_clear: %s" % (message,))
        clear = result.get('clear')
        if clear is True:
            reason = str(result.get('reason')
                         or "camera reports the bed is clear")
            self._set_state(STATE_CLEAR, reason, method='camera', checked=True,
                            metrics=result)
            gcmd.respond_info("bed_clear: bed is CLEAR (%s)"
                              % (self._summary(),))
            return result
        if clear is False:
            reason = str(result.get('reason')
                         or "camera detected a change on the bed")
            self._set_state(STATE_BLOCKED, reason, method='camera',
                            checked=True, metrics=result)
            raise gcmd.error("bed_clear: BED NOT CLEAR - %s (%s)"
                             % (reason, self._summary()))
        message = "camera returned an invalid result (missing clear field)"
        self._set_state(STATE_ERROR, message, method='camera', checked=True,
                        metrics={})
        raise gcmd.error("bed_clear: %s" % (message,))

    cmd_BED_CLEAR_CALIBRATE_help = ("Capture a new empty-bed camera baseline")

    def cmd_BED_CLEAR_CALIBRATE(self, gcmd):
        if self.method != 'camera':
            raise gcmd.error("bed_clear: BED_CLEAR_CALIBRATE requires "
                             "method: camera")
        self._require_idle(gcmd)
        try:
            self._park()
            result = self._request('/calibrate', 'POST')
            if not result.get('ok'):
                raise CheckerError("checker did not confirm the calibration")
        except Exception as e:
            message = "camera calibration failed: %s" % (e,)
            if self.state not in (STATE_BLOCKED, STATE_ERROR):
                self._set_state(STATE_ERROR, message, method='camera',
                                checked=True, metrics={})
            raise gcmd.error("bed_clear: %s" % (message,))
        self._set_state(STATE_CLEAR,
                        "camera baseline captured on an empty bed",
                        method='camera', checked=True, metrics={})
        size = result.get('decode_size')
        suffix = ""
        if isinstance(size, list) and len(size) == 2:
            suffix = " (%sx%s px)" % (size[0], size[1])
        gcmd.respond_info("bed_clear: camera baseline captured%s. Keep the "
                          "toolhead parking and lighting consistent with this "
                          "capture." % (suffix,))

    cmd_BED_CLEAR_CONFIRM_help = ("Mark the bed as clear after clearing it by "
                                  "hand")

    def cmd_BED_CLEAR_CONFIRM(self, gcmd):
        self._require_idle(gcmd)
        self._set_state(STATE_CLEAR, "bed confirmed clear by operator",
                        method='manual', checked=True, metrics={})
        gcmd.respond_info("bed_clear: bed marked CLEAR. Run BED_CLEAR_RESET "
                          "to forget this confirmation.")

    cmd_BED_CLEAR_MARK_DIRTY_help = "Mark the bed as not verified (dirty)"

    def cmd_BED_CLEAR_MARK_DIRTY(self, gcmd):
        reason = gcmd.get('REASON', "marked dirty by gcode")
        self._set_state(STATE_DIRTY, reason, method='manual')
        gcmd.respond_info("bed_clear: bed marked DIRTY (%s)" % (reason,))

    cmd_BED_CLEAR_GATE_help = ("Raise an error unless the last check "
                               "confirmed the bed is clear")

    def cmd_BED_CLEAR_GATE(self, gcmd):
        clear, reason = self._verified_clear()
        if not clear:
            raise gcmd.error(
                "bed_clear: bed is not verified clear (%s). Run "
                "BED_CLEAR_CHECK or BED_CLEAR_CONFIRM before starting a print."
                % (reason,))
        gcmd.respond_info("bed_clear: bed is clear (%s)" % (reason,))

    cmd_BED_CLEAR_STATUS_help = "Report the current bed clear state"

    def cmd_BED_CLEAR_STATUS(self, gcmd):
        clear, reason = self._is_clear()
        lines = ["bed_clear: state=%s clear=%s" % (self.state, clear),
                 "  reason: %s" % (reason,),
                 "  method: %s" % (self.method,),
                 "  checker: %s" % (self.checker_url,)]
        if self.last_check_at > 0.:
            lines.append("  last check: %s (%s)"
                         % (self._format_time(self.last_check_at),
                            self._format_age(self.last_check_at)))
        if self.checked_method == 'camera' and self.last_check_at > 0.:
            lines.append("  camera: %s" % (self._summary(),))
        for region in self.metrics.get('regions', [])[:3]:
            if not isinstance(region, dict):
                continue
            box = region.get('box')
            if not isinstance(box, list) or len(box) != 4:
                continue
            lines.append(
                "  change region: x %.0f-%.0f%%, y %.0f-%.0f%% (%s cells)"
                % (box[0] * 100., box[2] * 100., box[1] * 100.,
                   box[3] * 100., region.get('cells', '?')))
        if self.method == 'manual':
            lines.append("  checker status: not used (manual mode)")
        else:
            status = self._checker_status()
            if 'error' in status:
                lines.append("  checker status: offline (%s)"
                             % (status['error'],))
            else:
                baseline = "yes" if status.get('baseline') else "no"
                lines.append("  checker status: online, baseline=%s"
                             % (baseline,))
        if self.print_in_progress:
            lines.append("  a print is in progress")
        if self.max_age > 0.:
            lines.append("  verification expires after %.0f s"
                         % (self.max_age,))
        lines.append("  data file: %s" % (self.data_file,))
        gcmd.respond_info("\n".join(lines))

    cmd_BED_CLEAR_RESET_help = ("Forget the current bed state; BASELINE=1 "
                                "also deletes the camera baseline")

    def cmd_BED_CLEAR_RESET(self, gcmd):
        self._require_idle(gcmd)
        if gcmd.get_int('BASELINE', 0):
            if self.method == 'camera':
                try:
                    self._request('/reset', 'POST')
                except CheckerError as e:
                    gcmd.respond_info("bed_clear: warning: could not delete "
                                      "the camera baseline: %s" % (e,))
            self.metrics = {}
        self.print_in_progress = False
        self._set_state(STATE_UNKNOWN, "state reset by BED_CLEAR_RESET")
        if not self._save_data():
            raise gcmd.error("bed_clear: state reset in memory but could not "
                             "be saved to %s" % (self.data_file,))
        gcmd.respond_info("bed_clear: state reset")

    # Print start guard

    def _install_start_guard(self):
        gcode = self.gcode
        try:
            func_param = signature(gcode.register_command).parameters.get(
                'func')
        except (TypeError, ValueError):
            func_param = None
        if func_param is None or func_param.default is Parameter.empty:
            logging.warning(
                "bed_clear: this Klipper version cannot chain gcode commands; "
                "block_print_start is disabled, use BED_CLEAR_GATE in "
                "PRINT_START instead")
            return
        targets = (('M24', self._guard_m24, '_orig_m24'),
                   ('SDCARD_PRINT_FILE', self._guard_sdcard_print_file,
                    '_orig_sdcard_print_file'))
        for command, handler, attr in targets:
            old = gcode.register_command(command, None)
            if old is None:
                logging.warning("bed_clear: no %s handler found; not guarded",
                                command)
                continue
            setattr(self, attr, old)
            gcode.register_command(command, handler)

    def _require_start_allowed(self, gcmd):
        clear, reason = self._verified_clear()
        if clear:
            return
        if self.auto_check_on_start and self.method == 'camera':
            print_state = self._print_state()
            if print_state in PRINT_ACTIVE_STATES:
                raise gcmd.error(
                    "bed_clear: cannot auto-check while a print is %s"
                    % (print_state,))
            gcmd.respond_info("bed_clear: bed not verified (%s); running "
                              "camera check" % (reason,))
            self._run_camera_check(gcmd)
            return
        raise gcmd.error(
            "bed_clear: refusing to start a print, bed is not verified "
            "clear (%s). Run BED_CLEAR_CHECK or BED_CLEAR_CONFIRM."
            % (reason,))

    def _guard_m24(self, gcmd):
        if self._print_state() != 'paused':
            self._require_start_allowed(gcmd)
        if self._orig_m24 is not None:
            self._orig_m24(gcmd)

    def _guard_sdcard_print_file(self, gcmd):
        self._require_start_allowed(gcmd)
        if self._orig_sdcard_print_file is not None:
            self._orig_sdcard_print_file(gcmd)

    # Reporting

    @staticmethod
    def _format_time(timestamp):
        if not timestamp:
            return "never"
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))

    @staticmethod
    def _format_age(timestamp):
        age = max(0., time.time() - timestamp)
        if age < 120.:
            return "%.0f s ago" % (age,)
        if age < 7200.:
            return "%.0f min ago" % (age / 60.,)
        return "%.1f h ago" % (age / 3600.,)

    def get_status(self, eventtime):
        clear, reason = self._is_clear(eventtime)
        age = -1.
        if self.last_check_at > 0.:
            age = max(0., time.time() - self.last_check_at)
        return {
            'state': self.state,
            'clear': clear,
            'reason': reason,
            'method': self.method,
            'in_print': self._print_active(eventtime),
            'print_in_progress': self.print_in_progress,
            'last_check': self.last_check_at,
            'last_check_age': age,
            'state_changed': self.changed_at,
            'changed_cells': self._as_int(
                self.metrics.get('changed_cells')),
            'changed_fraction': self._as_float(
                self.metrics.get('changed_fraction')),
            'largest_component': self._as_int(
                self.metrics.get('largest_component')),
            'max_diff': self._as_float(self.metrics.get('max_diff')),
            'mean_diff': self._as_float(self.metrics.get('mean_diff')),
            'regions': list(self.metrics.get('regions', [])),
            'checker_url': self.checker_url,
            'max_age': self.max_age,
            'block_print_start': self.block_print_start,
            'auto_check_on_start': self.auto_check_on_start,
        }


def load_config(config):
    return BedClear(config)
