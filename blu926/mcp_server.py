"""MCP tool layer for the BLU926 power profiler.

Every tool returns a self-describing dict:
    {"ok": true,  ...payload...}
    {"ok": false, "error": "...", "hint": "...actionable next step..."}

Policy gate: environment variable BLU926_POLICY
    full      (default) everything allowed
    read_only blocks the control-level tools (voltage/power/selftest)
Guard: BLU926_MAX_MV caps blu926_voltage_set (default 5000 = hardware max).

Run:  python -m blu926 serve     (stdio transport)
"""

import functools
import os
from typing import Optional

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as AppServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as AppServer  # type: ignore

from . import selftest as selftest_mod
from .service import Blu926Service, ToolError, get_service

mcp = AppServer(
    "blu926-power",
    instructions=(
        "Control and read a BLU926 USB power profiler (source meter for low-power work): "
        "it outputs a configurable 0.5-5.0V rail to the DUT, switches it on/off, and samples "
        "the DUT current (20K samples/s class, ~16.5k sustained on BLU926, "
        "uA precision, auto-ranging). "
        "Typical low-power workflow: blu926_doctor -> blu926_voltage_set -> blu926_power_set "
        "-> blu926_monitor_start (before flashing/running firmware) -> reproduce the firmware "
        "behaviour -> blu926_monitor_read / blu926_monitor_wait_for -> blu926_monitor_stop "
        "-> blu926_analyze for sleep/active/charge metrics. One-shot checks: blu926_current_read. "
        "All currents are in uA machine fields (human strings included). "
        "Call blu926_doctor first whenever anything misbehaves."
    ),
)

_CONTROL_TOOLS = {"blu926_voltage_set", "blu926_power_set", "blu926_selftest"}


def tool(fn):
    """Unified contract + policy gate. functools.wraps keeps the real signature
    so the SDK derives the argument schema from the wrapped function."""
    name = fn.__name__

    @functools.wraps(fn)
    def run(*a, **kw):
        policy = os.environ.get("BLU926_POLICY", "full").strip().lower()
        if policy == "read_only" and name in _CONTROL_TOOLS:
            return {
                "ok": False,
                "error": f"BLU926_POLICY=read_only blocks {name}",
                "hint": "control operations are disabled by policy; "
                        "the operator must set BLU926_POLICY=full to allow them",
            }
        try:
            return fn(*a, **kw)
        except ToolError as e:
            return {"ok": False, "error": e.error, "hint": e.hint}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "hint": "call blu926_doctor for diagnostics"}
    return mcp.tool()(run)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

@tool
def blu926_doctor() -> dict:
    """Health check of the BLU926 profiler: python deps, device discovery
    (USB VID/PID), serial connection, calibration metadata, and a short live
    sampling test. Run this FIRST when anything misbehaves, or before trusting
    measurements on a fresh session. Read-only, safe at any time."""
    return _doctor_impl()


def _doctor_impl() -> dict:
    from .cli import doctor as _doctor
    return _doctor()


@tool
def blu926_device_info() -> dict:
    """Report the connected BLU926: COM port, serial number, calibration shunt
    resistors, the voltage saved in device firmware, and the host-tracked
    output state (power on/off, last set voltage). Read-only."""
    return get_service().device_info()


@tool
def blu926_voltage_set(mv: int) -> dict:
    """Set the output rail voltage in millivolts (hardware range 500-5000 mV,
    may be capped lower by BLU926_MAX_MV). Applies to the DUT only after
    blu926_power_set(True). Example: blu926_voltage_set(3300) for 3.3V."""
    return get_service().set_voltage(mv)


@tool
def blu926_power_set(on: bool) -> dict:
    """Switch the DUT power output on or off. Set the voltage first with
    blu926_voltage_set. Turning on is the moment of inrush current; start a
    monitor session BEFORE switching on if the startup transient matters."""
    return get_service().set_power(on)


@tool
def blu926_current_read(settle_ms: int = 200, duration_ms: int = 500) -> dict:
    """One-shot current measurement of the DUT: discards settle_ms of
    transient, samples for duration_ms, and returns avg/median/min/max/p01/p99
    in uA. Use for quick checks (is my sleep current really 2 uA?); use
    blu926_monitor_start for time-series capture. Refuses while a monitor
    session is running."""
    return get_service().current_read(settle_ms, duration_ms)


@tool
def blu926_monitor_start(max_buffer_s: float = 120.0, log: bool = True) -> dict:
    """Start a background monitoring session: samples continuously, aggregates
    into 1 ms buckets (avg/min/max), keeps the last max_buffer_s in a ring
    buffer and (log=true) streams everything to a JSONL file. Start this
    BEFORE powering the DUT or running firmware so no event is missed.
    Returns immediately; the session survives across tool calls."""
    return get_service().monitor_start(max_buffer_s, log)


@tool
def blu926_monitor_status() -> dict:
    """Status of the monitoring session: running?, elapsed, bucket/sample
    counts, actual sample rate, ring occupancy, log file path. Read-only."""
    return get_service().monitor_status()


@tool
def blu926_monitor_read(duration_ms: Optional[float] = None, max_points: int = 300,
                        include_trace: bool = True) -> dict:
    """Read recent data from the monitoring session's ring buffer. Returns
    summary statistics (avg/median/min/max/percentiles in uA) over the last
    duration_ms (default: whole buffer) plus a downsampled trace of at most
    max_points points (t_ms/avg/min/max arrays) so answers stay context-sized.
    The full-resolution data lives in the JSONL log."""
    return get_service().monitor_read(duration_ms, max_points, include_trace)


@tool
def blu926_monitor_wait_for(condition: str, threshold_ua: Optional[float] = None,
                            tolerance_pct: float = 5.0, window_ms: float = 200.0,
                            timeout_s: float = 30.0) -> dict:
    """Block until a condition holds on the live current, then return when it
    fired (machine-judgeable: triggered=true/false). Conditions:
    'above'  - windowed average rises above threshold_ua (e.g. firmware woke up),
    'below'  - windowed average falls below threshold_ua (e.g. entered sleep),
    'stable' - current flat within tolerance_pct over window_ms.
    Needs a running session (blu926_monitor_start)."""
    return get_service().monitor_wait_for(condition, threshold_ua, tolerance_pct,
                                          window_ms, timeout_s)


@tool
def blu926_monitor_stop() -> dict:
    """Stop the monitoring session. Returns full-session statistics, the
    estimated charge consumed (uAh/mAh) and the JSONL log path for later
    blu926_analyze. Always stop a session before blu926_current_read/capture."""
    return get_service().monitor_stop()


@tool
def blu926_capture(duration_s: float = 5.0, settle_ms: int = 200) -> dict:
    """Capture a short window of RAW samples (full ~16.5k samples/s sustained, no bucket
    aggregation) to a CSV file and return its statistics. Use when ms-level
    buckets are too coarse (edge transients, spike shapes). duration_s is
    capped by BLU926_MAX_CAPTURE_S (default 60). Refuses while a monitor
    session is running."""
    return get_service().capture(duration_s, settle_ms)


@tool
def blu926_analyze(source: str = "monitor", path: Optional[str] = None,
                   sleep_threshold_ua: Optional[float] = None) -> dict:
    """Low-power analysis of a recording. source: 'monitor' (live ring of a
    running session), 'jsonl' (path=monitor log), or 'csv' (path=capture file).
    Returns avg/median/percentiles, min/max, duration and estimated charge
    (uAh). With sleep_threshold_ua it additionally classifies ms-buckets into
    sleep/active: sleep_bucket_fraction, sleep_current stats, active_peak and
    the top current peaks with timestamps. Example: source='jsonl',
    path='.../mon_x.jsonl', sleep_threshold_ua=50 for a firmware that sleeps
    below ~50 uA."""
    return get_service().analyze(source, path, sleep_threshold_ua)


@tool
def blu926_selftest(load_ohms: float = 10000.0) -> dict:
    """Run the full hardware self-test suite (takes ~20 s). Verifies discovery,
    calibration, output-off leakage, voltage accuracy and linearity against a
    known load resistor between VOUT+ and VOUT- (pass its value in load_ohms,
    or 0 to skip load-dependent checks), plus monitor/wait_for/capture flows.
    Leaves the output OFF afterwards. Control-level: blocked by read_only."""
    return selftest_mod.run(load_ohms)


def run_server():
    get_service()  # fail fast on import errors
    mcp.run()


if __name__ == "__main__":
    run_server()
