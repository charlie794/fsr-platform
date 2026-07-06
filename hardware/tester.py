# z_descent_h1_then_go_home.py
"""
Home the Duet, move Z down slowly with ONE G1 H1 move, stop on Z endstop,
then move to absolute Z0 after the switch is hit.

Requires:
    config.g contains the Z endstop on the pin you want, for example:
        M574 Z1 S1 P"io1.in"
    or for high-end stop:
        M574 Z2 S1 P"io1.in"

Run:
    python z_descent_h1_then_go_home.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# ----------------------------------------------------------------------
# Make runnable directly
# ----------------------------------------------------------------------
if __package__ is None or __package__ == "":
    THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    PKG_ROOT = os.path.dirname(THIS_DIR)
    PROJECT_ROOT = os.path.dirname(PKG_ROOT)
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)

from Sensor_Testor.hardware.duet_adapter import DuetAdapter


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
START_Z = 55.0          # Safe start height before descent
DESCENT_DIST = -500.0   # Large relative homing-style move downward
FEEDRATE = 120.0        # mm/min (~2 mm/s)
GO_TO_Z_AFTER = 0.0     # Where to go after the endstop is hit


def try_get_position(duet: DuetAdapter, label: str) -> None:
    try:
        pos, raw = duet.get_position(ok_timeout_s=2.0)
        print(f"[POS] {label}: {raw or pos}")
    except Exception as e:
        print(f"[WARN] Could not read position ({label}): {e}")


def home_and_go_to_start(duet: DuetAdapter) -> None:
    print("[1] Pre-home lift...")
    try:
        duet.safe_up(START_Z)
    except Exception as e:
        print(f"[WARN] safe_up failed: {e}")

    try_get_position(duet, "after pre-home lift")

    print("[2] Homing machine...")
    duet.home_and_report()
    try_get_position(duet, "after homing")

    print(f"[3] Moving to start height Z={START_Z:.1f}...")
    duet.send_gcode("G90")
    duet.send_gcode(f"G1 Z{START_Z:.3f} F{FEEDRATE:.0f}")
    duet.send_gcode("M400")
    try_get_position(duet, "at start height")


def main():
    duet = DuetAdapter()
    stage = "init"

    try:
        print("=" * 60)
        print("=== Z DESCENT H1 THEN GO TO Z0 ===")
        print(f"START_Z       = {START_Z}")
        print(f"DESCENT_DIST  = {DESCENT_DIST}")
        print(f"FEEDRATE      = {FEEDRATE} mm/min")
        print(f"GO_TO_Z_AFTER = {GO_TO_Z_AFTER}")
        print("=" * 60)

        stage = "home_and_start"
        home_and_go_to_start(duet)

        stage = "descent"
        print("[4] Starting single homing-style descent move...")
        print(f"    G91")
        print(f"    G1 H1 Z{DESCENT_DIST:.3f} F{FEEDRATE:.0f}")
        print("    This will stop when the Z endstop is triggered.")

        t0 = time.monotonic()

        duet.send_gcode("G91")
        duet.send_gcode(f"G1 H1 Z{DESCENT_DIST:.3f} F{FEEDRATE:.0f}")
        duet.send_gcode("M400")

        elapsed = time.monotonic() - t0
        print(f"[5] Descent move ended after {elapsed:.2f}s")
        try_get_position(duet, "after H1 stop")

        stage = "move_to_zero"
        print(f"[6] Moving to absolute Z={GO_TO_Z_AFTER:.3f} ...")
        duet.send_gcode("G90")
        duet.send_gcode(f"G1 Z{GO_TO_Z_AFTER:.3f} F{FEEDRATE:.0f}")
        duet.send_gcode("M400")

        print("[7] Final position:")
        try_get_position(duet, "final")

        print("[DONE] Sequence complete.")

    except KeyboardInterrupt:
        print(f"\n[ABORT] Interrupted at stage: {stage}")
        try:
            duet.send_gcode("M0")
        except Exception as e:
            print(f"[WARN] Could not send M0 on abort: {e}")

    except Exception as e:
        print(f"\n[ERROR] Crash at stage: {stage}")
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            duet.send_gcode("M0")
            print("[RECOVERY] M0 sent.")
        except Exception as stop_err:
            print(f"[RECOVERY] M0 failed: {stop_err}")


if __name__ == "__main__":
    main()
