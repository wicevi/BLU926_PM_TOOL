"""Hardware self-test suite for the BLU926 power profiler.

Run:  python -m blu926 selftest [--load-ohms 10000] [--skip-load]

Load-dependent items assume a known resistor between the instrument's VOUT+
and VOUT- (default 10k). Expected current = V / R. Tolerance covers resistor
and calibration error (8%). Every item reports PASS/FAIL with measured detail,
and the bench is always left with the output OFF.
"""

import time
from typing import Callable, Dict, List, Optional

from . import service as svc
from .service import Blu926Service, fmt_ua


class SelfTest:
    def __init__(self, load_ohms: float = 10000.0):
        self.load_ohms = float(load_ohms) if load_ohms else 0.0
        self.results: List[Dict] = []
        self.svc: Optional[Blu926Service] = None
        self._meds: Dict[int, float] = {}  # voltage_mv -> measured median uA

    def _record(self, name: str, ok: bool, detail: str, hint: str = ""):
        item = {"name": name, "pass": ok, "detail": detail}
        if hint:
            item["hint"] = hint
        self.results.append(item)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}", flush=True)
        if hint:
            print(f"       hint: {hint}", flush=True)

    def _item(self, name: str, fn: Callable[[], str]):
        try:
            detail = fn()
            self._record(name, True, detail)
        except AssertionError as e:
            self._record(name, False, str(e), self._hints.get(name, ""))
        except Exception as e:
            self._record(name, False, f"{type(e).__name__}: {e}", "run 'python -m blu926 doctor' for diagnostics")

    _hints: Dict[str, str] = {
        "device_discovery": "check the USB cable and driver; the device shows up as 'USB 串行设备' with VID 0x15A2/PID 0x300A",
        "metadata_calibration": "device answered but calibration text was incomplete; replug USB and retry",
        "off_baseline": "output may be leaking or the DUT/load draws standby current; disconnect the load and retest",
        "power_off_leakage": "residual charge on the output caps is normal for a moment; if it persists, the load or the output stage may be leaky",
    }

    # ------------------------------------------------------------------ #
    def run(self) -> Dict:
        print(f"BLU926 self-test (load: {self.load_ohms or 'none (skipping load checks)'} ohm)", flush=True)
        self.svc = svc.Blu926Service()

        self._item("device_discovery", self._t_discovery)
        self._item("metadata_calibration", self._t_metadata)
        if not any(r["name"] == "device_discovery" and r["pass"] for r in self.results):
            return self._finish()
        self.svc.set_power(False)
        time.sleep(0.3)
        self._item("off_baseline", self._t_off_baseline)
        if self.load_ohms:
            self._item("voltage_and_load_3300mv", self._t_3300)
            self._item("voltage_and_load_2000mv", self._t_2000)
            self._item("voltage_and_load_5000mv", self._t_5000)
            self._item("linearity_estimated_R", self._t_linearity)
        self._item("monitor_session", self._t_monitor)
        if self.load_ohms:
            self._item("wait_for_condition", self._t_wait_for)
        self._item("capture_file", self._t_capture)
        self._item("power_off_leakage", self._t_leak)
        return self._finish()

    def _expect(self, got_uA: float, want_uA: float, tol: float, label: str) -> str:
        lo, hi = want_uA * (1 - tol), want_uA * (1 + tol)
        ok = lo <= got_uA <= hi
        detail = (f"{label}: measured {fmt_ua(got_uA)} vs expected {fmt_ua(want_uA)} "
                  f"({got_uA / want_uA * 100:.1f}%)")
        if not ok:
            raise AssertionError(detail)
        return detail

    # -- individual items ------------------------------------------------ #
    def _t_discovery(self) -> str:
        dev = self.svc.ensure_open()
        sn = self.svc._sn_of(self.svc._port_of_dev())
        return f"opened {self.svc._port_of_dev()} SN={sn}"

    def _t_metadata(self) -> str:
        dev = self.svc.dev
        r = dev.modifiers.get("R", {})
        loaded = [k for k in ("0", "1", "2", "3", "4", "5") if r.get(k)]
        if len(loaded) < 6:
            raise AssertionError(f"only {len(loaded)}/6 shunt values present: {r}")
        return (f"calibration loaded: R0..R5={loaded}, saved_vdd={dev.saved_vdd_mv}mV, "
                f"HW={dev.modifiers.get('HW')}")

    def _t_off_baseline(self) -> str:
        out = self.svc.current_read(settle_ms=200, duration_ms=400)
        med = out["median_uA"]
        if med > 1.0:
            raise AssertionError(f"output OFF but median current {fmt_ua(med)} (> 1uA)")
        return f"output OFF, median {fmt_ua(med)} (n={out['n']}, rate={out['sample_rate_hz']}Hz)"

    def _read_at(self, mv: int) -> float:
        self.svc.set_voltage(mv)
        self.svc.set_power(True)
        time.sleep(0.3)
        out = self.svc.current_read(settle_ms=300, duration_ms=600)
        self._meds[mv] = out["median_uA"]
        return out["median_uA"]

    def _t_3300(self) -> str:
        med = self._read_at(3300)
        return self._expect(med, 3300.0 / self.load_ohms * 1000.0, 0.08, "3.3V on load")

    def _t_2000(self) -> str:
        med = self._read_at(2000)
        return self._expect(med, 2000.0 / self.load_ohms * 1000.0, 0.08, "2.0V on load")

    def _t_5000(self) -> str:
        med = self._read_at(5000)
        return self._expect(med, 5000.0 / self.load_ohms * 1000.0, 0.08, "5.0V on load")

    def _t_linearity(self) -> str:
        pts = sorted((v, i) for v, i in self._meds.items() if v in (2000, 3300, 5000))
        if len(pts) < 3:
            raise AssertionError(f"expected 3 measured points, got {pts}")
        n = len(pts)
        sx = sum(v for v, _ in pts)
        sy = sum(i for _, i in pts)
        sxx = sum(v * v for v, _ in pts)
        sxy = sum(v * i for v, i in pts)
        denom = n * sxx - sx * sx
        if denom == 0:
            raise AssertionError("degenerate voltage points, cannot fit line")
        slope = (n * sxy - sx * sy) / denom          # uA per mV
        r_est = (1.0 / slope) * 1e3 if slope > 0 else float("inf")  # ohms
        lo, hi = self.load_ohms * 0.9, self.load_ohms * 1.1
        if not (lo <= r_est <= hi):
            raise AssertionError(
                f"V/I slope implies R={r_est:.0f} ohm, expected {self.load_ohms:.0f} +/-10% "
                f"(points: {[(v, round(i, 1)) for v, i in pts]})")
        return (f"I = f(V) linear over 2.0/3.3/5.0V; implied load R = {r_est:.0f} ohm "
                f"(expected {self.load_ohms:.0f})")

    def _t_monitor(self) -> str:
        self.svc.set_voltage(3300)
        self.svc.set_power(True)
        time.sleep(0.2)
        st = self.svc.monitor_start(max_buffer_s=30, log=True)
        time.sleep(2.0)
        rd = self.svc.monitor_read(duration_ms=1000, max_points=50)
        stop = self.svc.monitor_stop()
        if not rd.get("ok") or rd.get("empty"):
            raise AssertionError(f"monitor produced no data: {rd}")
        if stop.get("charge_uAh") is None:
            raise AssertionError(f"monitor stop missing charge: {stop}")
        med = rd["median_uA"]
        want = 3300.0 / self.load_ohms * 1000.0
        if self.load_ohms and abs(med - want) > 0.15 * want:
            raise AssertionError(f"monitor median {fmt_ua(med)} far from expected {fmt_ua(want)}")
        return (f"session: {stop.get('n')} ms-buckets, rate={stop.get('sample_rate_hz')}Hz, "
                f"median={fmt_ua(med)}, charge={stop.get('charge_uAh')}uAh, log={stop.get('log')}")

    def _t_wait_for(self) -> str:
        self.svc.set_voltage(2500)  # 250uA on 10k
        time.sleep(0.3)
        self.svc.monitor_start(max_buffer_s=30, log=False)
        try:
            r = self.svc.monitor_wait_for("below", threshold_ua=300.0, window_ms=200, timeout_s=10)
            if not r.get("triggered"):
                raise AssertionError(f"'below 300uA' not triggered: {r}")
            self.svc.set_voltage(3300)
            r3 = self.svc.monitor_wait_for("above", threshold_ua=300.0, window_ms=200, timeout_s=10)
            if not r3.get("triggered"):
                raise AssertionError(f"'above 300uA' not triggered: {r3}")
            return f"below/above triggers fired ({r['value']}uA -> {r3['value']}uA)"
        finally:
            self.svc.monitor_stop()

    def _t_capture(self) -> str:
        out = self.svc.capture(duration_s=1.0, settle_ms=200)
        if out["n"] < 1000:
            raise AssertionError(f"capture got only {out['n']} samples")
        import os
        size = os.path.getsize(out["csv"])
        return (f"capture ok: {out['n']} samples, rate={out['sample_rate_hz']}Hz, "
                f"median={fmt_ua(out['median_uA'])}, csv={out['csv']} ({size} bytes)")

    def _t_leak(self) -> str:
        self.svc.set_power(False)
        time.sleep(0.5)
        out = self.svc.current_read(settle_ms=300, duration_ms=500)
        med = out["median_uA"]
        if med > 1.0:
            raise AssertionError(f"output OFF but median {fmt_ua(med)} (> 1uA)")
        return f"power off, median {fmt_ua(med)}, max {fmt_ua(out['max_uA'])} (transient)"

    # ------------------------------------------------------------------ #
    def _finish(self) -> Dict:
        try:
            self.svc.set_power(False)
        except Exception:
            pass
        try:
            self.svc.dispose()
        except Exception:
            pass
        passed = sum(1 for r in self.results if r["pass"])
        total = len(self.results)
        all_ok = passed == total
        print(f"\nSELF-TEST {'PASSED' if all_ok else 'FAILED'}: {passed}/{total} items", flush=True)
        return {"ok": all_ok, "passed": passed, "total": total, "items": self.results}


def run(load_ohms: float = 10000.0) -> Dict:
    return SelfTest(load_ohms).run()
