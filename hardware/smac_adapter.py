# Sensor_Testor/hardware/smac_adapter.py
from __future__ import annotations
import re
import threading
import time
from typing import Callable, Optional, Tuple

try:
    import serial  # pyserial
except Exception:
    serial = None


_TP_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _normalize_tp(raw: str) -> Optional[str]:
    """Extract numeric text from a TP reply."""
    if not raw:
        return None
    m = _TP_NUMBER.search(raw.strip())
    return m.group(0) if m else None


class SmacAdapter:
    """
    SMAC serial adapter with stable TP polling and automatic capture of max downstroke.

    Uses a persistent serial connection (opened on first use, reused for all
    subsequent commands) rather than opening and closing the port on every
    single command.  Opening a serial port on Linux involves a kernel round-trip
    that takes several milliseconds; for TP polling loops running at 20-50 ms
    intervals this added up to a significant fraction of each poll window.

    Thread safety: all serial I/O is protected by self._lock (RLock so the
    same thread can re-enter, e.g. _tp_poll_until_stable calling _send_only).
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 9600,
        read_timeout_s: float = 0.2,
        write_terminator: str = "\r\n",
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout_s = float(read_timeout_s)
        self.write_terminator = write_terminator
        self._log: Callable[[str], None] = logger if logger else print

        # Simulation fallback when pyserial is not available
        self._sim_tp = 0

        # Store the maximum TP reached during a downstroke
        self.max_smac_downstroke: Optional[float] = None

        # Persistent connection state
        self._ser = None
        self._lock = threading.RLock()

    # --------------------- connection management ---------------------

    def _ensure_open(self):
        """
        Return the persistent serial handle, opening it if necessary.
        Must be called with self._lock held.
        """
        if serial is None:
            return None

        if self._ser is not None:
            try:
                if self._ser.is_open:
                    return self._ser
            except Exception:
                pass
            # Port was closed or errored — clear and reopen
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            parity=serial.PARITY_NONE,
            bytesize=serial.EIGHTBITS,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.read_timeout_s,
            write_timeout=1.0,
        )
        return self._ser

    def close(self) -> None:
        """Explicitly close the persistent connection (call on shutdown)."""
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    # --------------------- low-level ---------------------

    def _send_only(self, ser, command: str) -> None:
        payload = (command + self.write_terminator).encode("utf-8", errors="ignore")
        if ser is None:
            c = command.replace(" ", "").upper()
            if c.startswith("PM,MA"):
                self._sim_tp += 25  # simulate motion
            time.sleep(0.002)
            return
        ser.write(payload)
        ser.flush()

    def _readline(self, ser, timeout_s: Optional[float] = None) -> str:
        if ser is None:
            time.sleep(0.002)
            return ""
        old = ser.timeout
        try:
            if timeout_s is not None:
                ser.timeout = float(timeout_s)
            data = ser.readline()
        finally:
            ser.timeout = old
        return data.decode("utf-8", errors="ignore").strip()

    def _send(self, command: str) -> None:
        """Send a command over the persistent connection."""
        if serial is None:
            self._send_only(None, command)
            return
        with self._lock:
            ser = self._ensure_open()
            self._send_only(ser, command)

    # --------------------- TP helpers ---------------------

    def get_position_text(self, timeout_s: float = 0.2) -> str:
        """Return raw TP text, or '' on timeout."""
        if serial is None:
            return str(self._sim_tp)
        with self._lock:
            ser = self._ensure_open()
            self._send_only(ser, "TP")
            return self._readline(ser, timeout_s=timeout_s)

    def _tp_poll_until_stable(
        self,
        stable_reads: int = 5,
        poll_interval_s: float = 0.02,
        max_duration_s: Optional[float] = None,
        require_change: bool = True,
        stop_when_stable: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Poll TP until same value seen stable_reads times in a row (after a change).
        Optionally send ST when stable.
        Returns (stabilized, final_value).

        Uses the persistent connection for the entire polling loop — one port
        open for the duration rather than one open/close per poll tick.
        """
        poll_interval_s = max(0.005, float(poll_interval_s))
        deadline = None if max_duration_s is None else time.monotonic() + max_duration_s

        seen_change = False
        same_count = 0
        last_val: Optional[str] = None
        final_val: Optional[str] = None

        if serial is None:
            # Simulated polling
            while True:
                if not seen_change:
                    self._sim_tp += 1
                reply = str(self._sim_tp)
                val = _normalize_tp(reply)
                if val:
                    self._log(f"[SMAC TP] {val}")
                    if last_val is not None and val != last_val:
                        seen_change = True
                        same_count = 1
                    elif val == last_val:
                        same_count += 1
                    else:
                        same_count = 1
                    last_val = val
                    final_val = val
                    if (not require_change or seen_change) and same_count >= stable_reads:
                        if stop_when_stable:
                            self.stop()
                        return True, final_val
                if deadline and time.monotonic() >= deadline:
                    return False, final_val or last_val
                time.sleep(poll_interval_s)

        # Real hardware — hold the lock for the full polling loop so no other
        # thread interleaves commands with the TP read sequence.
        with self._lock:
            ser = self._ensure_open()
            while True:
                self._send_only(ser, "TP")
                reply = self._readline(ser, timeout_s=poll_interval_s)
                val = _normalize_tp(reply)
                if val:
                    self._log(f"[SMAC TP] {val}")
                    if last_val is not None and val != last_val:
                        seen_change = True
                        same_count = 1
                    elif val == last_val:
                        same_count += 1
                    else:
                        same_count = 1
                    last_val = val
                    final_val = val
                    if (not require_change or seen_change) and same_count >= stable_reads:
                        if stop_when_stable:
                            self._send_only(ser, "ST")
                        return True, final_val
                if deadline and time.monotonic() >= deadline:
                    return False, final_val or last_val
                time.sleep(poll_interval_s)

    # --------------------- public API ---------------------

    def stop(self) -> None:
        self._send("ST")

    def set_up_actuator(self) -> None:
        """Initialize motor and enable drive."""
        self._send(
            "SP27307,PH0,SG5,SI20,SD30,IL5000,FR1,SE16383,SA1000,SV100000,"
            "SQ32767,AL0,AR3,MN,EP"
        )
        self._send("MF")

    def move_actuator_down_for_test(
        self,
        poll_interval_s: float = 0.02,
        stable_reads: int = 5,
        max_duration_s: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Move actuator down and capture max downstroke value once stable.
        """
        self._send("ST")
        # Move indefinitely or long distance
        self._send("PM,SA2600,SV6550,SQ32767,MN,MA10000000,GO,EP")

        stabilized, final_val = self._tp_poll_until_stable(
            stable_reads=stable_reads,
            poll_interval_s=poll_interval_s,
            max_duration_s=max_duration_s,
            require_change=True,
            stop_when_stable=True,
        )

        # Save and report
        if final_val is not None:
            try:
                self.max_smac_downstroke = float(final_val)
            except ValueError:
                self.max_smac_downstroke = None

        if self.max_smac_downstroke is not None:
            self._log(f"[SMAC] Max downstroke reached: {self.max_smac_downstroke}")
        else:
            self._log("[SMAC] Downstroke value not captured.")

        return stabilized, final_val

    def move_actuator_up_and_wait(
        self,
        settle_timeout_s: float = 5.0,
        stable_reads: int = 5,
        poll_interval_s: float = 0.05,
    ) -> Tuple[bool, Optional[str]]:
        """
        Move actuator up until stable.
        """
        self._send("SA1000,SV100000")
        self._send("PM,MA10,GO")
        self._send("WP10")
        self._send("MF")

        return self._tp_poll_until_stable(
            stable_reads=stable_reads,
            poll_interval_s=poll_interval_s,
            max_duration_s=settle_timeout_s,
            require_change=True,
            stop_when_stable=True,
        )

    def soft_touch(
        self,
        daq,
        duet=None,
        threshold: float = 3.0,
        poll_interval_s: float = 0.01,
        logger: Optional[Callable[[str], None]] = None,
    ) -> float:
        """
        Start a long down move, read DAQ ch0 continuously, print values as we descend,
        and stop the actuator as soon as the value goes past `threshold`.
        Optionally also prints Duet Z height (if `duet` is provided).

        Returns the final ch0 reading when threshold is crossed.
        """
        logf = logger if logger else self._log

        # Ensure we're idle, then begin the long down move
        self._send("ST")
        self._send("PM,SA2600,SV6550,SQ32767,MN,MA10000000,GO,EP")

        last_z_sample_t = 0.0
        while True:
            # --- read one short DAQ window and pull the latest value from ch0 ---
            try:
                _, v_force, _ = daq.capture_window(0.01)  # ~10 ms at 1 kHz default
                ch0 = float(v_force[-1]) if len(v_force) else float("nan")
            except Exception as e:
                logf(f"[SoftTouch] DAQ read error: {e}")
                ch0 = float("nan")

            # --- occasionally sample Duet Z (if available) ---
            ztxt = ""
            now = time.time()
            if duet is not None and (now - last_z_sample_t) >= 0.1:
                try:
                    pos, raw = duet.get_position()
                    if "Z" in pos:
                        ztxt = f"  Z={pos['Z']:.3f}"
                except Exception:
                    pass
                last_z_sample_t = now

            logf(f"[SoftTouch] ch0={ch0:.3f}{ztxt}")

            # --- threshold check ---
            if ch0 > threshold:
                # stop actuator and report
                self.stop()
                logf(f"[SoftTouch] Threshold {threshold:.3f} crossed: ch0={ch0:.3f}{ztxt}")
                return ch0

            time.sleep(max(0.002, float(poll_interval_s)))

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


__all__ = ["SmacAdapter"]
