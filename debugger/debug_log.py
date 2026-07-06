# debugger/debug_log.py
"""
Dead-simple debug logger — one plain-text file, everything in it.

Design goals (deliberately minimal, no monkey-patching, no Qt, no threads):
  • Any module calls  log("message")  and it appears in the file + terminal.
  • One file per run, opened by  open_log(folder)  at the start of a test.
  • Thread-safe (the live DAQ loop runs in a daemon thread).
  • Never raises — logging must never crash a test.

Usage
-----
    from Sensor_Testor.debugger.debug_log import log, open_log, close_log

    open_log("/home/charlie/Documents/test_15/9")   # start of run
    log("[Step] starting test 1")                    # anywhere, any thread
    close_log()                                      # end of run

If open_log() was never called, log() still prints to the terminal, so you
never lose output — it just won't be written to a file until a run starts.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

# ── module-level state ─────────────────────────────────────────────────────
_log_file = None            # type: Optional[object]
_log_path = None            # type: Optional[str]
_lock     = threading.Lock()
_t0       = None            # type: Optional[float]

# Optional callback into the in-app Debug tab. operator_mode sets this once so
# log lines also show live in the GUI. Kept dead simple: a single callable(str).
_ui_sink = None             # type: Optional[object]


def set_ui_sink(fn) -> None:
    """Register a callable(str) that mirrors each log line to the Debug tab."""
    global _ui_sink
    _ui_sink = fn


def open_log(folder: str, prefix: str = "test_run_debug") -> str:
    """
    Open a fresh log file inside `folder`. Closes any previous file.
    Returns the full path. Never raises — falls back to home dir on error.
    """
    global _log_file, _log_path, _t0
    close_log()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{prefix}_{stamp}.log")
    except Exception:
        path = os.path.join(os.path.expanduser("~"), f"{prefix}_{stamp}.log")
    try:
        # line-buffered so tail -f works and nothing is lost on a crash
        _log_file = open(path, "w", buffering=1, encoding="utf-8")
        _log_path = path
        _t0 = time.time()
    except Exception as e:
        _log_file = None
        _log_path = None
        print(f"[debug_log] could not open log file: {e}", flush=True)
    return _log_path or ""


def close_log() -> None:
    global _log_file, _log_path
    with _lock:
        if _log_file is not None:
            try:
                _log_file.flush()
                _log_file.close()
            except Exception:
                pass
        _log_file = None
        _log_path = None


def log(msg: str) -> None:
    """
    Write one line to terminal + file + optional UI sink.
    Prefixed with elapsed seconds since open_log() for easy timing reads.
    Thread-safe and exception-safe.
    """
    try:
        if _t0 is not None:
            line = f"{time.time() - _t0:7.3f}s  {msg}"
        else:
            line = str(msg)
    except Exception:
        line = str(msg)

    # terminal (always)
    try:
        print(line, flush=True)
    except Exception:
        pass

    # file (if open)
    with _lock:
        if _log_file is not None:
            try:
                _log_file.write(line + "\n")
            except Exception:
                pass

    # UI tab (if wired)
    if _ui_sink is not None:
        try:
            _ui_sink(line)
        except Exception:
            pass


def current_path() -> Optional[str]:
    """Return the path of the currently open log file, or None."""
    return _log_path
