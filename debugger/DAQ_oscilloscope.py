"""
daq_oscilloscope.py  -  Standalone MCC-128 Oscilloscope
========================================================
Drop into:  .../Sensor_Testor/debugger/
Run:        python daq_oscilloscope.py

New features in this version
-----------------------------
  Cursors          - two time cursors (dT, dV readout) + two voltage cursors
  Measurements     - live min/max/pk-pk/RMS/mean/freq per active channel
  Vertical offset  - per-channel V offset slider to separate overlapping traces
  XY mode          - plot any channel vs any other channel
  Persistence      - phosphor fade effect showing waveform history
  Maths channel    - named computed channel: CH_A op CH_B
  Screenshot       - save current plot as PNG
  FFT view         - real-time frequency spectrum panel (toggleable)
"""

from __future__ import annotations
import sys, os, time, threading, traceback, csv, datetime
from collections import deque
from math import pi, sin, cos
from typing import Dict, List, Optional, Tuple

import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QComboBox, QLineEdit, QCheckBox,
    QSlider, QScrollArea, QSplitter, QGroupBox,
    QDoubleSpinBox, QSpinBox, QMessageBox, QFileDialog, QFrame,
    QTabWidget,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QPalette, QFont

import pyqtgraph as pg
import pyqtgraph.exporters

# ── Theme ──────────────────────────────────────────────────────────────────────
BG       = "#0a0a0f"
GRID_COL = "#1a2a1a"
FORE     = "#33ff33"
PANEL_BG = "#111118"
BORDER   = "#2a4a2a"
BTN_BG   = "#1a2a1a"
BTN_ACT  = "#2a5a2a"
RED_WARN = "#ff4444"
AMBER    = "#ffaa00"
CYAN     = "#00ffff"

pg.setConfigOption("background", BG)
pg.setConfigOption("foreground", FORE)

CHANNEL_COLORS = ["#33ff33","#ffff00","#00ffff","#ff6600",
                  "#ff33ff","#33ffff","#ff3333","#aaaaff"]

FILTER_NAMES   = ["None","50 Hz Notch","100 Hz Notch","EMA a=0.3","MA 20-pt","Butterworth LP 50Hz"]
RANGE_LABELS   = ["+/-10 V","+/-5 V","+/-2 V","+/-1 V"]
RANGE_VOLTAGES = [10.0,5.0,2.0,1.0]
RANGE_ENUMS    = ["BIP_10V","BIP_5V","BIP_2V","BIP_1V"]
RATE_OPTIONS   = [100,500,1000,2000,4000]
BUFFER_OPTIONS = [10,30,60,120]
REFRESH_OPTIONS= [10,25,50]
TRIGGER_MODES  = ["Free Run","Rising Edge","Falling Edge","Either Edge","Pulse Width"]
SWEEP_MODES    = ["Auto","Normal","Single"]
MATH_OPS       = ["A - B","A + B","A * B","A / B","abs(A-B)"]

# ── Sensor equations (force + resistance from models.py) ───────────────────────
# ---------------------------------------------------------------------------
# Sensor equations
#
# These used to be re-implemented here, and had drifted from the runner: the
# force conversion divided by 9.81 (newtons) while test_runner divided by 1000
# (grams), so the oscilloscope read ~102x high, despite a comment claiming the
# two were identical.  Both now call the same code in processing/calibration.py.
# ---------------------------------------------------------------------------
# Make this standalone debug tool importable however it's launched (directly
# from Geany, or as a module). When run directly, sys.path starts at debugger/,
# so the Sensor_Testor package isn't findable — add its parent (and the package
# dir, for a flat fallback) before importing shared calibration code.
_osc_here = os.path.dirname(os.path.abspath(__file__))   # .../Sensor_Testor/debugger
_osc_pkg  = os.path.dirname(_osc_here)                    # .../Sensor_Testor
_osc_root = os.path.dirname(_osc_pkg)                     # .../sensor_testor_3
for _osc_p in (_osc_root, _osc_pkg):
    if _osc_p and _osc_p not in sys.path:
        sys.path.insert(0, _osc_p)

try:
    from Sensor_Testor.processing.calibration import (
        get_force_model,
        get_resistance_model,
    )
except Exception:
    from processing.calibration import (   # flat-layout fallback
        get_force_model,
        get_resistance_model,
    )

_FORCE_MODEL = get_force_model()
_RES_MODEL = get_resistance_model()

_FORCE_M = _FORCE_MODEL.m          # kg per volt
_FORCE_C = _FORCE_MODEL.c

if _RES_MODEL is not None:
    _RES_VMAX, _RES_K, _RES_N = _RES_MODEL.Vmax, _RES_MODEL.k, _RES_MODEL.n
    print(f"[OSC] force: kg = {_FORCE_M:.5g}*(V-{_FORCE_C:.5g})   "
          f"resist: ({_RES_K:.4g}*V/({_RES_VMAX:.5g}-V))^(1/{_RES_N:.5g})")
else:
    _RES_VMAX, _RES_K, _RES_N = float("nan"), float("nan"), float("nan")
    print("[OSC] WARNING: no power_rational resistance calibration found — "
          "resistance traces will be NaN. Re-run Resistance Calibration.")


def _force_kg(v_arr):
    """CH0 diff -> force in kg.  Shared with test_runner."""
    return _FORCE_MODEL.force_kg(v_arr)


def _resistance_ohm(v_arr):
    """CH2 diff -> resistance in ohms.  Shared with test_runner."""
    if _RES_MODEL is None:
        return np.full(np.asarray(v_arr, dtype=float).shape, np.nan)
    return _RES_MODEL.r_from_v_array(v_arr)


def _resistance_mohm(v_arr):
    return _resistance_ohm(v_arr)


# ── DAQ import ─────────────────────────────────────────────────────────────────
DAQ_AVAILABLE=False; _AIM=None; _AIR=None; _OPT_CONT=0
try:
    import daqhats as _dh
    from daqhats import mcc128,HatIDs,hat_list
    try: _OPT_CONT=_dh.OptionFlags.CONTINUOUS
    except: pass
    try: _AIM=_dh.AnalogInputMode
    except: pass
    try: _AIR=_dh.AnalogInputRange
    except: pass
    def _find_hat():
        h=hat_list(filter_by_id=HatIDs.MCC_128)
        if not h: raise RuntimeError("No MCC-128")
        return h[0].address
    DAQ_AVAILABLE=True
except: pass

def _mode_val(diff):
    if _AIM is None: return None
    for n in(("DIFF","DIFFERENTIAL")if diff else("SE","SE_BIP","SINGLE_ENDED","SINGLE")):
        v=getattr(_AIM,n,None)
        if v is not None: return v
def _range_val(lbl):
    if _AIR is None: return None
    try: return getattr(_AIR,RANGE_ENUMS[RANGE_LABELS.index(lbl)],None)
    except: return None
def _range_volts(lbl):
    try: return RANGE_VOLTAGES[RANGE_LABELS.index(lbl)]
    except: return 10.0

# ── Filters ────────────────────────────────────────────────────────────────────
class BiquadState:
    def __init__(self,b0,b1,b2,a1,a2):
        self.b0,self.b1,self.b2=b0,b1,b2; self.a1,self.a2=a1,a2
        self.x1=self.x2=self.y1=self.y2=0.0
    def process(self,x):
        out=np.empty_like(x)
        x1,x2,y1,y2=self.x1,self.x2,self.y1,self.y2
        b0,b1,b2,a1,a2=self.b0,self.b1,self.b2,self.a1,self.a2
        for i,xi in enumerate(x):
            y=b0*xi+b1*x1+b2*x2-a1*y1-a2*y2; x2,x1=x1,xi; y2,y1=y1,y; out[i]=y
        self.x1,self.x2,self.y1,self.y2=x1,x2,y1,y2; return out

def _notch(fs,f0,Q=30.):
    w=2*pi*f0/fs; a=sin(w)/(2*Q); c=cos(w); a0=1+a
    return 1/a0,-2*c/a0,1/a0,-2*c/a0,(1-a)/a0
def _butter_lp(fs,fc):
    try:
        from scipy.signal import butter
        s=butter(2,fc/(fs/2),btype="low",output="sos")[0]; return s[0],s[1],s[2],s[4],s[5]
    except:
        rc=1/(2*pi*fc); dt=1/fs; a=dt/(rc+dt); return a,0.,0.,-(1-a),0.
def make_filter(name,fs=1000.):
    if "50 Hz"  in name: return BiquadState(*_notch(fs,50.))
    if "100 Hz" in name: return BiquadState(*_notch(fs,100.))
    if "Butter" in name: return BiquadState(*_butter_lp(fs,50.))
    return None
def apply_filter(name,data,state):
    if name=="None": return data
    if state is not None: return state.process(data)
    if "EMA" in name:
        out=np.empty_like(data)
        if not len(data): return out
        out[0]=data[0]
        for i in range(1,len(data)): out[i]=0.3*data[i]+0.7*out[i-1]
        return out
    if "MA" in name: return np.convolve(data,np.ones(20)/20,mode="same")
    return data

# ── Mock ───────────────────────────────────────────────────────────────────────
class MockHat:
    _nch=1;_t=0.;_fs=1000.
    def a_in_scan_start(self,*a,**k): pass
    def a_in_scan_stop(self): pass
    def a_in_scan_cleanup(self): pass
    def a_in_mode_write(self,m): pass
    def a_in_range_write(self,r): pass
    def a_in_scan_read_numpy(self,n,to):
        n=max(1,int(n)); nch=self._nch; dt=1/self._fs
        fr=[60,120,180,240,60,90,45,30]; am=[1,.5,.8,.3,1.2,.7,.9,.4]
        buf=np.empty(n*nch,dtype=float)
        for i in range(n):
            t=self._t+i*dt
            for ci in range(nch): buf[i*nch+ci]=am[ci]*sin(2*pi*fr[ci]*t)+np.random.normal(0,.02)
        self._t+=n*dt
        class R: data=buf
        return R()
    class _S: samples_available=100
    def a_in_scan_status(self): return self._S()

# ── Settings ───────────────────────────────────────────────────────────────────
class GlobalSettings:
    def __init__(self):
        self.differential=False; self.range_label="+/-10 V"
        self.sample_rate=1000;   self.buffer_seconds=30
        self.refresh_hz=25
        self.trigger_mode="Free Run"; self.sweep_mode="Auto"
        self.trigger_ch=0;   self.trigger_thr=0.5
        self.trigger_hyst=0.05; self.trigger_holdoff=0.1
        self.pretrig_pct=20; self.pulse_min_ms=1.; self.pulse_max_ms=100.

# ── DAQ Worker ─────────────────────────────────────────────────────────────────
class DAQWorker(QObject):
    error_signal  =pyqtSignal(str)
    trigger_signal=pyqtSignal(float)

    def __init__(self,settings,parent=None):
        super().__init__(parent)
        self._s=settings; self._running=False
        self._lock=threading.Lock()
        self._reconfig=threading.Event(); self._rcfg_lock=threading.Lock()
        bl=int(settings.buffer_seconds*settings.sample_rate)
        self._bufs:Dict[int,deque]={ch:deque(maxlen=bl)for ch in range(8)}
        self._t_buf=deque(maxlen=bl); self._t0=0.
        self._channels=[]; self._pending=None
        self.hat=None; self._nch=0; self._chan_mask=0

    def set_channels(self,channels): self._channels=list(channels)

    def reconfigure(self,channels,new_settings=None):
        with self._rcfg_lock: self._pending={"ch":list(channels),"s":new_settings}
        self._reconfig.set()

    def resize_buffers(self,bl):
        with self._lock:
            for ch in range(8):
                old=list(self._bufs[ch]); self._bufs[ch]=deque(old[-bl:],maxlen=bl)
            old=list(self._t_buf); self._t_buf=deque(old[-bl:],maxlen=bl)

    def snapshot(self,n):
        with self._lock:
            t=np.array(list(self._t_buf)[-n:],dtype=float)
            c={ch:np.array(list(self._bufs[ch])[-n:],dtype=float)for ch in range(8)}
        return t,c

    def stop(self): self._running=False; self._reconfig.set()

    def start_daq(self):
        self._running=True
        try:
            self.hat=mcc128(_find_hat())if DAQ_AVAILABLE else MockHat()
            self._t0=time.monotonic()
            self._apply(self._channels)
            self._loop()
        except Exception as e:
            self.error_signal.emit(f"DAQ error: {e}\n{traceback.format_exc()}")
        finally: self._scan_stop()

    def _apply(self,channels,new_s=None):
        self._scan_stop()
        if new_s: self._s=new_s
        self._channels=list(channels); self._nch=len(channels)
        self._chan_mask=sum(1<<ch for ch in channels)
        if self._nch==0: return
        m=_mode_val(self._s.differential)
        if m is not None:
            try: self.hat.a_in_mode_write(m)
            except: pass
        r=_range_val(self._s.range_label)
        if r is not None:
            try: self.hat.a_in_range_write(r)
            except: pass
        if hasattr(self.hat,"_nch"): self.hat._nch=self._nch
        if hasattr(self.hat,"_fs"):  self.hat._fs=float(self._s.sample_rate)
        self.hat.a_in_scan_start(self._chan_mask,0,float(self._s.sample_rate),_OPT_CONT)

    def _scan_stop(self):
        if self.hat is None: return
        for f in(self.hat.a_in_scan_stop,self.hat.a_in_scan_cleanup):
            try: f()
            except: pass

    def _loop(self):
        CHUNK=max(10,self._s.sample_rate//20)
        last_t=time.monotonic()-self._t0
        holdoff_end=0.; trig_armed=True; last_v=0.; pulse_rise_t=None

        while self._running:
            if self._reconfig.is_set():
                self._reconfig.clear()
                if not self._running: break
                with self._rcfg_lock: p=self._pending; self._pending=None
                if p:
                    self._apply(p["ch"],p.get("s"))
                    CHUNK=max(10,self._s.sample_rate//20)
                    last_t=time.monotonic()-self._t0
                    trig_armed=True; last_v=0.; pulse_rise_t=None
                if self._nch==0: self._reconfig.wait(timeout=0.1); continue

            try:
                avail=self.hat.a_in_scan_status().samples_available
                read_n=max(CHUNK,min(avail,CHUNK*8))
            except: read_n=CHUNK

            tl=time.monotonic()
            try:
                res=self.hat.a_in_scan_read_numpy(read_n,0.5); data=res.data
            except: time.sleep(0.01); continue
            if data is None or data.size==0: time.sleep(0.005); continue
            n=data.size//self._nch
            if n==0: time.sleep(0.005); continue

            cd={}
            for ci,ch in enumerate(self._channels): cd[ch]=data[ci::self._nch][:n]
            dt=1./self._s.sample_rate
            t_arr=last_t+np.arange(1,n+1)*dt; last_t=float(t_arr[-1])
            active=set(self._channels); s=self._s

            with self._lock:
                for i in range(n):
                    self._t_buf.append(t_arr[i])
                    for ch in range(8):
                        v=float(cd[ch][i])if ch in active else float("nan")
                        self._bufs[ch].append(v)
                    if s.trigger_mode=="Free Run":
                        last_v=float(cd[s.trigger_ch][i])if s.trigger_ch in active else 0.; continue
                    if s.trigger_ch not in active: continue
                    v_now=float(cd[s.trigger_ch][i])if s.trigger_ch in cd else float("nan")
                    t_now=float(t_arr[i])
                    if t_now<holdoff_end: last_v=v_now; continue
                    fired=False; thr=s.trigger_thr; hyst=s.trigger_hyst
                    if s.trigger_mode=="Rising Edge":
                        if not trig_armed and v_now<(thr-hyst): trig_armed=True
                        if trig_armed and last_v<thr<=v_now: fired=True; trig_armed=False
                    elif s.trigger_mode=="Falling Edge":
                        if not trig_armed and v_now>(thr+hyst): trig_armed=True
                        if trig_armed and last_v>thr>=v_now: fired=True; trig_armed=False
                    elif s.trigger_mode=="Either Edge":
                        if last_v<thr<=v_now or last_v>thr>=v_now: fired=True
                    elif s.trigger_mode=="Pulse Width":
                        if last_v<thr<=v_now: pulse_rise_t=t_now
                        elif last_v>thr>=v_now and pulse_rise_t is not None:
                            pw=(t_now-pulse_rise_t)*1000.
                            if s.pulse_min_ms<=pw<=s.pulse_max_ms: fired=True
                            pulse_rise_t=None
                    if fired:
                        holdoff_end=t_now+s.trigger_holdoff
                        self.trigger_signal.emit(t_now)
                    last_v=v_now

            elapsed=time.monotonic()-tl
            sl=max(0.,(CHUNK/s.sample_rate)*0.5-elapsed)
            if sl>0: time.sleep(sl)

# ── Channel row ────────────────────────────────────────────────────────────────
# Shared column spec — the header and every ChannelRow use these exact widths,
# margins and spacing so the columns line up perfectly. Per-field text labels
# live only in the header (not repeated on every row) for a clean table look.
CH_ROW_MARGINS = (4, 1, 4, 1)
CH_ROW_SPACING = 4
COL_CH, COL_FILT, COL_Y, COL_OFF = 52, 140, 68, 68
COL_EQ, COL_ABS, COL_CLIP, COL_NOISE, COL_DC = 150, 34, 90, 86, 80
_CH_COLUMNS = [
    ("CH",        COL_CH),
    ("Filter",    COL_FILT),
    ("Y +/-",     COL_Y),
    ("Offset",    COL_OFF),
    ("Equation",  COL_EQ),
    ("Rect",      COL_ABS),
    ("Clip",      COL_CLIP),
    ("Noise",     COL_NOISE),
    ("DC Offset", COL_DC),
]


class ChannelRow(QWidget):
    enable_changed=pyqtSignal(int,bool)
    filter_changed=pyqtSignal(int,str)

    def __init__(self,ch,color,parent=None):
        super().__init__(parent); self.ch=ch; self.color=color
        lay=QHBoxLayout(self)
        lay.setContentsMargins(*CH_ROW_MARGINS); lay.setSpacing(CH_ROW_SPACING)

        self.chk=QCheckBox(f"CH{ch}")
        self.chk.setStyleSheet(f"color:{color};font-weight:bold;font-family:monospace;")
        self.chk.setFixedWidth(COL_CH); lay.addWidget(self.chk)

        self.cmb_filt=QComboBox(); self.cmb_filt.addItems(FILTER_NAMES)
        self.cmb_filt.setFixedWidth(COL_FILT); lay.addWidget(self.cmb_filt)

        self.spin_y=QDoubleSpinBox(); self.spin_y.setRange(0.001,100)
        self.spin_y.setValue(10); self.spin_y.setSingleStep(0.5)
        self.spin_y.setDecimals(3); self.spin_y.setFixedWidth(COL_Y)
        self.spin_y.setSuffix("V")
        self.spin_y.setToolTip("Vertical scale (Y +/-) for this channel")
        lay.addWidget(self.spin_y)

        self.spin_off=QDoubleSpinBox(); self.spin_off.setRange(-20,20)
        self.spin_off.setValue(0); self.spin_off.setSingleStep(0.1)
        self.spin_off.setDecimals(3); self.spin_off.setFixedWidth(COL_OFF)
        self.spin_off.setSuffix("V")
        self.spin_off.setToolTip("Vertical offset — shifts this channel up/down without changing scale")
        lay.addWidget(self.spin_off)

        self.txt_eq=QLineEdit("v"); self.txt_eq.setPlaceholderText("v | force_kg(v) | res_mohm(v) | resistance(v)")
        self.txt_eq.setFixedWidth(COL_EQ)
        self.txt_eq.setToolTip("Equation applied to the raw voltage v before plotting")
        lay.addWidget(self.txt_eq)

        # |+| rectify toggle — when ON, abs() is applied to the raw voltage
        # BEFORE the equation runs (negatives → positive, positives unchanged).
        self.btn_abs=QPushButton("|+|")
        self.btn_abs.setCheckable(True)
        self.btn_abs.setFixedWidth(COL_ABS)
        self.btn_abs.setToolTip("Rectify: convert negative voltages to positive before the equation runs")
        self.btn_abs.setStyleSheet(
            f"QPushButton{{background:#1a2a1a;color:{color};border:1px solid {BORDER};font-weight:bold;}}"
            f"QPushButton:checked{{background:{color};color:#000;}}")
        lay.addWidget(self.btn_abs)

        self.spin_clip=QDoubleSpinBox()
        self.spin_clip.setRange(-1.0, 9999999.0)
        self.spin_clip.setValue(-1.0)       # -1 means OFF (no clipping)
        self.spin_clip.setDecimals(4)
        self.spin_clip.setFixedWidth(COL_CLIP)
        self.spin_clip.setToolTip("High-end clip: any raw voltage above this value is discarded (NaN). Set to -1 to disable.")
        self.spin_clip.setStyleSheet(f"background:#1a2a1a;color:{color};border:1px solid {BORDER};")
        lay.addWidget(self.spin_clip)

        self.lbl_noise=QLabel("s:---")
        self.lbl_noise.setStyleSheet(f"color:{color};font-family:monospace;")
        self.lbl_noise.setToolTip("Live noise (standard deviation) of this channel")
        self.lbl_noise.setFixedWidth(COL_NOISE); lay.addWidget(self.lbl_noise)

        self.lbl_offset_live=QLabel("dc:---")
        self.lbl_offset_live.setStyleSheet(f"color:{color};font-family:monospace;")
        self.lbl_offset_live.setToolTip("Live DC offset (mean voltage) of this channel")
        self.lbl_offset_live.setFixedWidth(COL_DC); lay.addWidget(self.lbl_offset_live)

        lay.addStretch()
        self.chk.toggled.connect(lambda v: self.enable_changed.emit(ch,v))
        self.cmb_filt.currentTextChanged.connect(lambda v: self.filter_changed.emit(ch,v))

    @property
    def enabled(self): return self.chk.isChecked()
    @property
    def filter_name(self): return self.cmb_filt.currentText()
    @property
    def equation(self): return self.txt_eq.text().strip() or "v"
    @property
    def rectify(self): return self.btn_abs.isChecked()
    @property
    def clip_value(self):
        """Raw voltage above which samples are replaced with NaN. -1 = disabled."""
        return self.spin_clip.value()
    @property
    def y_scale(self): return self.spin_y.value()
    @property
    def offset(self): return self.spin_off.value()

    def set_noise(self,s): self.lbl_noise.setText(f"s:{s*1000:.2f}mV")
    def clear_noise(self): self.lbl_noise.setText("s:---")
    def set_offset_live(self,v:float): self.lbl_offset_live.setText(f"dc:{v:.4f}V")
    def clear_offset_live(self): self.lbl_offset_live.setText("dc:---")
    def set_chk_style(self,en):
        self.chk.setStyleSheet(
            f"color:{self.color if en else '#444'};font-weight:bold;font-family:monospace;")

# ── Maths channel row ──────────────────────────────────────────────────────────
class MathsRow(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        lay=QHBoxLayout(self); lay.setContentsMargins(4,1,4,1); lay.setSpacing(4)
        self.chk=QCheckBox("MATH")
        self.chk.setStyleSheet(f"color:#ffffff;font-weight:bold;font-family:monospace;")
        self.chk.setFixedWidth(60); lay.addWidget(self.chk)

        lay.addWidget(QLabel("A:"))
        self.cmb_a=QComboBox(); self.cmb_a.addItems([f"CH{i}" for i in range(8)])
        self.cmb_a.setFixedWidth(60); lay.addWidget(self.cmb_a)

        self.cmb_op=QComboBox(); self.cmb_op.addItems(MATH_OPS)
        self.cmb_op.setFixedWidth(90); lay.addWidget(self.cmb_op)

        lay.addWidget(QLabel("B:"))
        self.cmb_b=QComboBox(); self.cmb_b.addItems([f"CH{i}" for i in range(8)])
        self.cmb_b.setCurrentIndex(2); self.cmb_b.setFixedWidth(60); lay.addWidget(self.cmb_b)

        lay.addWidget(QLabel("Scale"))
        self.spin_y=QDoubleSpinBox(); self.spin_y.setRange(0.001,100)
        self.spin_y.setValue(10); self.spin_y.setFixedWidth(68); self.spin_y.setSuffix("V")
        lay.addWidget(self.spin_y)

        lay.addWidget(QLabel("Offset"))
        self.spin_off=QDoubleSpinBox(); self.spin_off.setRange(-20,20)
        self.spin_off.setValue(0); self.spin_off.setFixedWidth(68); self.spin_off.setSuffix("V")
        lay.addWidget(self.spin_off)

        self.lbl=QLabel("MATH: off")
        self.lbl.setStyleSheet("color:#ffffff;font-family:monospace;")
        lay.addWidget(self.lbl); lay.addStretch()

    @property
    def enabled(self): return self.chk.isChecked()
    @property
    def ch_a(self): return self.cmb_a.currentIndex()
    @property
    def ch_b(self): return self.cmb_b.currentIndex()
    @property
    def op(self): return self.cmb_op.currentText()

# ── Global settings panel ──────────────────────────────────────────────────────
class GlobalSettingsPanel(QWidget):
    settings_changed=pyqtSignal(object)

    def __init__(self,settings,parent=None):
        super().__init__(parent); self._s=settings
        self.setFixedWidth(235)
        self.setStyleSheet(f"background:{PANEL_BG};color:{FORE};")
        root=QVBoxLayout(self); root.setContentsMargins(8,8,8,8); root.setSpacing(6)

        def _sec(t):
            g=QGroupBox(t)
            g.setStyleSheet(f"QGroupBox{{color:{AMBER};border:1px solid {BORDER};"
                f"margin-top:8px;font-weight:bold;}}"
                f"QGroupBox::title{{subcontrol-origin:margin;left:6px;}}")
            v=QVBoxLayout(g); v.setSpacing(3); v.setContentsMargins(6,12,6,6)
            root.addWidget(g); return v

        def _row(lay,label,w):
            h=QHBoxLayout(); h.setSpacing(4)
            l=QLabel(label); l.setFixedWidth(82)
            h.addWidget(l); h.addWidget(w); lay.addLayout(h)

        def _cmb(*items):
            c=QComboBox(); c.addItems(items)
            c.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
            return c

        def _dspn(lo,hi,val,step,dec,suf,tip=""):
            w=QDoubleSpinBox(); w.setRange(lo,hi); w.setValue(val)
            w.setSingleStep(step); w.setDecimals(dec); w.setSuffix(suf)
            w.setStyleSheet(f"background:#1a2a1a;color:{FORE};")
            if tip: w.setToolTip(tip)
            return w

        # Hardware
        hw=_sec("HARDWARE")
        self.cmb_mode=_cmb("Single-Ended","Differential")
        self.cmb_mode.setCurrentText("Differential" if settings.differential else "Single-Ended")
        _row(hw,"Input Mode",self.cmb_mode)
        self.cmb_range=_cmb(*RANGE_LABELS)
        self.cmb_range.setCurrentText(settings.range_label); _row(hw,"Input Range",self.cmb_range)
        self.cmb_rate=_cmb(*[f"{r} Hz" for r in RATE_OPTIONS])
        self.cmb_rate.setCurrentText(f"{settings.sample_rate} Hz"); _row(hw,"Sample Rate",self.cmb_rate)

        # Buffer
        bf=_sec("BUFFER")
        self.cmb_buf=_cmb(*[f"{b} s" for b in BUFFER_OPTIONS])
        self.cmb_buf.setCurrentText(f"{settings.buffer_seconds} s"); _row(bf,"Buffer",self.cmb_buf)
        self.cmb_ref=_cmb(*[f"{r} Hz" for r in REFRESH_OPTIONS])
        self.cmb_ref.setCurrentText(f"{settings.refresh_hz} Hz"); _row(bf,"Refresh",self.cmb_ref)

        # Trigger
        tr=_sec("TRIGGER")
        self.cmb_sweep=_cmb(*SWEEP_MODES)
        self.cmb_sweep.setCurrentText(settings.sweep_mode)
        self.cmb_sweep.setToolTip("Auto: scroll+recentre\nNormal: freeze until trigger\nSingle: one capture then stop")
        _row(tr,"Sweep",self.cmb_sweep)
        self.cmb_trig=_cmb(*TRIGGER_MODES); self.cmb_trig.setCurrentText(settings.trigger_mode)
        _row(tr,"Edge",self.cmb_trig)
        self.cmb_tch=_cmb(*[f"CH{i}" for i in range(8)])
        self.cmb_tch.setCurrentIndex(settings.trigger_ch); _row(tr,"Source",self.cmb_tch)
        self.spin_thr=_dspn(-10,10,settings.trigger_thr,0.05,3," V","Threshold voltage")
        _row(tr,"Threshold",self.spin_thr)
        self.spin_hyst=_dspn(0,2,settings.trigger_hyst,0.01,3," V",
            "Hysteresis: signal must move this far past threshold before re-arming.\nIncrease to reject noise false triggers.")
        _row(tr,"Hysteresis",self.spin_hyst)
        self.spin_hold=_dspn(0.001,10,settings.trigger_holdoff,0.01,3," s","Dead time after trigger")
        _row(tr,"Holdoff",self.spin_hold)
        self.spin_pre=QSpinBox(); self.spin_pre.setRange(0,90)
        self.spin_pre.setValue(settings.pretrig_pct); self.spin_pre.setSuffix(" %")
        self.spin_pre.setStyleSheet(f"background:#1a2a1a;color:{FORE};")
        _row(tr,"Pre-trig",self.spin_pre)
        self.spin_pwmin=_dspn(0.001,10000,settings.pulse_min_ms,0.1,2," ms")
        self.spin_pwmax=_dspn(0.001,10000,settings.pulse_max_ms,1.,2," ms")
        _row(tr,"PW min",self.spin_pwmin); _row(tr,"PW max",self.spin_pwmax)
        self.spin_pwmin.setVisible(False); self.spin_pwmax.setVisible(False)

        self.lbl_status=QLabel("TRIG: FREE RUN")
        self.lbl_status.setStyleSheet(f"color:{AMBER};font-family:monospace;font-weight:bold;")
        tr.addWidget(self.lbl_status)

        self.btn_apply=QPushButton("APPLY SETTINGS")
        self.btn_apply.setStyleSheet(f"background:{BTN_ACT};color:{FORE};font-weight:bold;"
            f"border:1px solid {AMBER};padding:4px;")
        self.btn_apply.clicked.connect(self._emit)
        root.addWidget(self.btn_apply); root.addStretch()

        # Wire live
        for w in(self.cmb_ref,self.cmb_buf,self.spin_thr,self.spin_hyst,
                 self.spin_hold,self.spin_pre,self.spin_pwmin,self.spin_pwmax):
            sig=w.currentTextChanged if isinstance(w,QComboBox) else w.valueChanged
            sig.connect(self._emit)
        self.cmb_sweep.currentTextChanged.connect(self._on_sweep)
        self.cmb_trig.currentTextChanged.connect(self._on_trig_mode)
        self.cmb_tch.currentTextChanged.connect(self._emit)

    def _on_sweep(self,s):
        col={"Auto":FORE,"Normal":AMBER,"Single":RED_WARN}.get(s,FORE)
        self.lbl_status.setStyleSheet(f"color:{col};font-family:monospace;font-weight:bold;")
        self.lbl_status.setText(f"SWEEP: {s.upper()}"); self._emit()

    def _on_trig_mode(self,m):
        pw=m=="Pulse Width"
        self.spin_pwmin.setVisible(pw); self.spin_pwmax.setVisible(pw)
        self.lbl_status.setText(f"TRIG: {m.upper()}"); self._emit()

    def _emit(self,*_):
        s=GlobalSettings()
        s.differential=self.cmb_mode.currentText()=="Differential"
        s.range_label=self.cmb_range.currentText()
        s.sample_rate=RATE_OPTIONS[self.cmb_rate.currentIndex()]
        s.buffer_seconds=BUFFER_OPTIONS[self.cmb_buf.currentIndex()]
        s.refresh_hz=REFRESH_OPTIONS[self.cmb_ref.currentIndex()]
        s.sweep_mode=self.cmb_sweep.currentText()
        s.trigger_mode=self.cmb_trig.currentText()
        s.trigger_ch=self.cmb_tch.currentIndex()
        s.trigger_thr=self.spin_thr.value(); s.trigger_hyst=self.spin_hyst.value()
        s.trigger_holdoff=self.spin_hold.value(); s.pretrig_pct=self.spin_pre.value()
        s.pulse_min_ms=self.spin_pwmin.value(); s.pulse_max_ms=self.spin_pwmax.value()
        self._s=s; self.settings_changed.emit(s)

    def current_settings(self):
        self._emit(); return self._s

# ── Save panel ─────────────────────────────────────────────────────────────────
class SavePanel(QWidget):
    def __init__(self,win,parent=None):
        super().__init__(parent); self._win=win
        self.setStyleSheet(f"background:{PANEL_BG};color:{FORE};")
        lay=QHBoxLayout(self); lay.setContentsMargins(8,3,8,3); lay.setSpacing(6)

        def _cmb(*items):
            c=QComboBox(); c.addItems(items)
            c.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
            lay.addWidget(c); return c
        def _lbl(t): l=QLabel(t); lay.addWidget(l); return l
        def _spn(lo,hi,v,suf=""):
            s=QSpinBox(); s.setRange(lo,hi); s.setValue(v); s.setSuffix(suf)
            s.setStyleSheet(f"background:#1a2a1a;color:{FORE};"); lay.addWidget(s); return s
        def _btn(t,fn,style=""):
            b=QPushButton(t); b.setFixedHeight(26)
            b.setStyleSheet(style or _btn_style()); b.clicked.connect(fn)
            lay.addWidget(b); return b

        _lbl("Ch:"); self.cmb_ch=_cmb("All","CH0","CH1","CH2","CH3","CH4","CH5","CH6","CH7")
        _lbl("Data:"); self.cmb_data=_cmb("Raw","Filtered","Both")
        _lbl("Pts:"); self.spn_pts=_spn(10,1000000,5000)
        _lbl("Fmt:"); self.cmb_fmt=_cmb("time,V","abs_time,V","sample#,V","All cols")
        _btn("SAVE CSV",self._save)
        self.chk_trig=QCheckBox("Auto on trigger")
        self.chk_trig.setStyleSheet(f"color:{AMBER};"); lay.addWidget(self.chk_trig)
        self.lbl_st=QLabel("Ready")
        self.lbl_st.setStyleSheet(f"color:{AMBER};font-family:monospace;"); lay.addWidget(self.lbl_st)
        lay.addStretch()

    def do_trigger_save(self,t):
        if self.chk_trig.isChecked(): self._save(t_trig=t,auto=True)

    def _save(self,*_,t_trig=None,auto=False):
        win=self._win
        if not win._worker: self.lbl_st.setText("Not running"); return
        n=self.spn_pts.value()
        t_arr,ch_arrs=win._worker.snapshot(n)
        if len(t_arr)==0: self.lbl_st.setText("No data"); return

        chs_en=[ch for ch in range(8) if win.ch_rows[ch].enabled]
        sel=self.cmb_ch.currentText()
        save_chs=chs_en if sel=="All" else[int(sel[2:])]

        ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        def_name=f"osc_{'trig' if t_trig else 'manual'}_{ts}.csv"
        if auto: path=os.path.join(os.path.dirname(os.path.abspath(__file__)),def_name)
        else:
            path,_=QFileDialog.getSaveFileName(self,"Save CSV",def_name,"CSV (*.csv)")
            if not path: return

        t_rel=t_arr-t_arr[-1]; dtype=self.cmb_data.currentText(); fmt=self.cmb_fmt.currentText()
        hdrs=[]; cols=[]
        if "time" in fmt or "All" in fmt: hdrs.append("time_s"); cols.append(t_rel)
        if "abs" in fmt: hdrs.append("time_abs_s"); cols.append(t_arr)
        if "sample" in fmt or "All" in fmt: hdrs.append("sample_n"); cols.append(np.arange(len(t_arr)))
        for ch in save_chs:
            v=ch_arrs[ch]; valid=~np.isnan(v); vw=np.where(valid,v,0.)
            if dtype in("Raw","Both"): hdrs.append(f"CH{ch}_raw_V"); cols.append(v)
            if dtype in("Filtered","Both"):
                row=win.ch_rows[ch]; fs=win._settings.sample_rate
                vf=apply_filter(row.filter_name,vw,win._filter_states.get(ch)or make_filter(row.filter_name,fs))
                vf[~valid]=float("nan"); hdrs.append(f"CH{ch}_filt_V"); cols.append(vf)
        n2=min(len(c)for c in cols); cols=[c[-n2:]for c in cols]
        try:
            with open(path,"w",newline="")as f:
                w=csv.writer(f); w.writerow(hdrs)
                for i in range(n2): w.writerow([f"{cols[j][i]:.6g}"for j in range(len(hdrs))])
            self.lbl_st.setText(f"Saved {n2}pts -> {os.path.basename(path)}")
        except Exception as e: self.lbl_st.setText(f"Error: {e}")

# ── Measurements panel ─────────────────────────────────────────────────────────
class MeasPanel(QWidget):
    """Live auto-measurements: min, max, pk-pk, RMS, mean, freq per channel."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{PANEL_BG};color:{FORE};font-family:monospace;font-size:9px;")
        self._grid=QGridLayout(self)
        self._grid.setContentsMargins(4,2,4,2); self._grid.setSpacing(2)
        hdrs=["CH","Min","Max","Pk-Pk","RMS","Mean","Freq"]
        for j,h in enumerate(hdrs):
            l=QLabel(h); l.setStyleSheet(f"color:{AMBER};font-weight:bold;")
            self._grid.addWidget(l,0,j)
        self._rows:Dict[int,List[QLabel]]={}

    def update(self,ch:int,v:np.ndarray,fs:float):
        if ch not in self._rows:
            row=ch+1
            lbs=[QLabel(f"CH{ch}")]
            lbs[0].setStyleSheet(f"color:{CHANNEL_COLORS[ch]};font-weight:bold;")
            for j in range(6): lbs.append(QLabel("---"))
            for j,lb in enumerate(lbs): self._grid.addWidget(lb,row,j)
            self._rows[ch]=lbs
        lbs=self._rows[ch]
        v=v[~np.isnan(v)]
        if len(v)<4: return
        mn,mx=float(np.min(v)),float(np.max(v))
        pkpk=mx-mn; rms=float(np.sqrt(np.mean(v**2))); mean=float(np.mean(v))
        # Frequency: count zero-crossings of demeaned signal
        dm=v-mean; crosses=np.where(np.diff(np.sign(dm)))[0]
        freq=float(len(crosses)*fs/(2.*len(v))) if len(crosses)>1 else 0.
        vals=[f"{mn:.3f}V",f"{mx:.3f}V",f"{pkpk:.3f}V",
              f"{rms:.3f}V",f"{mean:.3f}V",
              f"{freq:.1f}Hz" if freq>0 else "---"]
        for i,val in enumerate(vals): lbs[i+1].setText(val)

    def clear_ch(self,ch):
        if ch in self._rows:
            for lb in self._rows[ch][1:]: lb.setText("---")

    def remove_ch(self,ch):
        if ch in self._rows:
            for lb in self._rows.pop(ch):
                self._grid.removeWidget(lb); lb.deleteLater()

# ── Duet motion control panel ──────────────────────────────────────────────────
class DuetControlPanel(QWidget):
    """Send G-code to Duet 3 over USB serial to jog X/Y/Z axes."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._port = "/dev/ttyACM0"
        self._baud = 115200
        self.setStyleSheet(f"background:{PANEL_BG};color:{FORE};font-family:monospace;")
        lay = QVBoxLayout(self); lay.setContentsMargins(8,6,8,6); lay.setSpacing(6)

        # ── Connection row ────────────────────────────────────────────────────
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Port:"))
        self.txt_port = QLineEdit(self._port)
        self.txt_port.setFixedWidth(130)
        self.txt_port.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        conn_row.addWidget(self.txt_port)
        self.btn_conn = QPushButton("CONNECT")
        self.btn_conn.setFixedHeight(26)
        self.btn_conn.setStyleSheet(_btn_style())
        self.btn_conn.clicked.connect(self._toggle_connect)
        conn_row.addWidget(self.btn_conn)
        self.lbl_conn = QLabel("● DISCONNECTED")
        self.lbl_conn.setStyleSheet(f"color:{RED_WARN};font-family:monospace;font-weight:bold;")
        conn_row.addWidget(self.lbl_conn)
        conn_row.addStretch()
        lay.addLayout(conn_row)

        # ── Position display ─────────────────────────────────────────────────
        pos_row = QHBoxLayout()
        self.lbl_pos = QLabel("X: ---   Y: ---   Z: ---")
        self.lbl_pos.setStyleSheet(f"color:{AMBER};font-family:monospace;font-weight:bold;font-size:13px;")
        pos_row.addWidget(self.lbl_pos)
        btn_pos = QPushButton("READ POS")
        btn_pos.setFixedHeight(24); btn_pos.setStyleSheet(_btn_style())
        btn_pos.clicked.connect(self._read_position)
        pos_row.addWidget(btn_pos)
        btn_home = QPushButton("HOME ALL")
        btn_home.setFixedHeight(24)
        btn_home.setStyleSheet(f"background:#3a1a1a;color:{RED_WARN};font-weight:bold;border:1px solid {RED_WARN};")
        btn_home.clicked.connect(self._home_all)
        pos_row.addWidget(btn_home)
        pos_row.addStretch()
        lay.addLayout(pos_row)

        # ── Jog controls ─────────────────────────────────────────────────────
        jog_grp = QGroupBox("JOG")
        jog_grp.setStyleSheet(f"QGroupBox{{color:{AMBER};border:1px solid {BORDER};margin-top:6px;padding:4px;}}"
                              f"QGroupBox::title{{subcontrol-origin:margin;left:8px;}}")
        jog_lay = QHBoxLayout(jog_grp)

        def _jog_axis(axis, label):
            grp = QVBoxLayout()
            lbl = QLabel(label); lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color:{AMBER};font-weight:bold;font-size:14px;")
            grp.addWidget(lbl)
            btn_p = QPushButton(f"▲ +")
            btn_p.setFixedSize(56, 32); btn_p.setStyleSheet(_btn_style())
            btn_p.clicked.connect(lambda: self._jog(axis, +1))
            btn_n = QPushButton(f"▼ −")
            btn_n.setFixedSize(56, 32); btn_n.setStyleSheet(_btn_style())
            btn_n.clicked.connect(lambda: self._jog(axis, -1))
            grp.addWidget(btn_p); grp.addWidget(btn_n)
            return grp

        for ax, lbl in [("X","X"), ("Y","Y"), ("Z","Z")]:
            jog_lay.addLayout(_jog_axis(ax, lbl))

        jog_lay.addWidget(QLabel("Step:"))
        self.cmb_step = QComboBox()
        self.cmb_step.addItems(["0.1","0.5","1","5","10","50"])
        self.cmb_step.setCurrentIndex(2)
        self.cmb_step.setFixedWidth(60)
        self.cmb_step.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        jog_lay.addWidget(self.cmb_step)
        jog_lay.addWidget(QLabel("mm"))

        jog_lay.addWidget(QLabel("  Speed:"))
        self.spin_feed = QSpinBox()
        self.spin_feed.setRange(1, 20000); self.spin_feed.setValue(3000)
        self.spin_feed.setSuffix(" mm/min")
        self.spin_feed.setFixedWidth(120)
        self.spin_feed.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        jog_lay.addWidget(self.spin_feed)
        jog_lay.addStretch()
        lay.addWidget(jog_grp)

        # ── Go to absolute position ───────────────────────────────────────────
        goto_grp = QGroupBox("GO TO POSITION (absolute)")
        goto_grp.setStyleSheet(jog_grp.styleSheet())
        goto_lay = QHBoxLayout(goto_grp)

        def _coord_spin(label):
            goto_lay.addWidget(QLabel(label))
            sp = QDoubleSpinBox()
            sp.setRange(-500, 500); sp.setValue(0); sp.setDecimals(3)
            sp.setSuffix(" mm"); sp.setFixedWidth(100)
            sp.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
            goto_lay.addWidget(sp)
            return sp

        self.spin_gx = _coord_spin("X:")
        self.spin_gy = _coord_spin("Y:")
        self.spin_gz = _coord_spin("Z:")
        btn_go = QPushButton("GO")
        btn_go.setFixedHeight(28)
        btn_go.setStyleSheet(f"background:#1a3a1a;color:{FORE};font-weight:bold;border:1px solid {FORE};")
        btn_go.clicked.connect(self._goto)
        goto_lay.addWidget(btn_go)
        goto_lay.addStretch()
        lay.addWidget(goto_grp)

        # ── Raw G-code ────────────────────────────────────────────────────────
        raw_row = QHBoxLayout()
        raw_row.addWidget(QLabel("G-code:"))
        self.txt_gcode = QLineEdit()
        self.txt_gcode.setPlaceholderText("e.g.  G28  or  G0 X50 Y50 Z30 F3000")
        self.txt_gcode.setStyleSheet(f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        self.txt_gcode.returnPressed.connect(self._send_raw)
        raw_row.addWidget(self.txt_gcode)
        btn_send = QPushButton("SEND")
        btn_send.setFixedHeight(26); btn_send.setStyleSheet(_btn_style())
        btn_send.clicked.connect(self._send_raw)
        raw_row.addWidget(btn_send)
        lay.addLayout(raw_row)

        # ── Response log ─────────────────────────────────────────────────────
        from PyQt5.QtWidgets import QTextEdit
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFixedHeight(80)
        self.txt_log.setStyleSheet(f"background:#0a0a0f;color:{FORE};font-family:monospace;font-size:10px;"
                                   f"border:1px solid {BORDER};")
        lay.addWidget(self.txt_log)
        lay.addStretch()

        self._serial = None

    def _log(self, msg):
        self.txt_log.append(msg)
        self.txt_log.verticalScrollBar().setValue(self.txt_log.verticalScrollBar().maximum())

    def _toggle_connect(self):
        if self._serial and self._serial.is_open:
            try: self._serial.close()
            except: pass
            self._serial = None
            self.lbl_conn.setText("● DISCONNECTED")
            self.lbl_conn.setStyleSheet(f"color:{RED_WARN};font-family:monospace;font-weight:bold;")
            self.btn_conn.setText("CONNECT")
            self._log("Disconnected.")
        else:
            try:
                import serial
                port = self.txt_port.text().strip() or "/dev/ttyACM0"
                self._serial = serial.Serial(port, self._baud, timeout=1.0)
                import time; time.sleep(0.2)
                # Drain any startup messages
                self._serial.read_all()
                self.lbl_conn.setText(f"● {port}")
                self.lbl_conn.setStyleSheet(f"color:#33ff33;font-family:monospace;font-weight:bold;")
                self.btn_conn.setText("DISCONNECT")
                self._log(f"Connected: {port} @ {self._baud}")
                self._read_position()
            except Exception as e:
                self._log(f"Connect error: {e}")

    def _send(self, cmd) -> str:
        if self._serial is None or not self._serial.is_open:
            self._log("Not connected."); return ""
        try:
            import time
            self._serial.write((cmd.strip() + "\n").encode())
            self._serial.flush()
            resp = ""; deadline = time.time() + 2.0
            while time.time() < deadline:
                line = self._serial.readline().decode(errors="replace").strip()
                if line: resp += line + " "
                if "ok" in line.lower(): break
            self._log(f">>> {cmd}  |  {resp.strip()}")
            return resp
        except Exception as e:
            self._log(f"Send error: {e}"); return ""

    def _read_position(self):
        resp = self._send("M114")
        import re
        m = re.search(r"X:([\d.\-]+)\s+Y:([\d.\-]+)\s+Z:([\d.\-]+)", resp)
        if m:
            self.lbl_pos.setText(f"X: {m.group(1)}   Y: {m.group(2)}   Z: {m.group(3)}")
            self.spin_gx.setValue(float(m.group(1)))
            self.spin_gy.setValue(float(m.group(2)))
            self.spin_gz.setValue(float(m.group(3)))

    def _home_all(self):
        self._send("G28"); self._read_position()

    def _jog(self, axis, direction):
        step = float(self.cmb_step.currentText())
        feed = int(self.spin_feed.value())
        dist = step * direction
        self._send("G91")
        self._send(f"G0 {axis}{dist:.3f} F{feed}")
        self._send("G90")
        self._read_position()

    def _goto(self):
        x = self.spin_gx.value(); y = self.spin_gy.value(); z = self.spin_gz.value()
        feed = int(self.spin_feed.value())
        self._send("G90")
        self._send(f"G0 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feed}")
        self._read_position()

    def _send_raw(self):
        cmd = self.txt_gcode.text().strip()
        if cmd:
            self._send(cmd)
            self.txt_gcode.clear()


# ── Data terminal ──────────────────────────────────────────────────────────────
class DataTerminal(QWidget):
    """Scrolling table showing raw voltage and calculated value side-by-side
    for every active channel. Updated at 5 Hz — readable without flickering."""

    def __init__(self, get_worker_fn, get_ch_rows_fn, parent=None):
        super().__init__(parent)
        self._get_worker  = get_worker_fn
        self._get_ch_rows = get_ch_rows_fn
        self._paused = False
        self._max_rows = 200          # keep last N rows in the display

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # ── Toolbar ──────────────────────────────────────────────────────────
        tb = QHBoxLayout()
        self.btn_pause = QPushButton("⏸ PAUSE")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setFixedHeight(24)
        self.btn_pause.setStyleSheet(
            f"QPushButton{{background:#1a2a1a;color:{FORE};border:1px solid {BORDER};}}"
            f"QPushButton:checked{{background:#3a1a1a;color:{RED_WARN};}}")
        self.btn_pause.toggled.connect(self._on_pause)
        tb.addWidget(self.btn_pause)

        self.btn_clear = QPushButton("✕ CLEAR")
        self.btn_clear.setFixedHeight(24)
        self.btn_clear.setStyleSheet(
            f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        self.btn_clear.clicked.connect(self._clear)
        tb.addWidget(self.btn_clear)

        tb.addWidget(QLabel("  Rows:"))
        self.cmb_rows = QComboBox()
        self.cmb_rows.addItems(["50", "100", "200", "500"])
        self.cmb_rows.setCurrentIndex(2)
        self.cmb_rows.setFixedWidth(60)
        self.cmb_rows.setStyleSheet(
            f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        self.cmb_rows.currentTextChanged.connect(
            lambda v: setattr(self, "_max_rows", int(v)))
        tb.addWidget(self.cmb_rows)

        tb.addWidget(QLabel("  Rate:"))
        self.cmb_rate = QComboBox()
        self.cmb_rate.addItems(["1 Hz", "5 Hz", "10 Hz", "25 Hz"])
        self.cmb_rate.setCurrentIndex(1)
        self.cmb_rate.setFixedWidth(70)
        self.cmb_rate.setStyleSheet(
            f"background:#1a2a1a;color:{FORE};border:1px solid {BORDER};")
        self.cmb_rate.currentTextChanged.connect(self._on_rate_change)
        tb.addWidget(self.cmb_rate)

        tb.addStretch()
        self.lbl_status = QLabel("waiting...")
        self.lbl_status.setStyleSheet(f"color:{AMBER};font-family:monospace;font-size:10px;")
        tb.addWidget(self.lbl_status)
        lay.addLayout(tb)

        # ── Text display ──────────────────────────────────────────────────────
        from PyQt5.QtWidgets import QPlainTextEdit
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setMaximumBlockCount(self._max_rows + 10)
        self.txt.setStyleSheet(
            f"background:#030308;color:{FORE};font-family:monospace;font-size:11px;"
            f"border:1px solid {BORDER};")
        lay.addWidget(self.txt)

        # ── Update timer ──────────────────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.setInterval(200)   # 5 Hz default
        self._timer.timeout.connect(self._update)
        self._last_n = 0    # track how many samples we last saw

    def start(self): self._timer.start()
    def stop(self):  self._timer.stop()

    def _on_pause(self, checked):
        self._paused = checked
        self.btn_pause.setText("▶ RESUME" if checked else "⏸ PAUSE")

    def _clear(self):
        self.txt.clear()
        self._last_n = 0

    def _on_rate_change(self, text):
        hz = int(text.split()[0])
        self._timer.setInterval(1000 // hz)

    def _update(self):
        if self._paused:
            return
        worker = self._get_worker()
        if worker is None or not hasattr(worker, "snapshot"):
            return
        ch_rows = self._get_ch_rows()

        try:
            t_arr, ch_arrs = worker.snapshot(50)  # last 50 samples
        except Exception:
            return

        active = [ch for ch, row in ch_rows.items()
                  if row.chk.isChecked() and len(ch_arrs.get(ch, [])) > 0]
        if not active:
            return

        # Build header if channels changed
        header = "sample | " + " | ".join(
            f"CH{ch}_raw(V)   CH{ch}_calc" for ch in active)

        n_new = len(t_arr)
        if n_new == 0:
            return

        # Grab last min(10, n_new) samples to show
        show = min(10, n_new)
        lines = []
        for i in range(n_new - show, n_new):
            parts = []
            for ch in active:
                raw_v = ch_arrs[ch]
                if i >= len(raw_v):
                    parts.append("     ---          ---    ")
                    continue
                v_raw = float(raw_v[i])
                # Apply equation
                row = ch_rows[ch]
                eq = row.equation
                try:
                    v_in = abs(v_raw) if row.rectify else v_raw
                    clip = row.clip_value
                    if clip >= 0 and v_in > clip:
                        calc = float("nan")
                    else:
                        calc = float(eval(eq, {
                            "np": np, "v": np.array([v_in]),
                            "force_kg": _force_kg,
                            "resistance": _resistance_ohm,
                            "res_mohm": _resistance_mohm,
                            "abs": abs, "sqrt": np.sqrt,
                            "log": np.log, "exp": np.exp,
                        })[0])
                except Exception:
                    calc = v_raw
                parts.append(f"{v_raw:>+10.5f}V  {calc:>12.4g}")
            lines.append(f"{i:>6d} | " + " | ".join(parts))

        block = header + "\n" + "\n".join(lines)

        # Trim display to max_rows
        self.txt.appendPlainText(block)
        sb = self.txt.verticalScrollBar()
        sb.setValue(sb.maximum())

        self.lbl_status.setText(
            f"n={n_new}  active={active}  "
            f"CH{active[0]}_last_raw={float(ch_arrs[active[0]][-1]):.5f}V"
            if active else "no data")


# ── Main window ────────────────────────────────────────────────────────────────
class OscilloscopeWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MCC-128 OSCILLOSCOPE")
        self.resize(1600,950)
        self._settings=GlobalSettings()
        self._filter_states:Dict[int,Optional[BiquadState]]={ch:None for ch in range(8)}
        self._worker:Optional[DAQWorker]=None
        self._worker_thread:Optional[threading.Thread]=None
        self._running=False
        self._y_locked=False; self._locked_ymin=-10.; self._locked_ymax=10.
        self._curves:Dict[int,pg.PlotDataItem]={}
        self._math_curve:Optional[pg.PlotDataItem]=None
        self._trig_line:Optional[pg.InfiniteLine]=None
        self._last_trig_t:Optional[float]=None
        self._display_frozen=False; self._single_captured=False
        # Persistence: store last N rendered arrays per channel
        self._persist_curves:Dict[int,List[pg.PlotDataItem]]={}
        self._persist_on=False; self._persist_depth=8
        # XY mode
        self._xy_mode=False; self._xy_curve:Optional[pg.PlotDataItem]=None
        self._xy_ch_x=0; self._xy_ch_y=2
        # Cursors
        self._cursors_on=False
        self._vcursor1:Optional[pg.InfiniteLine]=None
        self._vcursor2:Optional[pg.InfiniteLine]=None
        self._hcursor1:Optional[pg.InfiniteLine]=None
        self._hcursor2:Optional[pg.InfiniteLine]=None
        self._cursor_label:Optional[QLabel]=None
        # FFT
        self._fft_on=False; self._fft_curves:Dict[int,pg.PlotDataItem]={}

        self._apply_palette()
        self._build_ui()
        self._rebuild_plot_items()

    def _apply_palette(self):
        app=QApplication.instance(); app.setStyle("Fusion")
        pal=QPalette()
        pal.setColor(QPalette.Window,QColor(BG))
        pal.setColor(QPalette.WindowText,QColor(FORE))
        pal.setColor(QPalette.Base,QColor(PANEL_BG))
        pal.setColor(QPalette.Text,QColor(FORE))
        pal.setColor(QPalette.Button,QColor(BTN_BG))
        pal.setColor(QPalette.ButtonText,QColor(FORE))
        pal.setColor(QPalette.Highlight,QColor(AMBER))
        pal.setColor(QPalette.HighlightedText,QColor(BG))
        app.setPalette(pal)

    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central)
        outer=QVBoxLayout(central); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        # ── Top bar ───────────────────────────────────────────────────────────
        topbar=QWidget(); topbar.setFixedHeight(46)
        topbar.setStyleSheet(f"background:{PANEL_BG};border-bottom:1px solid {BORDER};")
        tb=QHBoxLayout(topbar); tb.setContentsMargins(8,4,8,4); tb.setSpacing(6)

        title=QLabel("◈  MCC-128 OSCILLOSCOPE")
        title.setStyleSheet(f"color:{AMBER};font-size:13px;font-weight:bold;font-family:monospace;")
        tb.addWidget(title); tb.addWidget(_sep())

        self.btn_run=QPushButton("▶ RUN"); self.btn_run.setFixedSize(80,30)
        self.btn_run.setStyleSheet(_run_btn_style(False)); self.btn_run.clicked.connect(self._toggle_run)
        tb.addWidget(self.btn_run); tb.addWidget(_sep())

        tb.addWidget(QLabel("TIME/DIV:"))
        self.sld_time=QSlider(Qt.Horizontal); self.sld_time.setRange(1,300)
        self.sld_time.setValue(50); self.sld_time.setFixedWidth(180)
        self.sld_time.setStyleSheet(
            f"QSlider::groove:horizontal{{background:{BORDER};height:4px;}}"
            f"QSlider::handle:horizontal{{background:{FORE};width:12px;height:12px;margin:-4px 0;border-radius:6px;}}")
        self.sld_time.valueChanged.connect(self._on_time_changed); tb.addWidget(self.sld_time)
        self.lbl_time=QLabel("5.0s")
        self.lbl_time.setStyleSheet(f"color:{AMBER};font-family:monospace;min-width:44px;")
        tb.addWidget(self.lbl_time); tb.addWidget(_sep())

        # Y controls
        tb.addWidget(QLabel("Y:"))
        self.btn_fit=QPushButton("FIT"); self.btn_fit.setFixedSize(44,28)
        self.btn_fit.setStyleSheet(_btn_style()); self.btn_fit.clicked.connect(self._autofit_y)
        tb.addWidget(self.btn_fit)
        self.btn_lock=QPushButton("LOCK"); self.btn_lock.setCheckable(True)
        self.btn_lock.setFixedSize(50,28); self.btn_lock.setStyleSheet(_btn_style())
        self.btn_lock.toggled.connect(self._on_lock_y); tb.addWidget(self.btn_lock)
        tb.addWidget(_sep())

        # Feature buttons
        self.btn_cursors=QPushButton("CURSORS"); self.btn_cursors.setCheckable(True)
        self.btn_cursors.setFixedHeight(28); self.btn_cursors.setStyleSheet(_btn_style())
        self.btn_cursors.toggled.connect(self._on_cursors_toggled); tb.addWidget(self.btn_cursors)

        self.btn_persist=QPushButton("PERSIST"); self.btn_persist.setCheckable(True)
        self.btn_persist.setFixedHeight(28); self.btn_persist.setStyleSheet(_btn_style())
        self.btn_persist.toggled.connect(self._on_persist_toggled); tb.addWidget(self.btn_persist)

        self.btn_xy=QPushButton("XY"); self.btn_xy.setCheckable(True)
        self.btn_xy.setFixedHeight(28); self.btn_xy.setStyleSheet(_btn_style())
        self.btn_xy.toggled.connect(self._on_xy_toggled); tb.addWidget(self.btn_xy)

        self.btn_fft=QPushButton("FFT"); self.btn_fft.setCheckable(True)
        self.btn_fft.setFixedHeight(28); self.btn_fft.setStyleSheet(_btn_style())
        self.btn_fft.toggled.connect(self._on_fft_toggled); tb.addWidget(self.btn_fft)

        self.btn_shot=QPushButton("SCREENSHOT"); self.btn_shot.setFixedHeight(28)
        self.btn_shot.setStyleSheet(_btn_style()); self.btn_shot.clicked.connect(self._screenshot)
        tb.addWidget(self.btn_shot); tb.addWidget(_sep())

        # XY channel selectors (hidden by default)
        self._xy_panel=QWidget(); xy_l=QHBoxLayout(self._xy_panel)
        xy_l.setContentsMargins(0,0,0,0); xy_l.setSpacing(3)
        xy_l.addWidget(QLabel("X:"))
        self.cmb_xy_x=QComboBox(); self.cmb_xy_x.addItems([f"CH{i}" for i in range(8)])
        self.cmb_xy_x.setFixedWidth(55); xy_l.addWidget(self.cmb_xy_x)
        xy_l.addWidget(QLabel("Y:"))
        self.cmb_xy_y=QComboBox(); self.cmb_xy_y.addItems([f"CH{i}" for i in range(8)])
        self.cmb_xy_y.setCurrentIndex(2); self.cmb_xy_y.setFixedWidth(55); xy_l.addWidget(self.cmb_xy_y)
        def _xy_x_changed(i):
            self._xy_ch_x = i
            if self._xy_mode:
                pi = self.plot_widget.getPlotItem()
                pi.setLabel("bottom", f"CH{i}", "V", **{"color": FORE})
        def _xy_y_changed(i):
            self._xy_ch_y = i
            if self._xy_mode:
                pi = self.plot_widget.getPlotItem()
                pi.setLabel("left", f"CH{i}", "V", **{"color": FORE})
                if self._xy_curve is not None:
                    self._xy_curve.setSymbolBrush(CHANNEL_COLORS[i % len(CHANNEL_COLORS)])
        self.cmb_xy_x.currentIndexChanged.connect(_xy_x_changed)
        self.cmb_xy_y.currentIndexChanged.connect(_xy_y_changed)
        self._xy_panel.setVisible(False); tb.addWidget(self._xy_panel)

        tb.addStretch()
        lbl_hw=QLabel("◉ HW" if DAQ_AVAILABLE else "◎ MOCK")
        lbl_hw.setStyleSheet(f"color:{'#33ff33' if DAQ_AVAILABLE else RED_WARN};font-family:monospace;")
        tb.addWidget(lbl_hw)
        outer.addWidget(topbar)

        # Cursor delta readout (hidden by default)
        self._cursor_label=QLabel("  dT: ---   dV: ---   1/dT: ---  ")
        self._cursor_label.setStyleSheet(
            f"background:{PANEL_BG};color:{CYAN};font-family:monospace;"
            f"border-bottom:1px solid {BORDER};padding:2px 8px;")
        self._cursor_label.setVisible(False)
        outer.addWidget(self._cursor_label)

        # ── Main area ─────────────────────────────────────────────────────────
        main_h=QHBoxLayout(); main_h.setContentsMargins(0,0,0,0); main_h.setSpacing(0)
        outer.addLayout(main_h,stretch=1)

        self.settings_panel=GlobalSettingsPanel(self._settings)
        self.settings_panel.settings_changed.connect(self._on_settings_changed)
        main_h.addWidget(self.settings_panel)
        div=QFrame(); div.setFrameShape(QFrame.VLine)
        div.setStyleSheet(f"color:{BORDER};"); main_h.addWidget(div)

        right=QWidget(); right_lay=QVBoxLayout(right)
        right_lay.setContentsMargins(0,0,0,0); right_lay.setSpacing(0)
        main_h.addWidget(right,stretch=1)

        # ── Plot area splitter (main | FFT) ───────────────────────────────────
        self._plot_splitter=QSplitter(Qt.Horizontal)
        right_lay.addWidget(self._plot_splitter,stretch=1)

        # Main plot
        self.plot_widget=pg.PlotWidget()
        pi=self.plot_widget.getPlotItem()
        pi.showGrid(x=True,y=True,alpha=0.35)
        pi.getAxis("bottom").setStyle(tickFont=QFont("monospace",8))
        pi.getAxis("left").setStyle(tickFont=QFont("monospace",8))
        pi.setLabel("bottom","Time","s",**{"color":FORE,"font-size":"10px"})
        pi.setLabel("left","Voltage","V",**{"color":FORE,"font-size":"10px"})
        pi.addLegend(offset=(10,10)); pi.getViewBox().setBackgroundColor(BG)
        self.plot_widget.setMouseEnabled(x=True,y=True)
        for gv in np.linspace(-10,10,11):
            pi.addItem(pg.InfiniteLine(pos=gv,angle=0,
                pen=pg.mkPen(color=GRID_COL,width=1,style=Qt.DotLine)))
        self._plot_splitter.addWidget(self.plot_widget)

        # FFT plot (hidden by default)
        self._fft_widget=pg.PlotWidget()
        fpi=self._fft_widget.getPlotItem()
        fpi.showGrid(x=True,y=True,alpha=0.3)
        fpi.setLabel("bottom","Frequency","Hz",**{"color":FORE})
        fpi.setLabel("left","Amplitude","V",**{"color":FORE})
        fpi.setTitle("FFT SPECTRUM",color=AMBER)
        self._plot_splitter.addWidget(self._fft_widget)
        self._fft_widget.setVisible(False)

        # Trigger line
        self._trig_line=pg.InfiniteLine(pos=0.5,angle=0,
            pen=pg.mkPen(color=RED_WARN,width=1,style=Qt.DashLine),
            label="TRIG",labelOpts={"color":RED_WARN,"fill":BG})
        self._trig_line.setVisible(False)
        pi.addItem(self._trig_line)

        # ── Equation info bar ────────────────────────────────────────────────
        eq_bar = QWidget(); eq_bar.setFixedHeight(48)
        eq_bar.setStyleSheet(f"background:#0d1a0d;border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};")
        eq_lay = QVBoxLayout(eq_bar); eq_lay.setContentsMargins(8,3,8,3); eq_lay.setSpacing(1)
        force_str = (f"force_kg(v)  =  {_FORCE_M:.5g} × (v − {_FORCE_C:.5g})    "
                     f"[CH0 diff → kg]")
        res_str = (f"res_ohm(v)  =  ({_RES_K:.5g} × v / ({_RES_VMAX:.5g} - v)) ^ (1/{_RES_N:.5g})    "
                   f"[CH2 diff → Ω]")
        lbl_feq = QLabel(f"⚡ {force_str}")
        lbl_feq.setStyleSheet(f"color:{AMBER};font-family:monospace;font-size:10px;font-weight:bold;")
        lbl_req = QLabel(f"Ω  {res_str}")
        lbl_req.setStyleSheet(f"color:{CYAN};font-family:monospace;font-size:10px;font-weight:bold;")
        eq_lay.addWidget(lbl_feq); eq_lay.addWidget(lbl_req)
        right_lay.addWidget(eq_bar)

        # ── Tabs: channels | measurements ─────────────────────────────────────
        tabs=QTabWidget()
        tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {BORDER};background:{PANEL_BG};}}"
            f"QTabBar::tab{{background:{BTN_BG};color:{FORE};padding:4px 10px;border:1px solid {BORDER};}}"
            f"QTabBar::tab:selected{{background:{BTN_ACT};color:{AMBER};}}")
        tabs.setFixedHeight(256)
        right_lay.addWidget(tabs)

        # Channels tab
        ch_scroll=QScrollArea(); ch_scroll.setWidgetResizable(True)
        ch_scroll.setStyleSheet(f"background:{PANEL_BG};border:none;")
        ch_inner=QWidget(); ch_inner.setStyleSheet(f"background:{PANEL_BG};")
        ch_lay=QVBoxLayout(ch_inner); ch_lay.setSpacing(1); ch_lay.setContentsMargins(4,4,4,4)

        hdr=QHBoxLayout()
        hdr.setContentsMargins(*CH_ROW_MARGINS); hdr.setSpacing(CH_ROW_SPACING)
        for t,w in _CH_COLUMNS:
            l=QLabel(t); l.setFixedWidth(w)
            l.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
            l.setStyleSheet(f"color:{AMBER};font-weight:bold;font-family:monospace;")
            hdr.addWidget(l)
        hdr.addStretch(); ch_lay.addLayout(hdr)

        self.ch_rows:Dict[int,ChannelRow]={}
        for ch in range(8):
            row=ChannelRow(ch,CHANNEL_COLORS[ch])
            row.enable_changed.connect(self._on_enable_changed)
            row.filter_changed.connect(self._on_filter_changed)
            ch_lay.addWidget(row); self.ch_rows[ch]=row

        # Maths row
        self.math_row=MathsRow(); ch_lay.addWidget(self.math_row)
        ch_scroll.setWidget(ch_inner)
        tabs.addTab(ch_scroll,"CHANNELS + MATHS")

        # Measurements tab
        self.meas_panel=MeasPanel()
        tabs.addTab(self.meas_panel,"MEASUREMENTS")

        # Duet motion control tab
        self.duet_panel=DuetControlPanel()
        tabs.addTab(self.duet_panel,"DUET MOTION")

        # Data terminal tab
        self.data_terminal = DataTerminal(
            get_worker_fn  = lambda: self._worker,
            get_ch_rows_fn = lambda: self.ch_rows,
        )
        tabs.addTab(self.data_terminal, "DATA TERMINAL")
        # Start terminal when its tab is selected, stop when leaving
        tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs_widget = tabs

        # ── Save panel ────────────────────────────────────────────────────────
        self.save_panel=SavePanel(self)
        self.save_panel.setFixedHeight(42)
        outer.addWidget(self.save_panel)

        # Timer
        self._plot_timer=QTimer()
        self._plot_timer.setInterval(int(1000/self._settings.refresh_hz))
        self._plot_timer.timeout.connect(self._refresh_plot)

        self.ch_rows[0].chk.setChecked(True)
        self._update_ch_availability()

    # ── Tab switching ────────────────────────────────────────────────────────
    def _on_tab_changed(self, index):
        """Start the data terminal timer only when that tab is visible."""
        tab_widget = self._tabs_widget
        current = tab_widget.widget(index)
        if current is self.data_terminal:
            self.data_terminal.start()
        else:
            self.data_terminal.stop()

    # ── Plot items ────────────────────────────────────────────────────────────
    def _rebuild_plot_items(self):
        self.plot_widget.clear(); self._curves={}; self._fft_curves={}
        self._persist_curves={}; self._math_curve=None; self._xy_curve=None
        pi=self.plot_widget.getPlotItem()
        leg=pi.legend
        if leg: leg.clear()
        for gv in np.linspace(-10,10,11):
            pi.addItem(pg.InfiniteLine(pos=gv,angle=0,
                pen=pg.mkPen(color=GRID_COL,width=1,style=Qt.DotLine)))
        pi.addItem(self._trig_line)
        if not self._xy_mode:
            for ch in range(8):
                if self.ch_rows[ch].enabled:
                    pen=pg.mkPen(color=CHANNEL_COLORS[ch],width=1.5)
                    self._curves[ch]=self.plot_widget.plot(pen=pen,name=f"CH{ch}")
        if self.math_row.enabled:
            self._math_curve=self.plot_widget.plot(
                pen=pg.mkPen(color="#ffffff",width=1.5,style=Qt.DashLine),name="MATH")
        if self._xy_mode:
            self._xy_curve=self.plot_widget.plot(
                pen=None,symbol="o",symbolSize=2,symbolBrush=CHANNEL_COLORS[self._xy_ch_y])
        if self._fft_on:
            for ch in range(8):
                if self.ch_rows[ch].enabled:
                    pen=pg.mkPen(color=CHANNEL_COLORS[ch],width=1)
                    self._fft_curves[ch]=self._fft_widget.plot(pen=pen,name=f"CH{ch}")

    # ── Channel availability ──────────────────────────────────────────────────
    def _update_ch_availability(self):
        diff=self._settings.differential
        for ch in range(8):
            row=self.ch_rows[ch]; unavail=diff and ch%2!=0
            if unavail:
                row.chk.blockSignals(True); row.chk.setChecked(False); row.chk.blockSignals(False)
                row.chk.setEnabled(False); row.set_chk_style(False)
                if ch in self._curves:
                    self.plot_widget.removeItem(self._curves.pop(ch))
                row.clear_noise()
            else:
                row.chk.setEnabled(True); row.set_chk_style(True)
        valid=[ch for ch in range(8) if not(diff and ch%2!=0)]
        sp=self.settings_panel
        sp.cmb_tch.blockSignals(True); cur=sp.cmb_tch.currentIndex()
        sp.cmb_tch.clear(); sp.cmb_tch.addItems([f"CH{c}" for c in valid])
        if cur in valid: sp.cmb_tch.setCurrentText(f"CH{cur}")
        sp.cmb_tch.blockSignals(False)

    # ── Cursor logic ──────────────────────────────────────────────────────────
    def _on_cursors_toggled(self,on:bool):
        self._cursors_on=on
        pi=self.plot_widget.getPlotItem()
        if on:
            if self._vcursor1 is None:
                self._vcursor1=pg.InfiniteLine(pos=-1.,angle=90,movable=True,
                    pen=pg.mkPen(color=CYAN,width=1,style=Qt.DashLine),label="T1",
                    labelOpts={"color":CYAN,"fill":BG,"position":0.95})
                self._vcursor2=pg.InfiniteLine(pos=-0.5,angle=90,movable=True,
                    pen=pg.mkPen(color=AMBER,width=1,style=Qt.DashLine),label="T2",
                    labelOpts={"color":AMBER,"fill":BG,"position":0.85})
                self._hcursor1=pg.InfiniteLine(pos=1.,angle=0,movable=True,
                    pen=pg.mkPen(color=CYAN,width=1,style=Qt.DashLine),label="V1",
                    labelOpts={"color":CYAN,"fill":BG,"position":0.05})
                self._hcursor2=pg.InfiniteLine(pos=-1.,angle=0,movable=True,
                    pen=pg.mkPen(color=AMBER,width=1,style=Qt.DashLine),label="V2",
                    labelOpts={"color":AMBER,"fill":BG,"position":0.15})
            for c in(self._vcursor1,self._vcursor2,self._hcursor1,self._hcursor2):
                pi.addItem(c)
            for c in(self._vcursor1,self._vcursor2,self._hcursor1,self._hcursor2):
                c.sigPositionChanged.connect(self._update_cursor_readout)
            self._cursor_label.setVisible(True)
            self._update_cursor_readout()
            self.btn_cursors.setStyleSheet(_btn_active_style())
        else:
            for c in(self._vcursor1,self._vcursor2,self._hcursor1,self._hcursor2):
                if c and c.scene(): pi.removeItem(c)
            self._cursor_label.setVisible(False)
            self.btn_cursors.setStyleSheet(_btn_style())

    def _update_cursor_readout(self,*_):
        if not all([self._vcursor1,self._vcursor2,self._hcursor1,self._hcursor2]): return
        t1=self._vcursor1.value(); t2=self._vcursor2.value()
        v1=self._hcursor1.value(); v2=self._hcursor2.value()
        dt=abs(t2-t1); dv=abs(v2-v1)
        freq=1./dt if dt>1e-9 else 0.
        self._cursor_label.setText(
            f"  T1={t1:.4f}s  T2={t2:.4f}s  dT={dt:.4f}s  "
            f"1/dT={freq:.2f}Hz  |  V1={v1:.4f}V  V2={v2:.4f}V  dV={dv:.4f}V  ")

    # ── Persistence ───────────────────────────────────────────────────────────
    def _on_persist_toggled(self,on:bool):
        self._persist_on=on
        if not on:
            for ch,curves in self._persist_curves.items():
                for c in curves:
                    try: self.plot_widget.removeItem(c)
                    except: pass
            self._persist_curves={}
        self.btn_persist.setStyleSheet(_btn_active_style()if on else _btn_style())

    def _add_persist(self,ch:int,t:np.ndarray,v:np.ndarray):
        if ch not in self._persist_curves: self._persist_curves[ch]=[]
        alpha_steps=int(200./self._persist_depth)
        color=QColor(CHANNEL_COLORS[ch])
        color.setAlpha(60)
        pen=pg.mkPen(color=color,width=1)
        c=self.plot_widget.plot(x=t,y=v,pen=pen)
        self._persist_curves[ch].append(c)
        while len(self._persist_curves[ch])>self._persist_depth:
            old=self._persist_curves[ch].pop(0)
            try: self.plot_widget.removeItem(old)
            except: pass
        # Fade older curves
        n=len(self._persist_curves[ch])
        for i,curve in enumerate(self._persist_curves[ch]):
            a=int(30+(170*(i/(max(n-1,1)))))
            c2=QColor(CHANNEL_COLORS[ch]); c2.setAlpha(a)
            curve.setPen(pg.mkPen(color=c2,width=1))

    # ── XY mode ───────────────────────────────────────────────────────────────
    def _on_xy_toggled(self,on:bool):
        self._xy_mode=on
        self._xy_panel.setVisible(on)
        pi=self.plot_widget.getPlotItem()
        if on:
            pi.setLabel("bottom",f"CH{self._xy_ch_x}","V",**{"color":FORE})
            pi.setLabel("left",  f"CH{self._xy_ch_y}","V",**{"color":FORE})
            self.btn_xy.setStyleSheet(_btn_active_style())
        else:
            pi.setLabel("bottom","Time","s",**{"color":FORE})
            pi.setLabel("left","Voltage","V",**{"color":FORE})
            self.btn_xy.setStyleSheet(_btn_style())
        self._rebuild_plot_items()

    # ── FFT mode ──────────────────────────────────────────────────────────────
    def _on_fft_toggled(self,on:bool):
        self._fft_on=on
        self._fft_widget.setVisible(on)
        if on:
            self._plot_splitter.setSizes([700,400])
            self.btn_fft.setStyleSheet(_btn_active_style())
        else:
            self._plot_splitter.setSizes([1,0])
            self.btn_fft.setStyleSheet(_btn_style())
        self._rebuild_plot_items()

    def _update_fft(self,ch:int,v:np.ndarray,fs:float):
        if ch not in self._fft_curves: return
        v=v[~np.isnan(v)]
        if len(v)<8: return
        n=len(v); fft_v=np.abs(np.fft.rfft(v))*2./n
        freqs=np.fft.rfftfreq(n,d=1./fs)
        self._fft_curves[ch].setData(x=freqs,y=fft_v)

    # ── Screenshot ────────────────────────────────────────────────────────────
    def _screenshot(self):
        ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        def_name=f"scope_{ts}.png"
        path,_=QFileDialog.getSaveFileName(self,"Save Screenshot",def_name,"PNG (*.png)")
        if not path: return
        try:
            exp=pg.exporters.ImageExporter(self.plot_widget.getPlotItem())
            exp.export(path)
            self.save_panel.lbl_st.setText(f"Screenshot -> {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.warning(self,"Screenshot Error",str(e))

    # ── Settings change ───────────────────────────────────────────────────────
    def _on_settings_changed(self,new_s:GlobalSettings):
        old=self._settings; self._settings=new_s
        if new_s.differential!=old.differential: self._update_ch_availability()
        self._trig_line.setPos(new_s.trigger_thr)
        self._trig_line.setVisible(new_s.trigger_mode!="Free Run")
        if new_s.refresh_hz!=old.refresh_hz:
            self._plot_timer.setInterval(int(1000/new_s.refresh_hz))
        if new_s.buffer_seconds!=old.buffer_seconds and self._worker:
            self._worker.resize_buffers(int(new_s.buffer_seconds*new_s.sample_rate))
            self.sld_time.setRange(1,new_s.buffer_seconds*10)
        volts=_range_volts(new_s.range_label)
        for row in self.ch_rows.values(): row.spin_y.setValue(volts)
        if new_s.sweep_mode!=old.sweep_mode or new_s.trigger_mode!=old.trigger_mode:
            self._last_trig_t=None; self._single_captured=False
            self._display_frozen=new_s.sweep_mode in("Normal","Single")
        if self._running and self._worker:
            self._worker.reconfigure(self._current_channels(),new_s)

    # ── Channel handlers ──────────────────────────────────────────────────────
    def _current_channels(self):
        return[ch for ch in range(8)if self.ch_rows[ch].enabled]

    def _on_enable_changed(self,ch:int,en:bool):
        if en:
            if ch not in self._curves and not self._xy_mode:
                pen=pg.mkPen(color=CHANNEL_COLORS[ch],width=1.5)
                self._curves[ch]=self.plot_widget.plot(pen=pen,name=f"CH{ch}")
            self._filter_states[ch]=make_filter(self.ch_rows[ch].filter_name,self._settings.sample_rate)
        else:
            if ch in self._curves:
                self.plot_widget.removeItem(self._curves.pop(ch))
            self.meas_panel.remove_ch(ch)
            self.ch_rows[ch].clear_noise()
        if self._running and self._worker:
            self._worker.reconfigure(self._current_channels(),self._settings)

    def _on_filter_changed(self,ch:int,name:str):
        self._filter_states[ch]=make_filter(name,self._settings.sample_rate)

    # ── Start/stop ────────────────────────────────────────────────────────────
    def _toggle_run(self):
        if self._running: self._stop()
        else: self._start()

    def _start(self):
        chs=self._current_channels()
        if not chs: QMessageBox.warning(self,"No channels","Enable at least one channel."); return
        s=self.settings_panel.current_settings(); self._settings=s
        for ch in range(8):
            self._filter_states[ch]=make_filter(self.ch_rows[ch].filter_name,s.sample_rate)
        self._last_trig_t=None; self._display_frozen=s.sweep_mode in("Normal","Single")
        self._single_captured=False
        self._rebuild_plot_items()
        self._worker=DAQWorker(s); self._worker.set_channels(chs)
        self._worker.error_signal.connect(self._on_daq_error)
        self._worker.trigger_signal.connect(self._on_trigger)
        self._worker_thread=threading.Thread(target=self._worker.start_daq,daemon=True,name="DAQWorker")
        self._worker_thread.start()
        self._running=True
        self.btn_run.setText("■ STOP"); self.btn_run.setStyleSheet(_run_btn_style(True))
        self._plot_timer.start()

    def _stop(self):
        self._plot_timer.stop()
        if self._worker: self._worker.stop()
        self._running=False
        self.btn_run.setText("▶ RUN"); self.btn_run.setStyleSheet(_run_btn_style(False))

    def _on_daq_error(self,msg:str):
        self._stop(); QMessageBox.critical(self,"DAQ Error",msg)

    def _on_trigger(self,t:float):
        self._last_trig_t=t
        self.settings_panel.lbl_status.setText(f"TRIG @ {t:.3f}s")
        if self._settings.sweep_mode in("Normal","Single"):
            self._display_frozen=False
            QTimer.singleShot(int(1000/self._settings.refresh_hz)+10,self._refreeze)
        if self._settings.sweep_mode=="Single" and not self._single_captured:
            self._single_captured=True
            QTimer.singleShot(200,self._stop)
            self.settings_panel.lbl_status.setText(f"SINGLE @ {t:.3f}s — STOPPED")
        self.save_panel.do_trigger_save(t)

    def _refreeze(self):
        if self._settings.sweep_mode in("Normal","Single"):
            self._display_frozen=True

    # ── Plot refresh ──────────────────────────────────────────────────────────
    @property
    def _time_window_s(self): return self.sld_time.value()/10.

    def _refresh_plot(self):
        if not self._worker: return
        s=self._settings
        if self._display_frozen and s.sweep_mode in("Normal","Single"): return

        n_win=max(10,min(int(self._time_window_s*s.sample_rate),
                         int(s.buffer_seconds*s.sample_rate)))
        t_arr,ch_arrs=self._worker.snapshot(n_win)
        if len(t_arr)<2: return

        # ── XY mode ──────────────────────────────────────────────────────────
        if self._xy_mode and self._xy_curve:
            def _apply_eq_xy(ch, raw):
                row = self.ch_rows[ch]; eq = row.equation
                try:
                    v = np.where(~np.isnan(raw), raw, 0.)
                    if row.rectify: v = np.abs(v)
                    clip = row.clip_value
                    if clip >= 0.0:
                        v = np.where(v > clip, float("nan"), v)
                    out = eval(eq, {"np":np,"v":v,"t":t_arr,"sin":np.sin,"cos":np.cos,
                        "abs":np.abs,"log":np.log,"exp":np.exp,"sqrt":np.sqrt,
                        "force_kg":_force_kg,"resistance":_resistance_ohm,"res_mohm":_resistance_mohm})
                    return np.asarray(out, dtype=float)
                except:
                    return raw
            vx = _apply_eq_xy(self._xy_ch_x, ch_arrs[self._xy_ch_x])
            vy = _apply_eq_xy(self._xy_ch_y, ch_arrs[self._xy_ch_y])
            valid=~(np.isnan(vx)|np.isnan(vy))
            if np.any(valid): self._xy_curve.setData(x=vx[valid],y=vy[valid])
            return

        # ── Time-domain ───────────────────────────────────────────────────────
        if(s.trigger_mode!="Free Run" and self._last_trig_t is not None
                and s.sweep_mode in("Auto","Normal","Single")):
            pre_s=self._time_window_s*(s.pretrig_pct/100.)
            post_s=self._time_window_s-pre_s
            t_rel=t_arr-self._last_trig_t; x_min=-pre_s; x_max=post_s
        else:
            t_rel=t_arr-t_arr[-1]; x_min=-self._time_window_s; x_max=0.

        for ch,curve in self._curves.items():
            row=self.ch_rows[ch]; v_raw=ch_arrs[ch]
            if len(v_raw)==0: continue
            mn=min(len(v_raw),len(t_rel))
            v_raw=v_raw[-mn:]; t_plot=t_rel[-mn:]
            valid=~np.isnan(v_raw)
            if not np.any(valid): curve.setData([],[]); row.clear_noise(); row.clear_offset_live(); continue

            v_work=np.where(valid,v_raw,0.)
            v_filt=apply_filter(row.filter_name,v_work,self._filter_states[ch])
            v_filt[~valid]=float("nan")

            eq=row.equation
            try:
                v=np.abs(v_filt) if row.rectify else v_filt
                # High-end clip: raw samples above threshold become NaN (excluded from plot)
                clip=row.clip_value
                if clip >= 0.0:
                    v=np.where(v > clip, float("nan"), v)
                t=t_plot
                v_out=eval(eq,{"np":np,"v":v,"t":t,"sin":np.sin,"cos":np.cos,
                    "abs":np.abs,"log":np.log,"exp":np.exp,"sqrt":np.sqrt,
                    # Sensor equation shortcuts:
                    # force_kg(v)    — raw CH0 diff → force in kg
                    # resistance(v)  — raw CH2 diff → resistance in Ω
                    # res_mohm(v)    — raw CH2 diff → resistance in MΩ
                    "force_kg":_force_kg,"resistance":_resistance_ohm,"res_mohm":_resistance_mohm})
                v_out=np.asarray(v_out,dtype=float)
            except: v_out=v_filt

            # Apply vertical offset
            v_display=v_out+row.offset
            curve.setData(x=t_plot,y=v_display)

            # Persistence
            if self._persist_on:
                v_clean=v_display.copy(); v_clean[np.isnan(v_clean)]=0.
                self._add_persist(ch,t_plot,v_clean)

            # Measurements + live DC offset
            v_ok=v_out[valid[:len(v_out)]]
            if len(v_ok)>4:
                row.set_noise(float(np.std(v_ok)))
                row.set_offset_live(float(np.mean(v_ok)))
                self.meas_panel.update(ch,v_ok,float(s.sample_rate))

            # FFT
            if self._fft_on:
                self._update_fft(ch,v_ok,float(s.sample_rate))

        # ── Maths channel ─────────────────────────────────────────────────────
        if self.math_row.enabled and self._math_curve:
            ca=self.math_row.ch_a; cb=self.math_row.ch_b; op=self.math_row.op
            va=ch_arrs[ca]; vb=ch_arrs[cb]
            mn=min(len(va),len(vb),len(t_rel))
            va=va[-mn:]; vb=vb[-mn:]; t_m=t_rel[-mn:]
            valid=~(np.isnan(va)|np.isnan(vb))
            if np.any(valid):
                a=np.where(valid,va,0.); b=np.where(valid,vb,0.)
                try:
                    if   op=="A - B":    vm=a-b
                    elif op=="A + B":    vm=a+b
                    elif op=="A * B":    vm=a*b
                    elif op=="A / B":    vm=np.where(np.abs(b)>1e-9,a/b,0.)
                    elif op=="abs(A-B)": vm=np.abs(a-b)
                    else:                vm=a-b
                    vm[~valid]=float("nan")
                    vm_d=vm+self.math_row.spin_off.value()
                    self._math_curve.setData(x=t_m,y=vm_d)
                    self.math_row.lbl.setText(
                        f"pk-pk:{float(np.nanmax(vm)-np.nanmin(vm)):.3f}V  "
                        f"rms:{float(np.sqrt(np.nanmean(vm[valid]**2))):.3f}V")
                except: pass

        # ── Auto Y axis label ─────────────────────────────────────────────────
        _y_label = "Voltage"; _y_unit = "V"
        _res_eqs  = {"resistance", "res_mohm"}
        _force_eqs = {"force_kg"}
        _active_eqs = set()
        _active_vals = []
        for ch, curve in self._curves.items():
            row = self.ch_rows[ch]
            if not row.enabled: continue
            eq_stripped = row.equation.strip().split("(")[0].strip()
            _active_eqs.add(eq_stripped)
            data = curve.getData()
            if data is not None and data[1] is not None and len(data[1]) > 0:
                vals = data[1][~np.isnan(data[1])]
                if len(vals) > 0:
                    _active_vals.append(float(np.median(np.abs(vals))))
        if _active_eqs & _res_eqs:
            med = float(np.median(_active_vals)) if _active_vals else 0.0
            if med >= 1e6:
                _y_label = "Resistance"; _y_unit = "MΩ"
            elif med >= 1e3:
                _y_label = "Resistance"; _y_unit = "kΩ"
            else:
                _y_label = "Resistance"; _y_unit = "Ω"
        elif _active_eqs & _force_eqs:
            _y_label = "Force"; _y_unit = "kg"
        pi = self.plot_widget.getPlotItem()
        _cur = pi.getAxis("left").labelText
        if _cur != _y_label:
            pi.setLabel("left", _y_label, _y_unit, **{"color": FORE, "font-size": "10px"})

        self.plot_widget.setXRange(x_min,x_max,padding=0.)
        if self._y_locked:
            self.plot_widget.setYRange(self._locked_ymin,self._locked_ymax,padding=0)

    def _on_time_changed(self,v): self.lbl_time.setText(f"{v/10.:.1f}s")

    def _autofit_y(self):
        if not self._worker: return
        _,ch=self._worker.snapshot(int(self._time_window_s*self._settings.sample_rate))
        vals=[]
        for c in self._curves:
            v=ch[c]; v=v[~np.isnan(v)]
            if len(v): vals.extend((v+self.ch_rows[c].offset).tolist())
        if not vals: return
        mn,mx=float(np.nanmin(vals)),float(np.nanmax(vals))
        pad=max(0.05*(mx-mn),0.05); ylo,yhi=mn-pad,mx+pad
        if self._y_locked: self._locked_ymin=ylo; self._locked_ymax=yhi
        self.plot_widget.getPlotItem().getViewBox().disableAutoRange()
        self.plot_widget.setYRange(ylo,yhi,padding=0)

    def _on_lock_y(self,locked:bool):
        self._y_locked=locked
        if locked:
            vr=self.plot_widget.getPlotItem().getViewBox().viewRange()
            self._locked_ymin=vr[1][0]; self._locked_ymax=vr[1][1]
            self.plot_widget.setMouseEnabled(x=True,y=False)
            self.plot_widget.getPlotItem().getViewBox().disableAutoRange()
            self.plot_widget.setYRange(self._locked_ymin,self._locked_ymax,padding=0)
            self.btn_lock.setStyleSheet(_btn_active_style())
        else:
            self.plot_widget.setMouseEnabled(x=True,y=True)
            self.plot_widget.getPlotItem().getViewBox().enableAutoRange(axis=1)
            self.btn_lock.setStyleSheet(_btn_style())

    def wheelEvent(self,event):
        d=event.angleDelta().y(); cur=self.sld_time.value(); step=max(1,cur//10)
        self.sld_time.setValue(max(1,cur-step)if d>0 else min(self.sld_time.maximum(),cur+step))
        event.accept()

    def closeEvent(self,event):
        self._stop()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
        event.accept()


# ── Helpers ────────────────────────────────────────────────────────────────────
def _sep():
    f=QFrame(); f.setFrameShape(QFrame.VLine)
    f.setStyleSheet(f"color:{BORDER};"); f.setFixedWidth(2); return f

def _btn_style():
    return(f"background:{BTN_BG};color:{FORE};font-weight:bold;"
           f"border:1px solid {BORDER};font-family:monospace;")

def _btn_active_style():
    return(f"background:{BTN_ACT};color:{AMBER};font-weight:bold;"
           f"border:1px solid {AMBER};font-family:monospace;")

def _run_btn_style(running:bool):
    if running:
        return(f"background:#5a1a1a;color:{RED_WARN};font-weight:bold;"
               f"border:1px solid {RED_WARN};font-family:monospace;")
    return(f"background:{BTN_ACT};color:{FORE};font-weight:bold;"
           f"border:1px solid {FORE};font-family:monospace;")

# ── Entry ──────────────────────────────────────────────────────────────────────
def main():
    here=os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path: sys.path.insert(0,here)
    app=QApplication(sys.argv)
    win=OscilloscopeWindow()
    win.show()
    sys.exit(app.exec_())

if __name__=="__main__":
    main()
