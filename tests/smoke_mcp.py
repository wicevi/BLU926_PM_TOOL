"""Smoke test: drive the blu926 MCP server with a real stdio client.

Verifies: tool listing, unified ok/error contract, a control round-trip
(voltage -> power on -> read ~load current -> monitor -> power off), the
negative path (out-of-range voltage) and the read_only policy gate.

Run:  python tests/smoke_mcp.py [--load-ohms 10000]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_TOOLS = {
    "blu926_doctor", "blu926_device_info", "blu926_voltage_set", "blu926_power_set",
    "blu926_current_read", "blu926_monitor_start", "blu926_monitor_status",
    "blu926_monitor_read", "blu926_monitor_wait_for", "blu926_monitor_stop",
    "blu926_capture", "blu926_analyze", "blu926_selftest",
}

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


def payload(result):
    txt = result.content[0].text if result.content else "{}"
    return json.loads(txt)


def session_with(env_extra=None):
    env = {**os.environ, "PYTHONUTF8": "1"}
    if env_extra:
        env.update(env_extra)
    params = StdioServerParameters(command=sys.executable, args=["-m", "blu926", "serve"],
                                   cwd=ROOT, env=env)
    return stdio_client(params)


async def main():
    load_ohms = 10000.0
    if "--load-ohms" in sys.argv:
        load_ohms = float(sys.argv[sys.argv.index("--load-ohms") + 1])

    cm = session_with()
    read, write = await cm.__aenter__()
    try:
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            check("list_tools", EXPECTED_TOOLS <= names,
                  f"{len(names)} tools; missing: {EXPECTED_TOOLS - names or 'none'}")

            r = payload(await s.call_tool("blu926_device_info", {}))
            check("device_info", r.get("ok") is True, f"port={r.get('port')} sn={r.get('serial_number')}")

            r = payload(await s.call_tool("blu926_voltage_set", {"mv": 9000}))
            check("negative_out_of_range_voltage",
                  r.get("ok") is False and "error" in r and "hint" in r,
                  f"error={r.get('error')}")

            r = payload(await s.call_tool("blu926_voltage_set", {"mv": 3300}))
            check("voltage_set_3300", r.get("ok") is True and r.get("voltage_mv") == 3300)

            r = payload(await s.call_tool("blu926_power_set", {"on": True}))
            check("power_on", r.get("ok") is True and r.get("power") == "on")

            await asyncio.sleep(0.5)
            want = 3300.0 / load_ohms * 1000.0
            r = payload(await s.call_tool("blu926_current_read",
                                          {"settle_ms": 300, "duration_ms": 500}))
            med = r.get("median_uA", 0)
            check("current_read_on_load", r.get("ok") is True and abs(med - want) < 0.15 * want,
                  f"median={r.get('median')} expected={want:.0f}uA rate={r.get('sample_rate_hz')}Hz")

            r = payload(await s.call_tool("blu926_monitor_start", {"max_buffer_s": 30}))
            check("monitor_start", r.get("ok") is True and r.get("running") is True)

            r = payload(await s.call_tool("blu926_monitor_wait_for",
                                          {"condition": "above", "threshold_ua": want * 0.5,
                                           "timeout_s": 10}))
            check("monitor_wait_for", r.get("ok") is True and r.get("triggered") is True,
                  f"value={r.get('value')}uA")

            await asyncio.sleep(1.0)
            r = payload(await s.call_tool("blu926_monitor_read",
                                          {"duration_ms": 1000, "max_points": 60}))
            trace_ok = isinstance(r.get("trace", {}).get("avg_uA"), list) and r["trace"]["avg_uA"]
            check("monitor_read", r.get("ok") is True and trace_ok and not r.get("empty"),
                  f"median={r.get('median')} points={len(r.get('trace', {}).get('avg_uA', []))}")

            r = payload(await s.call_tool("blu926_monitor_stop", {}))
            check("monitor_stop", r.get("ok") is True and r.get("charge_uAh") is not None,
                  f"charge={r.get('charge_uAh')}uAh log={r.get('log')}")

            r = payload(await s.call_tool("blu926_power_set", {"on": False}))
            check("power_off", r.get("ok") is True)

            await asyncio.sleep(0.3)
            r = payload(await s.call_tool("blu926_current_read",
                                          {"settle_ms": 300, "duration_ms": 400}))
            check("current_read_after_off", r.get("ok") is True and r.get("median_uA", 9e9) < 1.0,
                  f"median={r.get('median')}")
    finally:
        await cm.__aexit__(None, None, None)

    # read_only policy gate in a separate server process
    cm2 = session_with({"BLU926_POLICY": "read_only"})
    read, write = await cm2.__aenter__()
    try:
        async with ClientSession(read, write) as s:
            await s.initialize()
            r = payload(await s.call_tool("blu926_power_set", {"on": True}))
            check("policy_read_only_blocks_power", r.get("ok") is False and "read_only" in r.get("error", ""),
                  f"error={r.get('error')}")
            r = payload(await s.call_tool("blu926_device_info", {}))
            check("policy_read_only_allows_reads", r.get("ok") is True)
    finally:
        await cm2.__aexit__(None, None, None)

    passed = sum(1 for _, ok in results if ok)
    print(f"\nSMOKE {'PASSED' if passed == len(results) else 'FAILED'}: {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
