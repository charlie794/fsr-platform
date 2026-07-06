from __future__ import annotations
from typing import Optional

from Sensor_Testor.hardware.duet_adapter import DuetAdapter
from Sensor_Testor.hardware.smac_adapter import SmacAdapter


def _log(terminal, msg: str):
    if terminal is not None:
        try:
            terminal.append(msg)
            return
        except Exception:
            pass
    print(msg)


def run_home_sequence(
    duet: DuetAdapter,
    smac: SmacAdapter,
    terminal: Optional[object] = None,
    safe_z: float = 55.0,   # lift Z to this absolute height BEFORE homing (near top = clear)
    post_home_z: float = 20.0,
) -> None:
    """
    Home the Duet and print the confirmed end position (CMD -> M400 -> M114).
    Before homing, lifts Z to `safe_z` (absolute) to get clear of any tooling.
    On this machine Z increases going UP so safe_z=55 means 55mm up = well clear.
    """
    # --- Duet: lift Z to safe height, then home ---
    try:
        try:
            _log(terminal, f"[Home] Lifting Z to safe height Z={safe_z:.1f} before homing...")
            pos, raw = duet.safe_up(float(safe_z))
            _log(terminal, f"[Home] Position after lift: {raw or pos}")
        except Exception as e:
            _log(terminal, f"[Home] Pre-home lift failed (continuing anyway): {e}")

        _log(terminal, "[Home] Homing stage...")
        pos, raw = duet.home_and_report()
        _log(terminal, f"[Home] Position after homing: {raw or pos}")

    except Exception as e:
        _log(terminal, f"[Home] Duet home error: {e}")
        raise

    # --- SMAC: SETUP -> ensure stopped -> capture max TP -> then jog down/up ---
    try:
        _log(terminal, "[Home] Setting up actuator...")
        smac.set_up_actuator()

        _log(terminal, "[Home] Actuator down...")
        smac.move_actuator_down_for_test()

        _log(terminal, "[Home] Actuator up...")
        smac.move_actuator_up_and_wait()  # uses TS+TP to confirm stop and prints final TP

        _log(terminal, "[Home] Completed.")
    except Exception as e:
        _log(terminal, f"[Home] SMAC error: {e}")
        raise
