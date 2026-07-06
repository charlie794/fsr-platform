"""
soft_touch_debug.py
===================
Standalone live debugger for soft_touch().

HOW TO USE
----------
Add these three lines to the END of OperatorModePopup.__init__ in operator_mode.py:

    from Sensor_Testor.ui.soft_touch_debug import SoftTouchDebugger
    self._st_dbg = SoftTouchDebugger()
    self._st_dbg.attach(self.duet)

When you press Soft Touch, a debug window pops up automatically.
It shows every event in real time. Use the Copy button or Ctrl+A / Ctrl+C
to copy everything. The full log is also saved to ~/soft_touch_debug.log.

TO REMOVE: delete the 3 lines you added to operator_mode.py __init__.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QPlainTextEdit,
    QLabel,
    QSizePolicy,
)

_LOG_PATH = os.path.join(os.path.expanduser("~"), "soft_touch_debug.log")


# ---------------------------------------------------------------------------
# Signal bridge — lets background threads safely post text to the Qt window
# ---------------------------------------------------------------------------
class _Bridge(QObject):
    append_text = pyqtSignal(str)
    set_title   = pyqtSignal(str)


# ---------------------------------------------------------------------------
# Debug window
# ---------------------------------------------------------------------------
class _DebugWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Soft Touch Debugger")
        self.resize(980, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # -- top bar --
        top = QHBoxLayout()

        self._title_lbl = QLabel("Waiting for soft touch run…")
        self._title_lbl.setStyleSheet("font-weight:bold; color:#00d0ff; font-size:12px;")
        top.addWidget(self._title_lbl, 1)

        for label, slot in [
            ("Clear",      self._clear),
            ("Select All", self._select_all),
            ("Copy",       self._copy),
        ]:
            b = QPushButton(label)
            b.setFixedWidth(90)
            b.clicked.connect(slot)
            top.addWidget(b)

        root.addLayout(top)

        # -- text area --
        self._text = QPlainTextEdit()
        self._text.setReadOnly(False)          # lets user select manually
        self._text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._text.setFont(QFont("Monospace", 9))
        self._text.setStyleSheet(
            "background:#0a0a0a; color:#e8e8e8; "
            "border:1px solid #444; selection-background-color:#1a4a7a;"
        )
        self._text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._text, 1)

        # -- status bar --
        self._status = QLabel(f"Log file: {_LOG_PATH}")
        self._status.setStyleSheet("color:#888; font-size:10px;")
        root.addWidget(self._status)

        self._bridge = _Bridge()
        self._bridge.append_text.connect(self._on_append)
        self._bridge.set_title.connect(self._title_lbl.setText)

    # -- thread-safe posting --
    def post(self, text: str) -> None:
        self._bridge.append_text.emit(text)

    def set_run_title(self, run_n: int, ts_str: str) -> None:
        self._bridge.set_title.emit(f"Run #{run_n}  —  {ts_str}")

    # -- slots --
    def _on_append(self, text: str) -> None:
        self._text.appendPlainText(text)
        sb = self._text.verticalScrollBar()
        sb.setValue(sb.maximum())
        n = self._text.document().lineCount()
        self._status.setText(f"{n} lines  |  {_LOG_PATH}")

    def _select_all(self) -> None:
        self._text.selectAll()
        self._status.setText("All selected — Ctrl+C or click Copy.")

    def _copy(self) -> None:
        self._text.selectAll()
        self._text.copy()
        self._status.setText("Copied to clipboard ✓")

    def _clear(self) -> None:
        self._text.clear()
        self._status.setText(f"Cleared.  |  {_LOG_PATH}")


# ---------------------------------------------------------------------------
# SoftTouchDebugger
# ---------------------------------------------------------------------------
class SoftTouchDebugger:
    """
    Attach to a DuetAdapter and show all soft_touch events in a pop-up window.

    Add to END of OperatorModePopup.__init__:

        from Sensor_Testor.ui.soft_touch_debug import SoftTouchDebugger
        self._st_dbg = SoftTouchDebugger()
        self._st_dbg.attach(self.duet)
    """

    def __init__(self):
        self._duet: Optional[object] = None
        self._window: Optional[_DebugWindow] = None
        self._lock = threading.Lock()
        self._log_file = None
        self._run_n    = 0
        self._run_start = 0.0
        self._step_counts: dict = {}
        self._daq_counts:  dict = {}

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def attach(self, duet) -> None:
        self._duet = duet
        duet.soft_touch_debug_hook = self._hook
        print("[SoftTouchDebugger] attached — window opens on next soft touch run.")

    def detach(self) -> None:
        if self._duet is not None:
            self._duet.soft_touch_debug_hook = None
            self._duet = None

    # ------------------------------------------------------------------ #
    # Hook — called from DuetAdapter threads. Thread-safe.                #
    # ------------------------------------------------------------------ #

    def _hook(self, event: str, data: dict) -> None:
        ts = time.time()

        if event == "baseline_done":
            self._run_n    += 1
            self._run_start = ts
            self._step_counts = {}
            self._daq_counts  = {}
            self._open_log()
            # Show/create window on main thread
            QTimer.singleShot(0, self._show_window)
            ts_str = time.strftime("%H:%M:%S")
            if self._window is not None:
                self._window.set_run_title(self._run_n, ts_str)

        line = self._format(event, data, ts)
        if line:
            self._write(line)

    # ------------------------------------------------------------------ #
    # Window                                                               #
    # ------------------------------------------------------------------ #

    def _show_window(self) -> None:
        if self._window is None:
            if QApplication.instance() is None:
                return
            self._window = _DebugWindow()
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    # ------------------------------------------------------------------ #
    # Log file                                                             #
    # ------------------------------------------------------------------ #

    def _open_log(self) -> None:
        with self._lock:
            if self._log_file is not None:
                try:    self._log_file.close()
                except Exception: pass
                self._log_file = None
            try:
                self._log_file = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)
            except Exception as exc:
                print(f"[SoftTouchDebugger] cannot open log: {exc}")

    def _write(self, text: str) -> None:
        # Window (thread-safe via Qt signal)
        if self._window is not None:
            try:
                self._window.post(text)
            except Exception:
                pass
        # Log file
        with self._lock:
            f = self._log_file
        if f is not None:
            try:
                f.write(text + "\n")
            except Exception:
                pass
        # Launch terminal (always — useful even without window)
        print(text)

    # ------------------------------------------------------------------ #
    # Format                                                               #
    # ------------------------------------------------------------------ #

    def _format(self, event: str, data: dict, ts: float) -> str:
        e = ts - self._run_start
        t = f"[+{e:7.3f}s]"

        # ---- baseline_done ----
        if event == "baseline_done":
            return (
                f"{t} BASELINE DONE\n"
                f"          samples     = {data.get('samples')}\n"
                f"          duration    = {data.get('duration_s', 0):.3f} s\n"
                f"          sample rate = {data.get('fs_est_hz', 0):.1f} Hz\n"
                f"          offset      = {data.get('offset_v', 0):.6f} V  "
                f"(this is subtracted from every DAQ reading)"
            )

        # ---- phase1_start ----
        elif event == "phase1_start":
            return (
                f"{t} PHASE 1 START  (coarse fast descent)\n"
                f"          feedrate    = {data.get('eff_feed_coarse', 0):.1f} mm/min\n"
                f"          step size   = {data.get('dz_step_coarse', 0):.4f} mm per tick\n"
                f"          tick period = {data.get('stream_dt', 0)*1000:.1f} ms\n"
                f"          max descent = {data.get('z_bottom_limit', 0):.1f} mm from start\n"
                f"          threshold   = {data.get('threshold', 0):.5f} V above baseline\n"
                f"          settle win  = {data.get('coarse_ignore_s', 0):.2f} s\n"
                f"          confirm N   = {data.get('confirm_count')} consecutive samples"
            )

        # ---- phase2_start ----
        elif event == "phase2_start":
            return (
                f"{t} PHASE 2 START  (fine slow descent)\n"
                f"          feedrate    = {data.get('eff_feed_fine', 0):.1f} mm/min\n"
                f"          step size   = {data.get('dz_step_fine', 0):.4f} mm per tick\n"
                f"          tick period = {data.get('stream_dt_fine', 0)*1000:.1f} ms\n"
                f"          threshold   = {data.get('fine_threshold', 0):.5f} V above baseline\n"
                f"          max descent = {data.get('z_bottom_limit', 0):.1f} mm from start\n"
                f"          coarse Z    = {data.get('coarse_z')} mm  "
                f"(absolute after coarse trigger)\n"
                f"          coarse desc = {data.get('z_coarse_descended', 0):.3f} mm descended"
            )

        # ---- daq_sample ----
        elif event == "daq_sample":
            phase    = data.get("phase", "?")
            n        = self._daq_counts.get(phase, 0) + 1
            self._daq_counts[phase] = n
            filt     = data.get("filt_v", 0.0)
            thr      = data.get("threshold", 1e9)
            above    = filt > thr
            ignoring = data.get("in_ignore_window", False)
            consec   = data.get("consec", 0)

            # Print every 50th sample, or any time it's above threshold
            if not above and n % 50 != 0:
                return ""

            if ignoring:
                flag = "  [settle window — trigger suppressed]"
            elif above:
                flag = f"  <<< ABOVE THRESHOLD  consec={consec} >>>"
            else:
                flag = ""

            return (
                f"{t} DAQ  n={n:06d}  ph={phase}  "
                f"raw={data.get('raw_v', 0.0):+.5f}V  "
                f"adj={data.get('adj_v', 0.0):+.5f}V  "
                f"filt={filt:+.5f}V  "
                f"thr={thr:.5f}V"
                f"{flag}"
            )

        # ---- feeder_step ----
        elif event == "feeder_step":
            phase = data.get("phase", "?")
            n     = self._step_counts.get(phase, 0) + 1
            self._step_counts[phase] = n
            # First 5, then every 10th
            if n > 5 and n % 10 != 0:
                return ""
            return (
                f"{t} STEP  n={n:05d}  ph={phase}  "
                f"step={data.get('step_mm', 0):.4f} mm  "
                f"total_desc={data.get('z_descended_mm', 0):.4f} mm  "
                f"limit={data.get('z_bottom_limit', 0):.2f} mm"
            )

        # ---- feeder_write_fail ----
        elif event == "feeder_write_fail":
            return (
                f"{t} *** FEEDER WRITE FAIL  ph={data.get('phase','?')}  "
                f"after {data.get('z_descended_mm', 0):.4f} mm ***\n"
                f"          Serial write to Duet failed.\n"
                f"          Possible causes:\n"
                f"            - USB cable disconnected\n"
                f"            - Serial port timeout (ser.write_timeout)\n"
                f"            - Duet firmware serial buffer overflow"
            )

        # ---- exit ----
        elif event == "exit":
            reason    = data.get("reason", "unknown")
            descended = data.get("z_descended_mm", 0.0)
            crossing  = data.get("crossing_v")
            limit     = data.get("z_bottom_limit", 0.0)
            threshold = data.get("threshold", "?")

            def _cv(v):
                try:    return f"{v:.5f} V"
                except Exception: return str(v)

            descs = {
                "bottom_limit_coarse": (
                    f"BOTTOM LIMIT HIT (coarse) — descended {descended:.3f} mm, "
                    f"limit={limit:.1f} mm\n"
                    f"          >>> Force sensor NEVER crossed threshold <<<\n"
                    f"\n"
                    f"          Diagnose using the DAQ lines above:\n"
                    f"            * If 'filt' values are all near 0.0 — sensor not responding.\n"
                    f"              Check wiring and which DAQ channel force is on.\n"
                    f"            * If 'filt' rises slowly but never hits threshold ({threshold}):\n"
                    f"              threshold is too high — lower it in run_soft_touch().\n"
                    f"            * If 'filt' stays flat even on contact — check sensor offset\n"
                    f"              and whether the notch filter is killing the signal.\n"
                    f"            * If no STEP lines appeared — feeder never ran.\n"
                    f"              Check setup_relative_feed() ok timeouts."
                ),
                "bottom_limit_fine": (
                    f"BOTTOM LIMIT HIT (fine) — descended {descended:.3f} mm, "
                    f"limit={limit:.1f} mm\n"
                    f"          Coarse found surface OK, fine did not.\n"
                    f"          The 5 mm back-off may have gone past the surface.\n"
                    f"          If the surface is <5 mm from the coarse trigger point,\n"
                    f"          reduce the back-off in retract_mm() call."
                ),
                "threshold_triggered_coarse": (
                    f"SURFACE FOUND (coarse)  at {descended:.3f} mm  "
                    f"crossing={_cv(crossing)}"
                ),
                "threshold_triggered_fine": (
                    f"SURFACE FOUND (fine)    at {descended:.3f} mm  "
                    f"crossing={_cv(crossing)}"
                ),
                "feeder_died_coarse": (
                    f"FEEDER DIED (coarse) after {descended:.3f} mm\n"
                    f"          Serial write failed — check USB cable / port."
                ),
                "feeder_died_fine": (
                    f"FEEDER DIED (fine) after {descended:.3f} mm\n"
                    f"          Serial write failed — check USB cable / port."
                ),
                "no_serial": (
                    "NO SERIAL — pyserial not installed or Duet not connected.\n"
                    "          Baseline ran but all Duet motion was skipped."
                ),
            }

            desc        = descs.get(reason, f"UNKNOWN EXIT: {reason}")
            total_steps = sum(self._step_counts.values())
            total_daq   = sum(self._daq_counts.values())

            return (
                f"\n{'-'*68}\n"
                f"{t} EXIT: {desc}\n"
                f"          total steps = {total_steps}   "
                f"total DAQ samples = {total_daq}\n"
                f"{'-'*68}\n"
            )

        return ""
