# runner/test_run_debugger.py
"""
DEPRECATED — the old monkey-patching debugger has been removed.

All debug output now goes through the simple shared logger in
`debugger/debug_log.py`. Logging calls live directly in test_runner.py,
writers.py, etc. — no runtime patching, no separate trigger tracking.

This thin shim remains only so any lingering
`from ... import TestRunDebugger` keeps importing cleanly. Its attach()
is a no-op that just wires the shared logger's UI sink if given a log_fn.
"""

from __future__ import annotations

try:
    from Sensor_Testor.debugger.debug_log import set_ui_sink
except Exception:
    try:
        from debugger.debug_log import set_ui_sink  # type: ignore
    except Exception:
        try:
            from debug_log import set_ui_sink  # type: ignore
        except Exception:
            def set_ui_sink(fn):  # fallback no-op
                pass


class TestRunDebugger:
    """No-op replacement for the old patching debugger."""

    def attach(self, worker, log_fn=None) -> None:
        # Only useful action: mirror log lines to the in-app Debug tab.
        if log_fn is not None:
            try:
                set_ui_sink(log_fn)
            except Exception:
                pass

    def detach(self, *a, **k) -> None:
        pass

    def update_log_path(self, *a, **k) -> None:
        pass
