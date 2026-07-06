from __future__ import annotations

import os
import re
import sys
import time
import threading
from typing import Optional, Tuple, Dict

_THIS_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    import serial  # pyserial
except Exception:
    serial = None

# GPIO for probe arm/release signal (GPIO 17, 3.3 V logic, P5 digital probe on Duet)
try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except Exception:
    GPIO = None
    _GPIO_AVAILABLE = False

_PROBE_PIN = 17  # BCM pin number

try:
    from Sensor_Testor.domain import models
except ModuleNotFoundError:
    try:
        from domain import models
    except ModuleNotFoundError:
        models = None

_RRX = re.compile(r"[Xx]\s*[:=]\s*(-?\d+(?:\.\d+)?)")
_RRY = re.compile(r"[Yy]\s*[:=]\s*(-?\d+(?:\.\d+)?)")
_RRZ = re.compile(r"[Zz]\s*[:=]\s*(-?\d+(?:\.\d+)?)")


def _parse_rrf_xyz_from_lines(lines) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    x = y = z = None
    for ln in lines:
        if x is None:
            m = _RRX.search(ln)
            if m:
                try:
                    x = float(m.group(1))
                except:
                    pass
        if y is None:
            m = _RRY.search(ln)
            if m:
                try:
                    y = float(m.group(1))
                except:
                    pass
        if z is None:
            m = _RRZ.search(ln)
            if m:
                try:
                    z = float(m.group(1))
                except:
                    pass
    return x, y, z


class Notch50Hz:
    def __init__(self, fs_hz: float, f0_hz: float = 50.0, Q: float = 30.0):
        fs = max(1.0, float(fs_hz))
        from math import sin, cos, pi

        w0 = 2.0 * pi * (f0_hz / fs)
        alpha = sin(w0) / (2.0 * Q)
        c = cos(w0)
        b0 = 1.0
        b1 = -2.0 * c
        b2 = 1.0
        a0 = 1.0 + alpha
        a1 = -2.0 * c
        a2 = 1.0 - alpha
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def reset(self):
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def update(self, x: float) -> float:
        y = (
            self.b0 * x
            + self.b1 * self.x1
            + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2
        )
        self.x2 = self.x1
        self.x1 = x
        self.y2 = self.y1
        self.y1 = y
        return y


class DuetAdapter:
    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200, timeout_s: float = 0.5):
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.soft_touch_debug_hook = None  # set by SoftTouchDebugger.attach()
        self._gpio_ready = False

    # ------------------------ GPIO probe control ------------------------
    def _ensure_gpio(self) -> bool:
        """Set up GPIO pin 17 as output if not already done. Returns True if ready.

        io0.in in config.g is declared as C"!io0.in" — the ! inverts the pin in
        RRF firmware.  So the Duet's logical probe state is:
          GPIO 17 HIGH → io0.in physical HIGH → RRF reads as NOT triggered (armed)
          GPIO 17 LOW  → io0.in physical LOW  → RRF reads as triggered (stop move)
        Initialise HIGH so the Duet sees 'not triggered' from the moment the pin
        is set up.
        """
        if not _GPIO_AVAILABLE:
            return False
        if self._gpio_ready:
            return True
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            # Initial HIGH = not triggered (RRF inverts via ! in C"!io0.in").
            GPIO.setup(_PROBE_PIN, GPIO.OUT, initial=GPIO.HIGH)
            self._gpio_ready = True
            return True
        except Exception:
            return False

    def probe_arm(self) -> None:
        """Set GPIO 17 HIGH — RRF inverts (!io0.in), so HIGH = probe not triggered.
        Must be HIGH before G38.2 is sent or Duet refuses with 'already triggered'."""
        if self._ensure_gpio():
            try:
                GPIO.output(_PROBE_PIN, GPIO.HIGH)
            except Exception:
                pass

    def probe_release(self) -> None:
        """Set GPIO 17 LOW — RRF inverts (!io0.in), so LOW = probe triggered, halts G38.2."""
        if self._ensure_gpio():
            try:
                GPIO.output(_PROBE_PIN, GPIO.LOW)
            except Exception:
                pass

    def gpio_cleanup(self) -> None:
        """Release GPIO resources. Call when closing the adapter."""
        if _GPIO_AVAILABLE and self._gpio_ready:
            try:
                GPIO.output(_PROBE_PIN, GPIO.HIGH)  # leave HIGH = not triggered / safe
                GPIO.cleanup(_PROBE_PIN)
            except Exception:
                pass
            self._gpio_ready = False

    def _dbg(self, event: str, data: dict) -> None:
        """Fire debug hook if attached. Thread-safe, never raises."""
        hook = self.soft_touch_debug_hook
        if hook is None:
            return
        try:
            hook(event, data)
        except Exception:
            pass

    # ------------------------ Low-level serial helpers ------------------------
    def _open(self):
        if serial is None:
            raise RuntimeError("pyserial not available on this system.")
        ser = serial.Serial(self.port, self.baud, timeout=self.timeout_s)
        time.sleep(0.05)
        return ser

    def _send(self, cmd: str, wait_ok: bool = True, ok_timeout_s: float = 1.0) -> str:
        with self._open() as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write((cmd + "\n").encode("utf-8"))
            ser.flush()
            if not wait_ok:
                return ""
            deadline = time.time() + ok_timeout_s
            last = ""
            while time.time() < deadline:
                try:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                except Exception:
                    break
                if line:
                    last = line
                    if "ok" in line.lower():
                        break
            return last

    def send_gcode(self, cmd: str) -> None:
        self._send(cmd, wait_ok=True, ok_timeout_s=1.0)

    def send_gcode_nowait(self, cmd: str) -> None:
        """Fire-and-forget via _send. Still opens the port per call.
        Only use for one-off commands. For the Z feeder hot loop use the
        session API below instead."""
        self._send(cmd, wait_ok=False)

    # ------------------------------------------------------------------
    # Persistent serial session — for the Z-feeder hot loop.
    #
    # open_session()            opens one Serial that stays open.
    # session_write_nowait(cmd) writes without waiting for 'ok' — returns True/False.
    # session_write_ok(cmd, t)  writes and drains until 'ok' (blocking).
    # session_read_ok(t)        drains until 'ok' (used for setup commands).
    # close_session()           closes cleanly.
    #
    # Normal one-off send_gcode() keeps opening/closing as before.
    # ------------------------------------------------------------------
    def open_session(self) -> None:
        """Open a persistent serial connection for the feeder hot loop.
        No-ops if already open.  Sets conservative Z acceleration for smooth
        slow-descent motion — restored to normal by close_session()."""
        if serial is None:
            raise RuntimeError("pyserial not installed.")
        if getattr(self, "_sess", None) is not None:
            return
        ser = serial.Serial(self.port, self.baud, timeout=self.timeout_s)
        time.sleep(0.05)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        self._sess = ser
        # Reduce Z acceleration so Duet's planner can ramp velocity smoothly
        # across each 50 ms segment rather than jumping to target speed instantly.
        try:
            ser.write(b"M201 Z30\n");  ser.flush(); time.sleep(0.05)
            ser.write(b"M204 P200\n"); ser.flush(); time.sleep(0.05)
            waiting = ser.in_waiting
            if waiting:
                ser.read(waiting)
        except Exception:
            pass

    def close_session(self) -> None:
        """Close the persistent session. Safe to call even if not open.
        Restores normal acceleration before closing."""
        ser = getattr(self, "_sess", None)
        if ser is None:
            return
        try:
            ser.write(b"M201 Z100\n");  ser.flush(); time.sleep(0.03)
            ser.write(b"M204 P3000\n"); ser.flush(); time.sleep(0.03)
            waiting = ser.in_waiting
            if waiting:
                ser.read(waiting)
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass
        self._sess = None

    def session_write_nowait(self, cmd: str) -> bool:
        """Write one G-code line on the open session, no 'ok' wait.
        Returns True on success. Must call open_session() first."""
        ser = getattr(self, "_sess", None)
        if ser is None:
            return False
        try:
            ser.write((cmd + "\n").encode("utf-8"))
            ser.flush()
            return True
        except Exception:
            return False

    def session_drain_input(self) -> None:
        """Discard all bytes in the session receive buffer (non-blocking).

        The Duet sends an 'ok' response for every G-code command. When the
        feeder uses session_write_nowait() those responses are never read, so
        the OS receive buffer fills (~4 KB on Linux = ~800 unread 'ok' lines).
        Once full, ser.write() blocks waiting for space — stalling the feeder
        thread and making Z motion ragged.  One non-blocking read per tick
        keeps the buffer clear with a single syscall and negligible CPU cost.
        """
        ser = getattr(self, "_sess", None)
        if ser is None:
            return
        try:
            waiting = ser.in_waiting
            if waiting:
                ser.read(waiting)
        except Exception:
            pass

    def session_write_ok(self, cmd: str, timeout_s: float = 1.0) -> bool:
        """Write one G-code line on the open session and wait for 'ok'."""
        ser = getattr(self, "_sess", None)
        if ser is None:
            return False
        try:
            ser.write((cmd + "\n").encode("utf-8"))
            ser.flush()
        except Exception:
            return False
        return self.session_read_ok(timeout_s)

    def session_read_ok(self, timeout_s: float = 1.0) -> bool:
        """Drain session port until 'ok' seen or timeout. Returns True if 'ok' found."""
        ser = getattr(self, "_sess", None)
        if ser is None:
            return False
        deadline = time.time() + timeout_s
        try:
            while time.time() < deadline:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line and "ok" in line.lower():
                    return True
        except Exception:
            pass
        return False

    def set_feedrate(self, feed_mm_min: float) -> None:
        self._send(f"G1 F{float(feed_mm_min):.0f}", wait_ok=True, ok_timeout_s=1.0)

    def wait_until_idle(self, timeout_s: float = 5.0) -> None:
        self._send("M400", wait_ok=True, ok_timeout_s=timeout_s)

    def close(self) -> None:
        """Release GPIO and any other resources."""
        self.gpio_cleanup()

    def get_position(self, ok_timeout_s: float = 1.5) -> Tuple[Dict[str, float], str]:
        with self._open() as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b"M114\n")
            ser.flush()
            deadline = time.time() + ok_timeout_s
            buf = []
            while time.time() < deadline:
                try:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                except Exception:
                    break
                if line:
                    buf.append(line)
                    if "ok" in line.lower():
                        break
            x, y, z = _parse_rrf_xyz_from_lines(buf)
            pos = {}
            if x is not None:
                pos["X"] = x
            if y is not None:
                pos["Y"] = y
            if z is not None:
                pos["Z"] = z
            return pos, "\n".join(buf)

    # ------------------------ Homing helpers ------------------------
    def safe_up(self, z_abs: float, feedrate: float = 3000.0) -> Tuple[Dict[str, float], str]:
        """Absolute Z move used by home.py before homing to lift clear."""
        self._send("G90", wait_ok=True)
        self._send(f"G1 Z{float(z_abs):.3f} F{float(feedrate):.0f}", wait_ok=True)
        self.wait_until_idle(timeout_s=10.0)
        return self.get_position(ok_timeout_s=1.5)

    def home_and_report(self, terminal=None, axes: str = "XYZ") -> None:
        try:
            ax = "".join(ch for ch in (axes or "XYZ").upper() if ch in "XYZ")
            cmd = "G28" if ax in ("", "XYZ") else ("G28 " + " ".join(list(ax)))
            if terminal:
                terminal.append(f"[Home] Sending {cmd} ...")
            self._send("G90", wait_ok=True)
            self._send(cmd, wait_ok=True, ok_timeout_s=30.0)
            self.wait_until_idle(timeout_s=30.0)
            pos, _raw = self.get_position(ok_timeout_s=2.0)
            x = pos.get("X", None)
            y = pos.get("Y", None)
            z = pos.get("Z", None)
            if terminal:
                def fmt(v):
                    return "?" if v is None else f"{v:.3f}"

                terminal.append(
                    f"[Home] Homed {ax or 'XYZ'} -> X={fmt(x)} Y={fmt(y)} Z={fmt(z)}"
                )
        except Exception as e:
            if terminal:
                terminal.append(f"[Home] Duet home error: {e}")
            raise

    def home_xy(self, terminal=None) -> None:
        return self.home_and_report(terminal=terminal, axes="XY")

    def home_z(self, terminal=None) -> None:
        return self.home_and_report(terminal=terminal, axes="Z")

    # ------------------------ Motion convenience wrappers ------------------------
    def move_xy_and_report(self, x: float, y: float, feedrate: float = 3000.0) -> None:
        self._send("G90", wait_ok=True)
        self._send(
            f"G1 X{float(x):.3f} Y{float(y):.3f} F{float(feedrate):.0f}",
            wait_ok=True,
        )

    def move_z_and_report(self, z_abs: float, feedrate: float = 600.0) -> None:
        self._send("G90", wait_ok=True)
        self._send(f"G1 Z{float(z_abs):.3f} F{float(feedrate):.0f}", wait_ok=True)

        # ------------------------ Soft Touch (GPIO probe + G38.2) ------------------------
    def soft_touch(
        self,
        daq,
        logger=None,
        pre_x: float = 70,
        pre_y: float = 60.0,
        threshold: float = 0.10,
        approach_feedrate: float = 60.0,   # mm/s — converted to mm/min for M558/G38.2
        z_bottom_limit: float = 61.0,
        stream_dt: float = 0.05,
        notch_Q: float = 30.0,
    ):
        """
        Find the physical surface using the GPIO-17 probe signal and G38.2.

        Flow:
          1. 2-second DAQ baseline (measure quiet offset).
          2. Move XY to pre_x, pre_y.
          3. Arm probe: GPIO 17 LOW (active-low — Duet sees 'not triggered').
          4. Configure probe speed: M558 P5 C"!io0.in" F{feed_mm_min}.
          5. Send G90 then G38.2 P0 Z0  (descend until probe triggers or Z0 reached).
          6. DAQ thread watches CH1 diff; when |filtered(CH1 - offset)| > threshold:
               → GPIO 17 HIGH (active-low trigger — Duet stops G38.2).
          7. Wait for G38.2 to finish (Duet sends 'ok' when move ends).
          8. Read final Z position with M114.
          9. Record SoftTouchResult, retract to Z61, return.
        """

        def read_force_raw() -> float:
            """Read one sample from the force gauge — differential CH0 (pins 0+1).
            DaqAdapter is opened with channels=(0,2): CH0=force, CH2=resistance.
            Falls back through the available API on the daq object."""
            try:
                # Fastest path: direct hat access (DaqAdapter exposes .hat)
                if hasattr(daq, "hat") and daq.hat is not None:
                    return float(daq.hat.a_in_read(0))
            except Exception:
                pass
            try:
                # capture_window returns (t, v_force, v_res); first channel is CH0 (force)
                if hasattr(daq, "capture_window"):
                    _, _v0, _v1 = daq.capture_window(0.001)
                    if len(_v0):
                        return float(_v0[-1])
            except Exception:
                pass
            return float("nan")

        def _log(msg: str) -> None:
            print(f"[SoftTouch] {msg}", flush=True)
            if logger:
                try:
                    logger(f"[SoftTouch] {msg}")
                except Exception:
                    pass

        _log(f"ENTER — threshold={threshold:.3f} V  pre_x={pre_x}  pre_y={pre_y}  "
             f"approach_feedrate={approach_feedrate} mm/s  z_bottom_limit={z_bottom_limit}")
        _log(f"GPIO available: {_GPIO_AVAILABLE}  serial available: {serial is not None}")
        _log(f"DAQ object: {type(daq).__name__ if daq is not None else 'None'}")

        # ── 1. DAQ baseline — differential channel 1 (force gauge) ─────────
        # Sample the force gauge at rest for 2 seconds to measure its standing
        # voltage offset.  All watcher readings during descent are corrected by
        # subtracting this offset, so the signal sits near 0 V at rest and rises
        # away from 0 when the probe contacts the sensor.  The threshold is the
        # minimum |offset-corrected| voltage that counts as a contact event.
        _log("Step 1: 2-second baseline scan — differential CH1 (force gauge)...")

        # Safety: if a continuous scan is running on the DAQ, a_in_read will
        # conflict with it.  Stop it; it will be restarted by prearm_live_plot
        # when the actual test step begins.
        try:
            if hasattr(daq, "_scan_running") and daq._scan_running:
                _log("  Stopping active DAQ scan before polled baseline reads...")
                daq.stop_continuous_scan()
        except Exception:
            pass

        pre_samples = 0
        pre_sum = 0.0
        nan_count = 0
        t0 = time.time()
        while time.time() - t0 < 2.0:
            v = read_force_raw()
            if v == v:  # not NaN
                pre_sum += v
                pre_samples += 1
            else:
                nan_count += 1
            time.sleep(0.001)

        offset = (pre_sum / max(1, pre_samples)) if pre_samples > 0 else 0.0
        fs_est = pre_samples / 2.0 if pre_samples > 0 else 1000.0
        _log(f"Baseline done: {pre_samples} good samples, {nan_count} NaNs, "
             f"fs≈{fs_est:.1f} Hz")
        _log(f"Force gauge offset (CH1 diff, at rest) = {offset:.5f} V  "
             f"— will trigger when |CH1 - offset| > {threshold:.3f} V")

        if pre_samples == 0:
            _log("WARNING: zero DAQ samples in baseline — DAQ may not be open or CH1 not connected")

        self._dbg("baseline_done", {
            "samples": pre_samples, "duration_s": 2.0,
            "fs_est_hz": fs_est, "offset_v": offset,
        })

        # ── 2. Move XY ───────────────────────────────────────────────────────
        _log(f"Step 2: Moving XY to X={pre_x}, Y={pre_y}...")
        try:
            self.move_xy_and_report(pre_x, pre_y, feedrate=3000.0)
            _log("XY move complete.")
        except Exception as e:
            _log(f"XY move ERROR: {e}")

        # ── Serial check ─────────────────────────────────────────────────────
        if serial is None:
            _log("ABORT: pyserial not installed — cannot open serial port.")
            return None

        # ── 3-7. Probe sequence ───────────────────────────────────────────────
        feed_mm_min = max(1.0, float(approach_feedrate) * 60.0)
        max_travel_s = (z_bottom_limit / max(0.1, approach_feedrate)) + 15.0
        _log(f"Probe config: feed={feed_mm_min:.0f} mm/min  max_travel_s={max_travel_s:.1f}s")

        notch = Notch50Hz(fs_hz=fs_est, f0_hz=50.0, Q=notch_Q)
        triggered = threading.Event()

        def _daq_watch():
            """Watch CH1 differential (force gauge).
            When |notch_filtered(CH1 - offset)| > threshold, release probe."""
            consec = 0
            last_log = 0.0
            sample_count = 0
            nan_c = 0
            _log("DAQ watcher thread started.")
            while not triggered.is_set():
                vr = read_force_raw()
                if vr == vr:  # not NaN
                    sample_count += 1
                    v_f = notch.update(vr - offset)
                    now = time.time()
                    if now - last_log >= 0.25:
                        _log(f"DAQ watch: sample#{sample_count} "
                             f"CH1_raw={vr:.5f} V  corrected={v_f:.5f} V  "
                             f"|corrected|={abs(v_f):.5f}  thr={threshold:.5f}  "
                             f"consec={consec}  nans={nan_c}")
                        last_log = now
                    if abs(v_f) > threshold:
                        consec += 1
                        if consec >= 5:  # require 5 sustained samples to reject noise spikes
                            _log(f"THRESHOLD CROSSED — pulling GPIO 17 LOW to stop Duet.  "
                                 f"CH1_raw={vr:.5f} V  corrected={v_f:.5f} V  consec={consec}")
                            self.probe_release()
                            triggered.set()
                            return
                    else:
                        consec = 0
                else:
                    nan_c += 1
                time.sleep(0.001)
            _log(f"DAQ watcher exited (triggered={triggered.is_set()} "
                 f"samples={sample_count} nans={nan_c})")

        try:
            _log(f"Step 3: Opening serial port {self.port} at {self.baud} baud...")
            with self._open() as ser:
                _log("Serial port opened OK.")

                def _write_wait(cmd: str, timeout_s: float = 2.0) -> bool:
                    """Send cmd, wait for ok, log every response line.
                    Returns False immediately if Duet replies with an error."""
                    try:
                        ser.write((cmd + "\n").encode("utf-8"))
                        ser.flush()
                    except Exception as _we:
                        _log(f"WRITE ERROR '{cmd}': {_we}")
                        return False
                    deadline = time.time() + timeout_s
                    responses = []
                    try:
                        while time.time() < deadline:
                            ln = ser.readline().decode("utf-8", errors="ignore").strip()
                            if ln:
                                responses.append(ln)
                                if "error" in ln.lower():
                                    _log(f"  CMD '{cmd}' → DUET ERROR: {responses}")
                                    return False
                                if "ok" in ln.lower():
                                    _log(f"  CMD '{cmd}' → ok (responses: {responses})")
                                    return True
                    except Exception as _re:
                        _log(f"READ ERROR after '{cmd}': {_re}")
                    _log(f"  CMD '{cmd}' → TIMEOUT after {timeout_s}s (responses: {responses})")
                    return False

                def _m114() -> Tuple[Optional[float], Optional[float], Optional[float]]:
                    try:
                        ser.reset_input_buffer()
                        ser.write(b"M114\n")
                        ser.flush()
                    except Exception as e:
                        _log(f"M114 write error: {e}")
                        return None, None, None
                    buf = []
                    deadline = time.time() + 2.0
                    try:
                        while time.time() < deadline:
                            ln = ser.readline().decode("utf-8", errors="ignore").strip()
                            if ln:
                                buf.append(ln)
                                if "ok" in ln.lower():
                                    break
                    except Exception:
                        pass
                    _log(f"M114 response: {buf}")
                    return _parse_rrf_xyz_from_lines(buf)

                # Reduce acceleration for smooth probe move
                _log("Setting conservative acceleration (M201 Z20, M204 P100)...")
                r1 = _write_wait("M201 Z20", timeout_s=1.0)
                r2 = _write_wait("M204 P100", timeout_s=1.0)
                _log(f"Accel set: M201={'ok' if r1 else 'TIMEOUT'}  M204={'ok' if r2 else 'TIMEOUT'}")

                # Arm GPIO
                _log(f"Step 4: Arming probe — GPIO {_PROBE_PIN} HIGH (RRF !io0.in inverts: HIGH = not triggered)...")
                gpio_ok = self._ensure_gpio()
                _log(f"GPIO ready: {gpio_ok}  _GPIO_AVAILABLE={_GPIO_AVAILABLE}")
                self.probe_arm()
                _log(f">>> GPIO {_PROBE_PIN} is now HIGH (RRF !-inverted: Duet sees not triggered / armed) <<<")

                # Pre-probe CH0 sanity check — compare offset-corrected voltage,
                # since the watcher also subtracts the offset before comparing.
                _raw_pre = read_force_raw()
                _corr_pre = abs(_raw_pre - offset)
                _log(f"PRE-PROBE CH0 raw={_raw_pre:.5f} V  offset={offset:.5f} V  "
                     f"|corrected|={_corr_pre:.5f} V  threshold={threshold:.3f} V — "
                     f"{'ALREADY ABOVE THRESHOLD — will trigger instantly!' if _corr_pre >= threshold else 'below threshold, OK'}")

                # Configure probe — P5 = digital input on io0.in.
                # RRF 3.x requires the pin name via C"..." as well as the type via P.
                # Without C"io0.in" RRF rejects the command with "Missing Z probe P in name(s)".
                _log(f"Step 5: Sending M558 P5 C\"!io0.in\" F{feed_mm_min:.0f}...")
                r3 = _write_wait(f'M558 P5 C"!io0.in" F{feed_mm_min:.0f}', timeout_s=1.0)
                _log(f"M558 response: {'ok' if r3 else 'FAILED — probe may not be configured correctly'}")
                if not r3:
                    _log("WARNING: M558 failed. G38.2 may refuse to run or run without speed limit.")

                # Start DAQ watcher
                _log("Step 6: Starting DAQ watcher thread...")
                daq_thread = threading.Thread(target=_daq_watch, daemon=True)
                daq_thread.start()
                _log("DAQ watcher thread started.")

                # Send probe move
                _log("Step 7: Sending G90...")
                r4 = _write_wait("G90", timeout_s=1.0)
                _log(f"G90: {'ok' if r4 else 'TIMEOUT'}")

                # G38.2 P0 Z0 — P0 = use probe index 0 (the probe configured by M558)
                # Without P0 some RRF versions error with "Invalid probe number"
                _log(f"Sending G38.2 P0 Z0 (will block for up to {max_travel_s:.1f}s waiting for ok)...")
                probe_ok = _write_wait("G38.2 P0 Z0", timeout_s=max_travel_s)

                # Stop DAQ watcher
                _log(f">>> GPIO {_PROBE_PIN} state after G38.2: {'LOW (DAQ triggered — Duet stopped)' if triggered.is_set() else 'HIGH (DAQ did not trigger — G38.2 hit Z limit or error)'} <<<")
                triggered.set()
                daq_thread.join(timeout=1.0)

                if probe_ok:
                    _log("G38.2 finished — Duet sent ok.")
                else:
                    _log("G38.2 FAILED or TIMED OUT. Possible causes:\n"
                         "  1. Probe not configured in Duet config.g (no M558 there)\n"
                         "  2. GPIO pin not reaching Duet probe input physically\n"
                         "  3. G38.2 P0 not valid for your RRF version — try G38.2 Z0\n"
                         "  4. Duet returned an error (check lines above)")

                # Read final position
                _log("Step 8: Reading final position with M114...")
                final_x, final_y, final_z = _m114()
                _log(f"Final position: X={final_x} Y={final_y} Z={final_z}")

                self._dbg("exit", {
                    "reason": "probe_triggered" if triggered.is_set() else "timeout",
                    "z_final": final_z,
                })

                # Record result
                result = None
                try:
                    if models is not None and final_z is not None:
                        result = models.SoftTouchResult(
                            x=final_x, y=final_y, z=final_z,
                            voltage=None,
                            threshold=threshold,
                        )
                        if hasattr(models, "runtime_state"):
                            models.runtime_state.last_soft_touch = result
                        _log(f"SoftTouchResult recorded: z={final_z}")
                    else:
                        _log(f"Result NOT recorded — models={models is not None} "
                             f"final_z={final_z}")
                except Exception as _re:
                    _log(f"Result recording error: {_re}")
                    result = None

                # Restore and retract
                _log("Step 9: Restoring acceleration and retracting to Z61...")
                _write_wait("G90", timeout_s=1.0)
                _write_wait("G1 Z61 F2000", timeout_s=15.0)
                _write_wait("M201 Z100", timeout_s=1.0)
                _write_wait("M204 P3000", timeout_s=1.0)
                _log("Retract complete. soft_touch returning.")

                return result

        except Exception as e:
            import traceback
            self.probe_release()
            _log(f"EXCEPTION in soft_touch: {e}")
            _log(traceback.format_exc())
            self._dbg("exit", {"reason": f"exception: {e}"})
            return None
