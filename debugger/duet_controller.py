#!/usr/bin/env python3
"""
Duet 3 Controller — Raspberry Pi 4

Probe wiring (blue wire disconnected, red and green replaced by Pi):
  Pi GPIO 17 (pin 11)  →  Duet io0.in
  Pi GND     (pin 6)   →  Duet GND

Duet config (send in DWC once, or add to config.g):
  M558 P5 C"io0.in" H50 F200 T3000
  G31 P500 X0 Y0 Z0

Logic:
  GPIO HIGH = io0.in reads 1000 = NOT triggered = G38.2 moves freely
  GPIO LOW  = io0.in reads 0    = TRIGGERED     = G38.2 stops

App starts with GPIO HIGH so G38.2 can run immediately.
Press probe button → GPIO LOW → G38.2 stops.
Release probe button → GPIO HIGH → ready for next move.

Install:
  pip install pyserial RPi.GPIO

Find serial port:
  ls /dev/ttyACM*
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import serial
import time
import sys

# ── Config ──────────────────────────────────────────────────────────────────────
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE   = 115200
PROBE_PIN   = 17        # GPIO BCM — wired directly to io0.in (blue wire removed)
TIMEOUT_S   = 10

# ── GPIO ────────────────────────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PROBE_PIN, GPIO.OUT, initial=GPIO.LOW)   # LOW = 0 at rest
    GPIO_AVAILABLE = True
    print(f"[GPIO] Pin {PROBE_PIN} → LOW (rest, 0)")
except ImportError:
    GPIO_AVAILABLE = False
    print("[GPIO] RPi.GPIO not installed — no GPIO")
except Exception as e:
    GPIO_AVAILABLE = False
    print(f"[GPIO] Failed: {e}")


def gpio_activate():
    """GPIO HIGH → io0.in = 1000 → probe triggered."""
    if GPIO_AVAILABLE:
        GPIO.output(PROBE_PIN, GPIO.HIGH)
    print(f"[GPIO] {PROBE_PIN} → HIGH (1000 — triggered)")


def gpio_deactivate():
    """GPIO LOW → io0.in = 0 → probe at rest."""
    if GPIO_AVAILABLE:
        GPIO.output(PROBE_PIN, GPIO.LOW)
    print(f"[GPIO] {PROBE_PIN} → LOW (0 — rest)")


# ── Serial ──────────────────────────────────────────────────────────────────────
class DuetSerial:
    def __init__(self):
        self._ser  = None
        self._lock = threading.Lock()

    def connect(self) -> str:
        try:
            self._ser = serial.Serial(
                SERIAL_PORT, BAUD_RATE,
                timeout=TIMEOUT_S, write_timeout=5
            )
            time.sleep(0.5)
            self._ser.reset_input_buffer()
            print(f"[Serial] Connected: {SERIAL_PORT}")
            return "OK"
        except serial.SerialException as e:
            return f"ERROR: {e}"

    def send(self, gcode: str) -> str:
        if self._ser is None:
            return "ERROR: not connected"
        lines = [l.strip() for l in gcode.strip().splitlines() if l.strip()]
        responses = []
        with self._lock:
            for line in lines:
                try:
                    self._ser.reset_input_buffer()
                    self._ser.write((line + "\n").encode())
                    self._ser.flush()
                    print(f"[Serial] >> {line}")
                    resp = self._read_ok()
                    responses.append(resp)
                    print(f"[Serial] << {resp}")
                except Exception as e:
                    return f"ERROR: {e}"
        return " | ".join(responses)

    def _read_ok(self) -> str:
        lines    = []
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            lines.append(text)
            if text.lower().startswith("ok"):
                break
        return " | ".join(lines) if lines else "(no response)"

    def emergency_stop(self):
        if self._ser:
            try:
                self._ser.write(b"M112\n")
                self._ser.flush()
                print("[Serial] >> M112 EMERGENCY STOP")
            except Exception as e:
                print(f"[Serial] M112 error: {e}")

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()


duet = DuetSerial()


def send_async(gcode: str, callback=None):
    def _run():
        result = duet.send(gcode)
        if callback:
            callback(result)
    threading.Thread(target=_run, daemon=True).start()


# ── UI ──────────────────────────────────────────────────────────────────────────
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Duet 3 Controller")
        self.configure(bg="#1a1a2e")
        self.resizable(False, False)
        self._build_ui()
        self._connect()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        BIG   = tkfont.Font(family="Helvetica", size=15, weight="bold")
        MED   = tkfont.Font(family="Helvetica", size=10)
        SMALL = tkfont.Font(family="Helvetica", size=9)

        # Title
        tk.Label(self, text="Duet 3 Controller",
                 font=tkfont.Font(family="Helvetica", size=20, weight="bold"),
                 fg="#e0e0e0", bg="#1a1a2e").pack(pady=(20, 2))

        tk.Label(self, text=f"Serial: {SERIAL_PORT}   GPIO {PROBE_PIN} → io0.in",
                 font=SMALL, fg="#555555", bg="#1a1a2e").pack(pady=(0, 12))

        # Status bar
        sf = tk.Frame(self, bg="#0f3460", padx=12, pady=9)
        sf.pack(fill="x", padx=20, pady=(0, 16))
        tk.Label(sf, text="Status:", fg="#666666",
                 bg="#0f3460", font=MED).pack(side="left")
        self._status_var = tk.StringVar(value="Connecting...")
        self._status_lbl = tk.Label(
            sf, textvariable=self._status_var,
            fg="#f5a623", bg="#0f3460",
            font=tkfont.Font(family="Helvetica", size=10, weight="bold"))
        self._status_lbl.pack(side="left", padx=(6, 0))

        # Home All
        self._home_btn = self._make_btn(
            "⌂   HOME ALL", "#00d4aa", "#16213e", "#0f3460", self._home_all, BIG)
        self._home_btn.configure(state="disabled")

        # Speed entry
        sf2 = tk.Frame(self, bg="#1a1a2e")
        sf2.pack(fill="x", padx=20, pady=(10, 2))
        tk.Label(sf2, text="Speed (mm/min):", font=MED,
                 fg="#aaaaaa", bg="#1a1a2e").pack(side="left")
        self._speed_var = tk.StringVar(value="200")
        tk.Entry(sf2, textvariable=self._speed_var, font=MED,
                 bg="#0f3460", fg="#e0e0e0", insertbackground="#e0e0e0",
                 relief="flat", bd=0, width=8
                 ).pack(side="left", padx=(10, 0), ipady=5, ipadx=6)

        # Probe Move
        self._move_btn = self._make_btn(
            "↓   PROBE MOVE  (G38.2)", "#a29bfe", "#16213e", "#2d2060",
            self._probe_move, BIG)
        self._move_btn.configure(state="disabled")

        # Probe stop button — press to stop, release to free
        self._probe_btn = tk.Button(
            self,
            text="◉   PROBE  (hold to STOP)",
            font=BIG, bg="#16213e", fg="#f5a623",
            activebackground="#3d2800", activeforeground="#f5a623",
            relief="flat", bd=0, padx=30, pady=20,
            cursor="hand2", state="disabled"
        )
        self._probe_btn.pack(fill="x", padx=20, pady=6)
        self._probe_btn.bind("<ButtonPress-1>",   self._probe_press)
        self._probe_btn.bind("<ButtonRelease-1>", self._probe_release)
        self._probe_btn.bind("<Enter>",
            lambda e: self._probe_btn.configure(bg="#3d2800")
            if str(self._probe_btn["state"]) != "disabled" else None)
        self._probe_btn.bind("<Leave>",
            lambda e: self._probe_btn.configure(bg="#16213e"))

        # Probe indicator
        self._probe_ind = tk.Label(
            self, text="●  GPIO 17: LOW — 0 (rest)",
            font=MED, fg="#00d4aa", bg="#1a1a2e")
        self._probe_ind.pack(pady=(2, 8))

        # Emergency Stop
        tk.Button(
            self, text="⚠   EMERGENCY STOP",
            font=BIG, bg="#c0392b", fg="white",
            activebackground="#e74c3c", activeforeground="white",
            relief="flat", bd=0, padx=30, pady=24,
            cursor="hand2", command=self._estop
        ).pack(fill="x", padx=20, pady=(10, 6))

        gpio_col = "#00d4aa" if GPIO_AVAILABLE else "#c0392b"
        gpio_txt = (f"GPIO {PROBE_PIN}: LOW=0 (rest)  HIGH=1000 (triggered)"
                    if GPIO_AVAILABLE else "GPIO unavailable — demo mode")
        tk.Label(self, text=gpio_txt, font=SMALL,
                 fg=gpio_col, bg="#1a1a2e").pack(pady=(4, 16))

        self.geometry("430x570")

    def _make_btn(self, text, fg, bg, hover_bg, cmd, font):
        b = tk.Button(self, text=text, font=font,
                      bg=bg, fg=fg,
                      activebackground=hover_bg, activeforeground=fg,
                      relief="flat", bd=0, padx=30, pady=20,
                      cursor="hand2", command=cmd)
        b.pack(fill="x", padx=20, pady=6)
        b.bind("<Enter>", lambda e: b.configure(bg=hover_bg)
               if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    # ── Helpers ──────────────────────────────────────────────────────────────────
    def _set_status(self, msg, col="#00d4aa"):
        self._status_var.set(msg)
        self._status_lbl.configure(fg=col)

    def _set_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self._home_btn.configure(state=state)
        self._move_btn.configure(state=state)
        self._probe_btn.configure(state=state)

    # ── Connect ───────────────────────────────────────────────────────────────────
    def _connect(self):
        def _run():
            result = duet.connect()
            if "ERROR" in result:
                self.after(0, lambda: self._set_status(
                    f"Serial error: {result}", "#c0392b"))
                return
            self.after(0, lambda: self._set_status("Ready", "#00d4aa"))
            self.after(0, lambda: self._set_buttons(True))
        threading.Thread(target=_run, daemon=True).start()

    # ── Home All ──────────────────────────────────────────────────────────────────
    def _home_all(self):
        self._set_status("Homing all axes...", "#f5a623")
        self._set_buttons(False)
        def _done(r):
            ok = "ERROR" not in r
            self.after(0, lambda: self._set_status(
                "Home complete" if ok else f"Home error: {r}",
                "#00d4aa" if ok else "#c0392b"))
            self.after(0, lambda: self._set_buttons(True))
        send_async("G28", callback=_done)

    # ── Probe Move ────────────────────────────────────────────────────────────────
    def _probe_move(self):
        raw = self._speed_var.get().strip()
        try:
            speed = float(raw)
            if speed <= 0:
                raise ValueError
        except ValueError:
            self._set_status(f"Invalid speed: '{raw}'", "#c0392b")
            return

        self._set_status(f"Probing at {speed:.0f} mm/min...", "#f5a623")
        self._set_buttons(False)
        self._probe_btn.configure(state="normal")   # probe button stays live during move

        gcode = "\n".join([
            f"M558 F{speed:.0f}",    # set probe speed
            "G90",                    # absolute positioning
            "G38.2 Z0",           # move toward Z0, stop when probe triggers
        ])

        def _done(r):
            ok = "ERROR" not in r
            self.after(0, lambda: self._set_status(
                f"Move done at {speed:.0f} mm/min" if ok else f"Move error: {r}",
                "#00d4aa" if ok else "#c0392b"))
            self.after(0, lambda: self._set_buttons(True))

        send_async(gcode, callback=_done)

    # ── Probe Button ──────────────────────────────────────────────────────────────
    def _probe_press(self, _event):
        if str(self._probe_btn["state"]) == "disabled":
            return
        # Toggle on every click
        self._probe_on = not getattr(self, "_probe_on", False)
        if self._probe_on:
            gpio_activate()     # GPIO HIGH → 1000 → triggered
            self._probe_ind.configure(
                text="●  GPIO 17: HIGH — 1000 (TRIGGERED)", fg="#c0392b")
            self._set_status("Probe: ON (1000)", "#c0392b")
            self._probe_btn.configure(
                bg="#3d2800", text="◉   PROBE  — ON (click to turn off)")
        else:
            gpio_deactivate()   # GPIO LOW → 0 → rest
            self._probe_ind.configure(
                text="●  GPIO 17: LOW — 0 (rest)", fg="#00d4aa")
            self._set_status("Probe: OFF (0)", "#00d4aa")
            self._probe_btn.configure(
                bg="#16213e", text="◉   PROBE  — OFF (click to turn on)")

    def _probe_release(self, _event):
        pass    # toggle — do nothing on release

    # ── Emergency Stop ────────────────────────────────────────────────────────────
    def _estop(self):
        gpio_activate()     # pull HIGH to also trigger probe stop on any active G38.2
        self._probe_on = True
        self._probe_ind.configure(
            text="●  GPIO 17: HIGH — 1000 (TRIGGERED)", fg="#c0392b")
        self._set_status("⚠  EMERGENCY STOP", "#c0392b")
        threading.Thread(target=duet.emergency_stop, daemon=True).start()

    # ── Close ─────────────────────────────────────────────────────────────────────
    def _on_close(self):
        gpio_deactivate()   # back to 0 on exit
        if GPIO_AVAILABLE:
            GPIO.cleanup()
        duet.close()
        self.destroy()
        sys.exit(0)


if __name__ == "__main__":
    print(f"[App] Starting — Serial: {SERIAL_PORT}  GPIO: {PROBE_PIN}")
    print(f"[App] GPIO HIGH = free to move, GPIO LOW = stop")
    App().mainloop()
