"""Command-line interface for the BLU926 power profiler.

All commands print a single JSON object (ok/error contract, same shape as the
MCP tools). Use `--text` for a human-readable one-liner instead.

Typical use:
  python -m blu926 doctor
  python -m blu926 info
  python -m blu926 voltage 3300
  python -m blu926 on | off
  python -m blu926 read --duration-ms 500
  python -m blu926 monitor --seconds 10
  python -m blu926 analyze --file recordings/mon_xxx.jsonl --sleep-threshold-ua 50
  python -m blu926 selftest --load-ohms 10000
  python -m blu926 serve          # start the MCP stdio server
"""

import argparse
import json
import sys

from . import __version__
from . import selftest as selftest_mod
from . import service as svc
from .service import Blu926Service, ToolError, fmt_ua


def _emit(obj: dict, as_text: bool = False):
    if as_text:
        ok = obj.get("ok")
        if not ok:
            print(f"ERROR: {obj.get('error')}\nhint: {obj.get('hint', '')}")
        else:
            keys = [k for k in ("power", "voltage", "voltage_mv", "median", "avg", "running",
                                "triggered", "pass", "log", "csv") if k in obj]
            print(", ".join(f"{k}={obj[k]}" for k in keys) or json.dumps(obj, ensure_ascii=False)[:200])
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _wrap(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except ToolError as e:
        return {"ok": False, "error": e.error, "hint": e.hint}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "run 'python -m blu926 doctor' for diagnostics"}


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps a parent-level --text from being overwritten by subparser defaults
    common.add_argument("--text", action="store_true", default=argparse.SUPPRESS,
                        help="human-readable output instead of JSON")
    p = argparse.ArgumentParser(prog="blu926", description="BLU926 low-power profiler CLI",
                                parents=[common])
    p.add_argument("--version", action="version", version=f"blu926 {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", parents=[common], help="environment + device health check")
    sub.add_parser("info", parents=[common], help="device info and tracked state")
    sub.add_parser("on", parents=[common], help="turn DUT power output on")
    sub.add_parser("off", parents=[common], help="turn DUT power output off")

    sp = sub.add_parser("voltage", parents=[common], help="set output voltage in mV (500..5000)")
    sp.add_argument("mv", type=int)

    sp = sub.add_parser("read", parents=[common], help="one-shot current measurement")
    sp.add_argument("--settle-ms", type=int, default=200)
    sp.add_argument("--duration-ms", type=int, default=500)

    sp = sub.add_parser("monitor", parents=[common], help="blocking capture session with 1ms buckets")
    sp.add_argument("--seconds", type=float, default=10.0)
    sp.add_argument("--max-buffer-s", type=float, default=120.0)
    sp.add_argument("--no-log", action="store_true", help="do not write the JSONL log")
    sp.add_argument("--read-last-ms", type=float, default=1000.0,
                    help="window for the summary stats / trace")
    sp.add_argument("--trace-points", type=int, default=120)

    sp = sub.add_parser("capture", parents=[common], help="raw-rate burst capture to CSV")
    sp.add_argument("--seconds", type=float, default=5.0)
    sp.add_argument("--settle-ms", type=int, default=200)

    sp = sub.add_parser("analyze", parents=[common], help="low-power analysis of a monitor log / capture csv")
    sp.add_argument("--file", help="path to .jsonl or .csv (omit: live monitor ring)")
    sp.add_argument("--sleep-threshold-ua", type=float, default=None,
                    help="enable sleep/active classification below this bucket-max current")

    sp = sub.add_parser("selftest", parents=[common], help="hardware self-test (needs the reference load)")
    sp.add_argument("--load-ohms", type=float, default=10000.0)

    sub.add_parser("serve", help="start the MCP stdio server (for AI clients)")

    a = p.parse_args(argv)
    text = getattr(a, "text", False)

    if a.cmd == "serve":
        from .mcp_server import run_server
        run_server()
        return 0

    if a.cmd == "doctor":
        _emit(doctor(), text)
        return 0 if doctor()["ok"] else 1

    if a.cmd == "selftest":
        res = _wrap(selftest_mod.run, a.load_ohms)
        _emit(res, text)
        return 0 if res.get("ok") else 1

    s = Blu926Service()
    try:
        if a.cmd == "info":
            out = _wrap(s.device_info)
        elif a.cmd == "on":
            out = _wrap(s.set_power, True)
        elif a.cmd == "off":
            out = _wrap(s.set_power, False)
        elif a.cmd == "voltage":
            out = _wrap(s.set_voltage, a.mv)
        elif a.cmd == "read":
            out = _wrap(s.current_read, a.settle_ms, a.duration_ms)
        elif a.cmd == "capture":
            out = _wrap(s.capture, a.seconds, a.settle_ms)
        elif a.cmd == "monitor":
            out = _cli_monitor(s, a)
        elif a.cmd == "analyze":
            src = "monitor" if not a.file else ("jsonl" if a.file.endswith(".jsonl") else "csv")
            out = _wrap(s.analyze, src, a.file, a.sleep_threshold_ua)
        else:  # pragma: no cover
            out = {"ok": False, "error": f"unknown command {a.cmd}"}
    finally:
        s.dispose()
    _emit(out, text)
    return 0 if out.get("ok") else 1


def _cli_monitor(s: Blu926Service, a) -> dict:
    """Blocking monitor session for CLI use (start -> run -> stop -> summary)."""
    try:
        st = s.monitor_start(max_buffer_s=a.max_buffer_s, log=not a.no_log)
    except ToolError as e:
        return {"ok": False, "error": e.error, "hint": e.hint}
    import time as _t
    try:
        _t.sleep(a.seconds)
        if a.seconds >= 1.0:
            rd = s.monitor_read(duration_ms=a.read_last_ms, max_points=a.trace_points)
        else:
            rd = {}
        stop = s.monitor_stop()
    except ToolError as e:
        s.monitor_stop()
        return {"ok": False, "error": e.error, "hint": e.hint}
    out = {"ok": stop.get("ok", True), "requested_seconds": a.seconds,
           "log": st.get("log"), **{k: v for k, v in stop.items() if k != "ok"}}
    if rd:
        out["last_window"] = {k: rd[k] for k in rd if k != "trace"}
        out["trace"] = rd.get("trace")
    return out


def doctor() -> dict:
    checks = []

    def add(name, ok, detail, hint=""):
        checks.append({"name": name, "ok": ok, "detail": detail, **({"hint": hint} if hint else {})})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    try:
        import serial
        add("pyserial", True, f"pyserial {serial.__version__}")
    except Exception as e:
        add("pyserial", False, f"import failed: {e}", "python -m pip install pyserial")

    try:
        import mcp  # noqa: F401
        add("mcp-sdk", True, f"mcp {getattr(mcp, '__version__', 'installed')}")
    except Exception:
        add("mcp-sdk", False, "not installed",
            "only needed for MCP clients: python -m pip install mcp")

    devs = svc.driver.find_all()
    if devs:
        d = devs[0]
        add("device_discovery", True, f"{d['port']} SN={d['serial']} ({d['description']})")
        s = Blu926Service(port=d["port"])
        try:
            s.ensure_open()
            r = s.device_info()
            add("connection_metadata", True,
                f"calibration loaded, saved_vdd={r['saved_vdd_mv']}mV, SN={r['serial_number']}")
            cr = _wrap(s.current_read, 100, 200)
            if cr.get("ok"):
                add("sampling", True,
                    f"{cr['n']} samples in 200ms, rate={cr['sample_rate_hz']}Hz, "
                    f"median={fmt_ua(cr['median_uA'])} (power tracked as {cr['power']})")
            else:
                add("sampling", False, cr.get("error", "?"), cr.get("hint", ""))
        except ToolError as e:
            add("connection_metadata", False, e.error, e.hint)
        finally:
            s.dispose()
    else:
        import serial.tools.list_ports as lp
        ports = [p.device for p in lp.comports()]
        add("device_discovery", False, "no BLU device (VID 0x15A2/PID 0x300A) found",
            "check USB cable/power and the driver; COM ports visible: " + (", ".join(ports) or "none"))

    all_ok = all(c["ok"] for c in checks)
    hard = all(c["ok"] for c in checks if c["name"] != "mcp-sdk")
    print(f"\nDOCTOR {'PASSED' if hard else 'FAILED'} ({sum(c['ok'] for c in checks)}/{len(checks)})",
          flush=True)
    return {"ok": all_ok, "core_ok": hard, "checks": checks}


if __name__ == "__main__":
    raise SystemExit(main())
