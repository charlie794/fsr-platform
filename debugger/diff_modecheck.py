"""
diff_mode_check.py  —  MCC-128 differential mode diagnostic
============================================================
Run this standalone on the Pi to prove exactly what the HAT
supports and whether mode writes are taking effect.

    python diff_mode_check.py

It does NOT need the oscilloscope running. Just run it alone.
It will print every relevant enum, attempt the mode writes,
read back the modes if possible, and take a short sample
in both SE and DIFF mode so you can compare the noise floor.
"""

import time
import sys

print("=" * 60)
print("MCC-128 DIFFERENTIAL MODE DIAGNOSTIC")
print("=" * 60)

# ── Step 1: import daqhats and print everything available ──────
print("\n[1] Importing daqhats...")
try:
    import daqhats as dh
    print(f"    daqhats version : {getattr(dh, '__version__', 'unknown')}")
    print(f"    daqhats location: {dh.__file__}")
except Exception as e:
    print(f"    FAILED: {e}")
    sys.exit(1)

# ── Step 2: check AnalogInputMode ─────────────────────────────
print("\n[2] Checking AnalogInputMode enum...")
aim = getattr(dh, 'AnalogInputMode', None)
if aim is None:
    print("    AnalogInputMode NOT FOUND in daqhats.")
    print("    This means a_in_mode_write does not exist on this version.")
    print("    Differential mode cannot be set programmatically.")
    HAS_MODE = False
else:
    print(f"    AnalogInputMode found: {aim}")
    members = [(m, getattr(aim, m)) for m in dir(aim) if not m.startswith('_')]
    print(f"    Members:")
    for name, val in members:
        print(f"      {name} = {val!r}")
    HAS_MODE = True

# ── Step 3: check AnalogInputRange ────────────────────────────
print("\n[3] Checking AnalogInputRange enum...")
air = getattr(dh, 'AnalogInputRange', None)
if air is None:
    print("    AnalogInputRange NOT FOUND.")
else:
    members = [(m, getattr(air, m)) for m in dir(air) if not m.startswith('_')]
    print(f"    Members:")
    for name, val in members:
        print(f"      {name} = {val!r}")

# ── Step 4: check OptionFlags ─────────────────────────────────
print("\n[4] Checking OptionFlags...")
of = getattr(dh, 'OptionFlags', None)
if of is None:
    print("    OptionFlags NOT FOUND.")
else:
    members = [(m, getattr(of, m)) for m in dir(of) if not m.startswith('_')]
    print(f"    Members:")
    for name, val in members:
        print(f"      {name} = {val!r}")

# ── Step 5: find the HAT ──────────────────────────────────────
print("\n[5] Finding MCC-128 HAT...")
try:
    from daqhats import hat_list, HatIDs, mcc128
    hats = hat_list(filter_by_id=HatIDs.MCC_128)
    if not hats:
        print("    No MCC-128 found. Is the HAT connected?")
        sys.exit(1)
    addr = hats[0].address
    print(f"    Found MCC-128 at address {addr}")
    hat = mcc128(addr)
except Exception as e:
    print(f"    FAILED: {e}")
    sys.exit(1)

# ── Step 6: check a_in_mode_write ────────────────────────────
print("\n[6] Checking a_in_mode_write method on hat object...")
if hasattr(hat, 'a_in_mode_write'):
    print("    a_in_mode_write EXISTS on this hat object")
else:
    print("    a_in_mode_write DOES NOT EXIST on this hat object")
    print("    Mode writes will silently do nothing in the oscilloscope")

if hasattr(hat, 'a_in_mode_read'):
    print("    a_in_mode_read EXISTS — can verify mode after writing")
    CAN_READBACK = True
else:
    print("    a_in_mode_read DOES NOT EXIST — cannot verify mode after writing")
    CAN_READBACK = False

# ── Step 7: attempt mode writes and read back ─────────────────
SAMPLE_RATE = 1000.0
N_SAMPLES   = 1000   # 1 second of data
CHANNEL     = 0      # CH0 is the valid diff input for pair CH0/CH1

try:
    opt_continuous = dh.OptionFlags.CONTINUOUS
except Exception:
    opt_continuous = 0

def read_channel_noise(label, chan_mask, n_ch):
    """Start a scan, read N_SAMPLES, return std of CH0."""
    try:
        hat.a_in_scan_start(chan_mask, 0, SAMPLE_RATE, opt_continuous)
        time.sleep(0.2)   # let buffer fill
        result = hat.a_in_scan_read_numpy(N_SAMPLES, 2.0)
        hat.a_in_scan_stop()
        hat.a_in_scan_cleanup()
        import numpy as np
        data = result.data
        # CH0 is first column if scanning single channel
        ch0 = data[0::n_ch][:N_SAMPLES]
        mean = float(np.mean(ch0))
        std  = float(np.std(ch0))
        print(f"    {label:30s}  mean={mean:+.4f} V   noise σ={std*1000:.3f} mV   n={len(ch0)}")
        return std
    except Exception as e:
        print(f"    {label:30s}  FAILED: {e}")
        hat.a_in_scan_stop()
        hat.a_in_scan_cleanup()
        return None

if HAS_MODE and hasattr(hat, 'a_in_mode_write'):
    print("\n[7] Mode write + noise comparison on CH0...")
    print("    (Connect a signal or just leave floating to see noise floor)")
    print()

    # ── Single-ended ──
    se_val = None
    for name in ("SE_BIP", "SINGLE_ENDED", "SE", "SINGLE"):
        se_val = getattr(aim, name, None)
        if se_val is not None:
            print(f"    Writing SE mode using AnalogInputMode.{name} = {se_val!r}")
            break
    if se_val is None:
        print("    Could not find a single-ended enum value — skipping SE test")
    else:
        try:
            hat.a_in_mode_write(se_val)
            if CAN_READBACK:
                readback = hat.a_in_mode_read(CHANNEL)
                print(f"    Read back after SE write: {readback!r}  (expected {se_val!r})")
                if readback == se_val:
                    print("    CONFIRMED: mode register updated correctly")
                else:
                    print("    WARNING: read-back does not match — write may have been ignored")
        except Exception as e:
            print(f"    a_in_mode_write SE failed: {e}")

        se_noise = read_channel_noise("Single-ended CH0", 1 << CHANNEL, 1)

    # ── Differential ──
    diff_val = None
    for name in ("DIFF", "DIFFERENTIAL"):
        diff_val = getattr(aim, name, None)
        if diff_val is not None:
            print(f"\n    Writing DIFF mode using AnalogInputMode.{name} = {diff_val!r}")
            break
    if diff_val is None:
        print("    Could not find a differential enum value — skipping DIFF test")
    else:
        try:
            hat.a_in_mode_write(diff_val)
            if CAN_READBACK:
                readback = hat.a_in_mode_read(CHANNEL)
                print(f"    Read back after DIFF write: {readback!r}  (expected {diff_val!r})")
                if readback == diff_val:
                    print("    CONFIRMED: mode register updated correctly")
                else:
                    print("    WARNING: read-back does not match — write may have been ignored")
        except Exception as e:
            print(f"    a_in_mode_write DIFF failed: {e}")

        diff_noise = read_channel_noise("Differential CH0 (CH0+/CH1-)", 1 << CHANNEL, 1)

    if se_noise and diff_noise:
        ratio = se_noise / diff_noise if diff_noise > 0 else float('inf')
        print()
        print(f"    SE noise  : {se_noise*1000:.3f} mV")
        print(f"    DIFF noise: {diff_noise*1000:.3f} mV")
        print(f"    Ratio SE/DIFF: {ratio:.1f}x")
        if ratio > 1.5:
            print("    RESULT: Differential mode IS reducing noise as expected.")
        elif abs(ratio - 1.0) < 0.2:
            print("    RESULT: No noise difference detected.")
            print("    Possible causes:")
            print("      - Mode write is being ignored by firmware")
            print("      - CH1 is not connected to the signal ground")
            print("      - Both modes produce similar noise on your bench setup")
        else:
            print("    RESULT: Unexpected ratio — check connections")

else:
    print("\n[7] Skipping noise comparison (mode write unavailable)")
    print("    Run the MCC-128 example scripts to verify diff mode separately")

# ── Step 8: reset to SE before exit ──────────────────────────
print("\n[8] Resetting CH0 back to single-ended mode...")
if HAS_MODE and hasattr(hat, 'a_in_mode_write'):
    try:
        se_val = getattr(aim, 'SE_BIP', None) or getattr(aim, 'SINGLE_ENDED', None)
        if se_val is not None:
            hat.a_in_mode_write(se_val)
            print("    Reset OK")
        else:
            print("    Could not find SE enum value to reset")
    except Exception as e:
        print(f"    Reset failed: {e}")
else:
    print("    Skipped (no mode write available)")

print("\n" + "=" * 60)
print("DIAGNOSTIC COMPLETE")
print("=" * 60)
