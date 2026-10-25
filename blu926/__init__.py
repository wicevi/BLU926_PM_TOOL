"""BLU926 low-power profiler agent tool (MCP server + CLI).

Layers:
  driver.py   - vendored vendor SDK (blu2-api), serial protocol + sample decode
  service.py  - sessions, ms-bucket monitor, guards, statistics/analysis
  mcp_server.py - MCP tool layer (unified ok/error contract + policy gate)
  cli.py      - command line interface over the same service
  selftest.py - hardware self-test suite
"""

__version__ = "1.0.0"
