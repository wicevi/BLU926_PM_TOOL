"""Service layer for the BLU926 power profiler.

Owns the device session (connect / reconnect / control lock), background
monitoring (wall-clock-timestamped ms buckets + ring buffer + JSONL log),
one-shot measurement, raw capture, low-power analysis and safety guards.

Design notes (verified on real hardware, see docs/AGENTS.md):
- This is a BLU926 (20K-class spec; only the BLU939 does 100k). Sustained upload
  measures ~16.5k sample frames/s (4 bytes each) with bursty USB delivery, so
  the vendor BluClient timestamps/RateAdjuster (100k-based) must not be used
  for time math. We timestamp every serial chunk by sample count at the
  per-session measured rate, re-synced to the wall clock (see _chunk_timeline).
- Current unit is uA everywhere in machine fields; human strings via fmt_ua().
"""

import json
import math
import os
import statistics
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from . import driver

VID, PID = driver.DEFAULT_VID, driver.DEFAULT_PID
VDD_MIN_MV = 500
VDD_MAX_MV = 5000
# Measured sustained upload rate of a BLU926 (20K-class spec; bursty USB).
# Only bridges the first moments of a session - the real rate is always
# measured per session, never hard-assumed.
FALLBACK_FPS = 16500.0
RECORD_DIR = os.environ.get(
    "BLU926_RECORD_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings"),
)


class ToolError(Exception):
    """Raised for expected, user-actionable failures (message -> error, hint)."""

    def __init__(self, error: str, hint: str = ""):
        super().__init__(error)
        self.error = error
        self.hint = hint


# --------------------------------------------------------------------------- #
# statistics helpers (pure python, no numpy)
# --------------------------------------------------------------------------- #

def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def describe_ua(vals: List[float]) -> Dict[str, Any]:
    """Summary statistics over a list of uA samples."""
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    med = _percentile(s, 50)
    out = {
        "n": len(vals),
        "avg_uA": statistics.fmean(vals),
        "median_uA": med,
        "min_uA": s[0],
        "max_uA": s[-1],
        "p01_uA": _percentile(s, 1),
        "p99_uA": _percentile(s, 99),
        "stdev_uA": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
    }
    out["avg"] = fmt_ua(out["avg_uA"])
    out["median"] = fmt_ua(med)
    out["min"] = fmt_ua(s[0])
    out["max"] = fmt_ua(s[-1])
    return out


def fmt_ua(ua: float) -> str:
    x = abs(ua)
    if x >= 1e6:
        return f"{ua / 1e6:.3f}A"
    if x >= 1e3:
        return f"{ua / 1e3:.3f}mA"
    if x >= 1.0:
        return f"{ua:.2f}uA"
    if x >= 1e-3:
        return f"{ua * 1e3:.1f}nA"
    return f"{ua * 1e6:.1f}pA"


def charge_uAh_from_sums(sum_uA_frames: float, fps: float) -> float:
    """sum(uA per frame) / frames-per-second => uA*s => uAh."""
    if fps <= 0:
        return 0.0
    return (sum_uA_frames / fps) / 3600.0


def _bucket(values: List[float], t_start_ms: float, t_end_ms: float) -> Dict[str, float]:
    """Aggregate one millisecond bucket from values+times (parallel lists)."""
    s = sum(values)
    return {
        "t_ms": (t_start_ms + t_end_ms) / 2.0,
        "avg": s / len(values),
        "min": min(values),
        "max": max(values),
        "sum": s,
        "n": len(values),
    }


def _chunk_timeline(last_ms: Optional[float], arrival_ms: float, k: int,
                    fps: float) -> Tuple[float, float]:
    """Assign a (t0, t1) ms span to a chunk of k samples.

    The device uploads in bursts (up to ~128 ms of data at once), so arrival
    time alone clusters chunks. We derive the span from the sample count at
    the measured fps, but never let it run behind the previous chunk (keeps
    the timeline continuous) and re-sync to arrival time whenever the stream
    was idle (keeps absolute drift bounded)."""
    span = k / fps * 1000.0 if k else 0.0
    t0 = arrival_ms - span
    if last_ms is not None and t0 < last_ms:
        t0 = last_ms
    return t0, t0 + span


# --------------------------------------------------------------------------- #
# background monitor session
# --------------------------------------------------------------------------- #

class MonitorSession:
    """Streams samples, aggregates into 1 ms buckets, keeps a ring + JSONL log."""

    def __init__(self, dev: driver.BLU2_MP, max_buffer_s: float, log_path: Optional[str]):
        self.dev = dev
        self.max_buffer_s = float(max_buffer_s)
        self.log_path = log_path
        self.ring: deque = deque(maxlen=int(max_buffer_s * 1000))
        self.samples_total = 0
        self.buckets_total = 0
        self.sum_uA_frames = 0.0  # sum over every raw sample (for charge)
        self.fps = FALLBACK_FPS
        self.started_at = time.time()
        self.error: Optional[str] = None
        self._quit = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="blu926-monitor")
        self._log_fh = open(log_path, "a", encoding="utf-8") if log_path else None
        if self._log_fh:
            self._log_fh.write(json.dumps({
                "type": "header", "ts": time.time(), "max_buffer_s": self.max_buffer_s,
            }) + "\n")
        self._thread.start()

    # -- worker ------------------------------------------------------------- #
    def _run(self):
        cur_t0_ms: Optional[float] = None
        cur_vals: List[float] = []
        cur_min = math.inf
        cur_max = -math.inf
        last_ms: Optional[float] = None
        cum = 0
        t_start = time.time()
        last_flush = time.time()
        try:
            while not self._quit.is_set():
                buf = self.dev.get_data()
                if not buf:
                    # flush stale bucket even without new data (idle device)
                    if cur_t0_ms is not None and time.time() - last_flush > 0.25:
                        self._flush(cur_t0_ms, cur_vals, cur_min, cur_max)
                        cur_t0_ms, cur_vals = None, []
                        cur_min, cur_max = math.inf, -math.inf
                        last_flush = time.time()
                    time.sleep(0.002)
                    continue
                arrival_ms = time.time() * 1000.0
                vals, _bits = self.dev.get_samples(buf)
                k = len(vals)
                if k:
                    cum += k
                    elapsed = time.time() - t_start
                    if cum >= 5000 and elapsed > 0.1:
                        self.fps = cum / elapsed  # global average: burst-robust
                    t0, t1 = _chunk_timeline(last_ms, arrival_ms, k, self.fps)
                    last_ms = t1
                    step = (t1 - t0) / k
                    for i, v in enumerate(vals):
                        t_ms = t0 + (i + 1) * step
                        self.sum_uA_frames += v
                        if cur_t0_ms is None:
                            cur_t0_ms = t_ms
                            cur_min, cur_max = v, v
                            cur_vals = [v]
                        else:
                            bucket = int(t_ms)
                            if bucket == int(cur_t0_ms):
                                cur_vals.append(v)
                                if v < cur_min:
                                    cur_min = v
                                if v > cur_max:
                                    cur_max = v
                            else:
                                self._flush(cur_t0_ms, cur_vals, cur_min, cur_max)
                                cur_t0_ms, cur_vals = t_ms, [v]
                                cur_min, cur_max = v, v
                    self.samples_total += k
                if time.time() - last_flush > 0.05 and cur_t0_ms is not None:
                    self._flush(cur_t0_ms, cur_vals, cur_min, cur_max)
                    cur_t0_ms, cur_vals = None, []
                    cur_min, cur_max = math.inf, -math.inf
                    last_flush = time.time()
        except Exception as e:  # keep tool layer able to report it
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._close_log()

    def _flush(self, t0_ms: float, vals: List[float], vmin: float, vmax: float):
        if not vals:
            return
        rec = _bucket(vals, t0_ms, t0_ms + 1.0)
        rec["min"], rec["max"] = vmin, vmax
        self.ring.append(rec)
        self.buckets_total += 1
        if self._log_fh:
            self._log_fh.write(json.dumps({
                "type": "ms", "t": round(rec["t_ms"], 3), "avg": round(rec["avg"], 4),
                "min": round(rec["min"], 4), "max": round(rec["max"], 4),
                "sum": round(rec["sum"], 4), "n": rec["n"],
            }) + "\n")

    def _close_log(self):
        if self._log_fh:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    # -- api ---------------------------------------------------------------- #
    def stop(self):
        self._quit.set()
        self._thread.join(timeout=3.0)
        self._close_log()

    def window(self, duration_ms: Optional[float]) -> List[Dict[str, float]]:
        if duration_ms is None or duration_ms <= 0:
            return list(self.ring)
        cutoff = time.time() * 1000.0 - float(duration_ms)
        items = list(self.ring)
        return [r for r in items if r["t_ms"] >= cutoff]


# --------------------------------------------------------------------------- #
# main service
# --------------------------------------------------------------------------- #

class Blu926Service:
    def __init__(self, port: Optional[str] = None):
        self._port_hint = port or os.environ.get("BLU926_PORT") or None
        self.dev: Optional[driver.BLU2_MP] = None
        self.info: Dict[str, str] = {}
        self._ctl_lock = threading.RLock()
        self._sess_lock = threading.Lock()
        self._monitor: Optional[MonitorSession] = None
        self._monitor_log: Optional[str] = None
        self._state: Dict[str, Any] = {"power": "unknown", "voltage_mv": None}

    # -- connection --------------------------------------------------------- #
    def ensure_open(self) -> driver.BLU2_MP:
        if self.dev is not None:
            return self.dev
        if self._port_hint:
            port = self._port_hint
        else:
            devs = driver.find_all()
            if not devs:
                import serial.tools.list_ports as lp
                others = [p.device for p in lp.comports()]
                raise ToolError(
                    "no BLU device found (VID 0x15A2 / PID 0x300A)",
                    "check USB cable/power and the driver; COM ports visible now: "
                    + (", ".join(others) or "none"),
                )
            port = devs[0]["port"]
        try:
            dev = driver.BLU2_MP(port, buffer_max_size_seconds=2, buffer_chunk_seconds=0.01)
        except Exception as e:
            raise ToolError(
                f"cannot open serial port {port}: {e}",
                "close any other program using the port (vendor GUI, serial monitor), "
                "then retry; or set BLU926_PORT to the right COM port",
            )
        ok = dev.get_modifiers()
        if not ok:
            try:
                dev.ser.close()
            except Exception:
                pass
            raise ToolError(
                f"opened {port} but device did not answer GET_META_DATA",
                "retry once (USB CDC may need a moment); if it persists, replug USB",
            )
        self.dev = dev
        self._port_used = port
        self.info = {"port": port, "serial": self._sn_of(port)}
        return dev

    def _sn_of(self, port: str) -> str:
        for d in driver.find_all():
            if d["port"] == port:
                return d.get("serial", "")
        return ""

    def _reconnect(self):
        self._close_dev()
        time.sleep(0.3)
        self.ensure_open()

    def _close_dev(self):
        if self.dev is not None:
            # silence the vendor __del__, which writes to the already-closed port
            try:
                self.dev._write_serial = lambda cmd: None
                self.dev.ser.close()
            except Exception:
                pass
            self.dev = None

    def _control(self, fn):
        """Run a control op with one reconnect-retry on serial failure."""
        with self._ctl_lock:
            try:
                return fn()
            except ToolError:
                raise
            except Exception as e:
                try:
                    self._reconnect()
                except ToolError:
                    raise ToolError(f"serial error: {e}", "device may be unplugged; replug and retry")
                return fn()

    # -- info / doctor ------------------------------------------------------ #
    def device_info(self) -> Dict[str, Any]:
        dev = self.ensure_open()
        r = dev.modifiers.get("R", {})
        return {
            "ok": True,
            "port": self._port_of_dev(),
            "serial_number": self._sn_of(self._port_of_dev()),
            "calibrated_flag": dev.modifiers.get("Calibrated"),
            "shunt_R": {k: r.get(k) for k in sorted(r)},
            "saved_vdd_mv": dev.saved_vdd_mv if hasattr(dev, "saved_vdd_mv") else None,
            "tracked_power": self._state["power"],
            "tracked_voltage_mv": self._state["voltage_mv"],
            "monitor_running": self.monitor_running(),
            "hint": "tracked_* fields are host-side state; saved_vdd_mv is what the device firmware last saved",
        }

    def _port_of_dev(self) -> str:
        return getattr(self, "_port_used", None) or self._port_hint or ""

    def monitor_running(self) -> bool:
        return self._monitor is not None and self._monitor._thread.is_alive()

    # -- control ------------------------------------------------------------ #
    def set_voltage(self, mv: int) -> Dict[str, Any]:
        max_mv = int(os.environ.get("BLU926_MAX_MV", VDD_MAX_MV))
        try:
            mv = int(round(float(mv)))
        except (TypeError, ValueError):
            raise ToolError(f"invalid voltage {mv!r}", "pass millivolts as an integer, e.g. 3300")
        if mv < VDD_MIN_MV:
            raise ToolError(
                f"requested {mv} mV is below the hardware minimum {VDD_MIN_MV} mV",
                f"choose a value between {VDD_MIN_MV} and {max_mv} mV",
            )
        if mv > max_mv:
            raise ToolError(
                f"requested {mv} mV exceeds the policy cap BLU926_MAX_MV={max_mv} mV",
                "lower the request, or raise BLU926_MAX_MV (hardware max 5000 mV) if the DUT allows it",
            )
        dev = self.ensure_open()

        def op():
            dev.set_source_voltage(mv)

        self._control(op)
        self._state["voltage_mv"] = mv
        return {
            "ok": True,
            "voltage_mv": mv,
            "voltage": f"{mv / 1000.0:.3f}V",
            "hint": "use blu926_power_set to apply it to the DUT, then blu926_current_read to verify",
        }

    def set_power(self, on: bool) -> Dict[str, Any]:
        dev = self.ensure_open()

        def op():
            dev.toggle_DUT_power(bool(on))

        self._control(op)
        self._state["power"] = "on" if on else "off"
        out = {"ok": True, "power": self._state["power"], "voltage_mv": self._state["voltage_mv"]}
        if on:
            out["hint"] = ("power is on; measure the DUT with blu926_current_read, or start a "
                           "session with blu926_monitor_start before running the firmware")
        else:
            out["hint"] = "power is off; a quick blu926_current_read should now show ~0 uA"
        return out

    # -- sampling sessions -------------------------------------------------- #
    @staticmethod
    def _reset_filter_state(dev: driver.BLU2_MP):
        """The vendor spike-filter EMA keeps state across sessions; reset it so a
        new session does not start with the previous session's current level."""
        dev.rolling_avg = None
        dev.rolling_avg4 = None
        dev.prev_range = None
        dev.after_spike = 0
        dev.consecutive_range_samples = 0

    def _collect(self, dev: driver.BLU2_MP, settle_ms: float, duration_ms: float
                 ) -> Tuple[List[float], float, List[Tuple[List[float], float, float]]]:
        """Run one start->settle->collect->stop window.

        Returns (uA list, fps, timeline) where timeline is a list of
        (chunk, t0_ms, t1_ms) with a continuous, burst-robust time axis.
        settle_ms data is discarded (device backlog is drained), so the stats
        window excludes the turn-on transient and the spike-filter's EMA
        convergence samples. fps is computed inside the window only, which
        matches the device's true ~16.5k frames/s upload rate."""
        self._reset_filter_state(dev)
        dev.start_measuring()
        try:
            if settle_ms > 0:
                time.sleep(settle_ms / 1000.0)
                while not dev.get_data():     # wait until at least one chunk is pending
                    time.sleep(0.002)
                deadline = time.time() + 0.5
                while dev.get_data() and time.time() < deadline:
                    pass                       # drain the settle-period backlog
                time.sleep(0.02)
                while dev.get_data():          # catch the in-flight chunk
                    pass
            recs: List[Tuple[List[float], float]] = []
            t0 = time.time()
            t_end = t0 + duration_ms / 1000.0
            while time.time() < t_end:
                buf = dev.get_data()
                if not buf:
                    time.sleep(0.002)
                    continue
                chunk, _bits = dev.get_samples(buf)
                if chunk:
                    recs.append((chunk, time.time() * 1000.0))
            elapsed = max(time.time() - t0, 1e-6)
        finally:
            try:
                dev.stop_measuring()
            except Exception:
                pass
        total = sum(len(c) for c, _ in recs)
        fps = total / elapsed if (elapsed > 0 and total > 50) else FALLBACK_FPS
        vals: List[float] = []
        timeline: List[Tuple[List[float], float, float]] = []
        last_ms: Optional[float] = None
        for chunk, arrival_ms in recs:
            a, b = _chunk_timeline(last_ms, arrival_ms, len(chunk), fps)
            last_ms = b
            timeline.append((chunk, a, b))
            vals.extend(chunk)
        return vals, fps, timeline

    def current_read(self, settle_ms: int = 200, duration_ms: int = 500) -> Dict[str, Any]:
        if self.monitor_running():
            raise ToolError(
                "a monitor session is streaming",
                "use blu926_monitor_read instead, or blu926_monitor_stop first",
            )
        with self._sess_lock:
            dev = self.ensure_open()
            try:
                vals, fps, _tl = self._collect(dev, settle_ms, duration_ms)
            except Exception as e:
                raise ToolError(f"sampling failed: {e}", "retry; if it repeats, replug USB")
            if not vals:
                raise ToolError(
                    "no samples received",
                    "check that no other program holds the COM port; run blu926_doctor",
                )
            out = describe_ua(vals)
            out.update({
                "ok": True,
                "power": self._state["power"],
                "voltage_mv": self._state["voltage_mv"],
                "sample_rate_hz": round(fps, 1),
                "settle_ms": settle_ms,
                "duration_ms": duration_ms,
            })
            return out

    def capture(self, duration_s: float = 5.0, settle_ms: int = 200) -> Dict[str, Any]:
        max_s = float(os.environ.get("BLU926_MAX_CAPTURE_S", 60))
        if self.monitor_running():
            raise ToolError("a monitor session is streaming", "use blu926_monitor_stop first")
        duration_s = float(duration_s)
        if duration_s <= 0 or duration_s > max_s:
            raise ToolError(
                f"duration {duration_s}s out of range",
                f"capture between 0.1 and {max_s:g}s (BLU926_MAX_CAPTURE_S)",
            )
        with self._sess_lock:
            dev = self.ensure_open()
            os.makedirs(RECORD_DIR, exist_ok=True)
            base = time.strftime("cap_%Y%m%d_%H%M%S")
            csv_path = os.path.join(RECORD_DIR, base + ".csv")
            meta_path = os.path.join(RECORD_DIR, base + ".json")
            try:
                vals, fps, timeline = self._collect(dev, settle_ms, duration_s * 1000.0)
            except Exception as e:
                raise ToolError(f"sampling failed: {e}", "retry; if it repeats, replug USB")

            # write CSV from the continuous, burst-robust timeline
            sum_uA_frames = 0.0
            with open(csv_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("t_ms,ua\n")
                for chunk, a, b in timeline:
                    k = len(chunk)
                    if not k:
                        continue
                    step = (b - a) / k
                    for i, v in enumerate(chunk):
                        sum_uA_frames += v
                        fh.write(f"{a + (i + 1) * step:.3f},{v:.4f}\n")
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "type": "capture", "ts": time.time(), "port": self._port_of_dev(),
                    "sample_rate_hz": round(fps, 1), "voltage_mv": self._state["voltage_mv"],
                    "power": self._state["power"], "settle_ms": settle_ms,
                    "stats": describe_ua(vals),
                    "charge_uAh": round(charge_uAh_from_sums(sum_uA_frames, fps), 6),
                }, fh, indent=2)
            out = describe_ua(vals)
            out.update({
                "ok": True,
                "sample_rate_hz": round(fps, 1),
                "charge_uAh": round(charge_uAh_from_sums(sum_uA_frames, fps), 6),
                "csv": csv_path,
                "meta": meta_path,
                "hint": "raw data saved; run blu926_analyze with source=csv path for low-power metrics",
            })
            return out

    # -- monitor ------------------------------------------------------------ #
    def monitor_start(self, max_buffer_s: float = 120.0, log: bool = True) -> Dict[str, Any]:
        if self.monitor_running():
            return {
                "ok": True,
                "already_running": True,
                **self.monitor_status(),
                "hint": "session continues; use blu926_monitor_read / blu926_monitor_wait_for",
            }
        if self._monitor is not None:  # finished but not cleaned
            self._monitor.stop()
            self._monitor = None
        with self._sess_lock:
            dev = self.ensure_open()
            if log:
                os.makedirs(RECORD_DIR, exist_ok=True)
                self._monitor_log = os.path.join(
                    RECORD_DIR, "mon_" + time.strftime("%Y%m%d_%H%M%S") + ".jsonl")
            else:
                self._monitor_log = None
            try:
                self._reset_filter_state(dev)
                dev.start_measuring()
            except Exception as e:
                raise ToolError(f"cannot start sampling: {e}", "run blu926_doctor; replug USB if needed")
            self._monitor = MonitorSession(dev, max_buffer_s, self._monitor_log)
            self._state["monitor_started_at"] = time.time()
            return {
                "ok": True,
                "running": True,
                "max_buffer_s": max_buffer_s,
                "log": self._monitor_log,
                "hint": ("samples are aggregated into 1 ms buckets; read with blu926_monitor_read, "
                         "wait for events with blu926_monitor_wait_for, finish with blu926_monitor_stop"),
            }

    def monitor_status(self) -> Dict[str, Any]:
        m = self._monitor
        running = self.monitor_running()
        out: Dict[str, Any] = {
            "ok": True,
            "running": running,
            "voltage_mv": self._state["voltage_mv"],
            "power": self._state["power"],
        }
        if m is None:
            out["hint"] = "no monitor session; start one with blu926_monitor_start"
            return out
        out.update({
            "elapsed_s": round(time.time() - m.started_at, 2),
            "buckets": m.buckets_total,
            "samples": m.samples_total,
            "sample_rate_hz": round(m.fps, 1),
            "ring_seconds": round(len(m.ring) / 1000.0, 2),
            "max_buffer_s": m.max_buffer_s,
            "log": m.log_path,
        })
        if m.error:
            out["ok"] = False
            out["error"] = m.error
            out["hint"] = "monitor thread crashed; run blu926_monitor_stop then blu926_doctor"
        return out

    def monitor_read(self, duration_ms: Optional[float] = None, max_points: int = 300,
                     include_trace: bool = True) -> Dict[str, Any]:
        m = self._monitor
        if m is None:
            raise ToolError(
                "no monitor session", "start one with blu926_monitor_start, or use blu926_current_read for a one-shot value")
        recs = m.window(duration_ms)
        if not recs:
            return {
                "ok": True, "empty": True, "running": self.monitor_running(),
                "hint": "no data yet in the ring; wait a bit and read again",
            }
        out = self._summarize_buckets(recs)
        out["ok"] = True
        out["running"] = self.monitor_running()
        if include_trace:
            out["trace"] = self._downsample(recs, max_points)
        return out

    def monitor_wait_for(self, condition: str, threshold_ua: Optional[float] = None,
                         tolerance_pct: float = 5.0, window_ms: float = 200.0,
                         timeout_s: float = 30.0) -> Dict[str, Any]:
        m = self._monitor
        if m is None:
            raise ToolError(
                "no monitor session",
                "blu926_monitor_wait_for needs a running session: blu926_monitor_start first",
            )
        cond = (condition or "").strip().lower()
        if cond not in ("above", "below", "stable"):
            raise ToolError(
                f"unknown condition {condition!r}",
                "supported: 'above' (avg > threshold_ua), 'below' (avg < threshold_ua), "
                "'stable' (max-min within tolerance_pct of median over window_ms)",
            )
        if cond in ("above", "below") and threshold_ua is None:
            raise ToolError("threshold_ua is required for above/below", "pass e.g. threshold_ua=100")
        window_ms = max(50.0, float(window_ms))
        t0 = time.time()
        last_eval: Dict[str, Any] = {}
        while time.time() - t0 < timeout_s:
            recs = m.window(window_ms)
            if recs:
                avgs = [r["avg"] for r in recs]
                mn = min(r["min"] for r in recs)
                mx = max(r["max"] for r in recs)
                med = describe_ua(avgs)["median_uA"]
                avg = statistics.fmean(avgs)
                if cond == "above":
                    hit = avg > float(threshold_ua)
                    val = avg
                elif cond == "below":
                    hit = avg < float(threshold_ua)
                    val = avg
                else:
                    span = mx - mn
                    floor = max(0.2, abs(med) * float(tolerance_pct) / 100.0)
                    hit = span <= floor
                    val = span
                last_eval = {"value": round(val, 4), "window_min": round(mn, 4),
                             "window_max": round(mx, 4), "window_avg": round(avg, 4)}
                if hit:
                    return {
                        "ok": True, "triggered": True, "condition": cond,
                        "elapsed_s": round(time.time() - t0, 3), **last_eval,
                    }
            time.sleep(0.05)
        return {
            "ok": True, "triggered": False, "condition": cond, "timeout_s": timeout_s,
            "elapsed_s": round(time.time() - t0, 3), **last_eval,
            "hint": "condition not met before timeout; adjust threshold/window or inspect with blu926_monitor_read",
        }

    def monitor_stop(self) -> Dict[str, Any]:
        m = self._monitor
        if m is None:
            return {"ok": True, "running": False, "hint": "no monitor session was running"}
        was_running = self.monitor_running()
        m.stop()
        self._monitor = None
        recs = list(m.ring)
        out: Dict[str, Any] = {"ok": True, "was_running": was_running}
        if recs:
            out.update(self._summarize_buckets(recs))
            fps = m.fps
            charge = charge_uAh_from_sums(m.sum_uA_frames, fps)
            out["charge_uAh"] = round(charge, 6)
            out["charge_mAh"] = round(charge / 1000.0, 9)
        out["log"] = m.log_path
        out["sample_rate_hz"] = round(m.fps, 1)
        out["dropped_note"] = (
            f"ring kept the last {m.max_buffer_s:g}s; full data is in the JSONL log" if m.log_path
            else f"ring kept the last {m.max_buffer_s:g}s only (log was disabled)")
        if m.error:
            out["ok"] = False
            out["error"] = m.error
        return out

    # -- analysis ----------------------------------------------------------- #
    def analyze(self, source: str = "monitor", path: Optional[str] = None,
                sleep_threshold_ua: Optional[float] = None) -> Dict[str, Any]:
        recs: List[Dict[str, float]] = []
        src = (source or "monitor").lower()
        if src == "monitor":
            m = self._monitor
            if m is None or not m.ring:
                raise ToolError(
                    "no monitor data to analyze",
                    "start a session with blu926_monitor_start, or analyze a saved log/csv via source='jsonl'/'csv' with path",
                )
            recs = list(m.ring)
        elif src in ("jsonl", "file", "log"):
            recs = self._load_jsonl(path)
        elif src == "csv":
            recs = self._load_csv(path)
        else:
            raise ToolError(
                f"unknown source {source!r}",
                "use 'monitor' (live ring), 'jsonl' (monitor log), or 'csv' (capture file) with path",
            )
        if not recs:
            raise ToolError("selected source has no data", "check the path / run a longer capture")

        out = self._summarize_buckets(recs)
        out["ok"] = True
        out["source"] = src
        if path:
            out["path"] = path
        out["duration_s"] = round((recs[-1]["t_ms"] - recs[0]["t_ms"]) / 1000.0, 3)
        charge = sum(r["sum"] for r in recs) / (self._fps_for(recs) * 3600.0)
        out["charge_uAh"] = round(charge, 6)
        if sleep_threshold_ua is not None:
            th = float(sleep_threshold_ua)
            sleep_avgs = [r["avg"] for r in recs if r["max"] < th]
            active = [r for r in recs if r["max"] >= th]
            out["threshold_ua"] = th
            out["sleep_bucket_fraction"] = round(len(sleep_avgs) / len(recs), 4)
            out["sleep_current"] = describe_ua(sleep_avgs) if sleep_avgs else None
            out["active_bucket_fraction"] = round(len(active) / len(recs), 4)
            if active:
                pk = self._top_peaks(active)
                out["active_peak"] = fmt_ua(max(r["max"] for r in active))
                out["peaks"] = pk
        return out

    def _fps_for(self, recs: List[Dict[str, float]]) -> float:
        if self._monitor is not None and self.monitor_running():
            return self._monitor.fps
        n = sum(r["n"] for r in recs)
        span_s = max((recs[-1]["t_ms"] - recs[0]["t_ms"]) / 1000.0, 1e-3)
        est = n / span_s
        return est if est > 100 else FALLBACK_FPS

    @staticmethod
    def _top_peaks(active_recs: List[Dict[str, float]], top: int = 5,
                   gap_ms: float = 50.0) -> List[Dict[str, Any]]:
        peaks: List[Dict[str, Any]] = []
        for r in sorted(active_recs, key=lambda x: -x["max"]):
            if any(abs(r["t_ms"] - p["t_ms"]) < gap_ms for p in peaks):
                continue
            peaks.append({"t_ms": round(r["t_ms"], 1), "max_uA": round(r["max"], 2),
                          "max": fmt_ua(r["max"])})
            if len(peaks) >= top:
                break
        return peaks

    @staticmethod
    def _summarize_buckets(recs: List[Dict[str, float]]) -> Dict[str, Any]:
        avgs = [r["avg"] for r in recs]
        mins = [r["min"] for r in recs]
        maxs = [r["max"] for r in recs]
        out = describe_ua(avgs)
        out["min_uA"] = min(mins)
        out["max_uA"] = max(maxs)
        out["min"] = fmt_ua(out["min_uA"])
        out["max"] = fmt_ua(out["max_uA"])
        return out

    @staticmethod
    def _downsample(recs: List[Dict[str, float]], max_points: int) -> Dict[str, List[float]]:
        n = max(1, min(int(max_points), len(recs)))
        step = len(recs) / n
        t_rel0 = recs[0]["t_ms"]
        t, avg, mn, mx = [], [], [], []
        for i in range(n):
            seg = recs[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]
            t.append(round(seg[0]["t_ms"] - t_rel0, 1))
            avg.append(round(statistics.fmean([r["avg"] for r in seg]), 3))
            mn.append(round(min(r["min"] for r in seg), 3))
            mx.append(round(max(r["max"] for r in seg), 3))
        return {"t_ms": t, "avg_uA": avg, "min_uA": mn, "max_uA": mx}

    @staticmethod
    def _load_jsonl(path: Optional[str]) -> List[Dict[str, float]]:
        if not path or not os.path.isfile(path):
            raise ToolError(f"file not found: {path}", "pass the JSONL log path returned by monitor_start/stop")
        recs = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "ms":
                    recs.append({
                        "t_ms": row["t"], "avg": row["avg"], "min": row["min"],
                        "max": row["max"], "sum": row.get("sum", row["avg"] * row.get("n", 1)),
                        "n": row.get("n", 1),
                    })
        return recs

    @staticmethod
    def _load_csv(path: Optional[str]) -> List[Dict[str, float]]:
        if not path or not os.path.isfile(path):
            raise ToolError(f"file not found: {path}", "pass the csv path returned by blu926_capture")
        recs: List[Dict[str, float]] = []
        cur_t = None
        cur: List[float] = []
        with open(path, "r", encoding="utf-8") as fh:
            next(fh, None)  # header
            for line in fh:
                line = line.strip()
                if not line or "," not in line:
                    continue
                ts, v = line.split(",", 1)
                t_ms = float(ts)
                b = int(t_ms)
                if cur_t is None or b != int(cur_t):
                    if cur:
                        recs.append(_bucket(cur, cur_t, cur_t + 1.0))
                    cur_t, cur = t_ms, [float(v)]
                else:
                    cur.append(float(v))
        if cur:
            recs.append(_bucket(cur, cur_t, cur_t + 1.0))
        return recs

    # -- teardown ----------------------------------------------------------- #
    def dispose(self):
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                pass
            self._monitor = None
        if self.dev is not None:
            try:
                self.dev.stop_measuring()
            except Exception:
                pass
            self._close_dev()


_SERVICE: Optional[Blu926Service] = None


def get_service() -> Blu926Service:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = Blu926Service()
    return _SERVICE
