# BLU926 Agent Power Tool

把 [BLU926 低功耗测试仪](https://item.taobao.com/item.htm?id=686815370389)（USB 源表）变成
AI agent（ZCode / Claude / Cursor / Codex 等）可以直接驱动的功耗调试工具：**开关 DUT 电源、
设定输出电压 0.5-5V、测量电流（nA~mA）、持续监视电流变化并做睡眠/活动/电量分析**。

```
┌─────────────┐   MCP(stdio) / CLI    ┌──────────────────────────────┐
│  AI client  │ ────────────────────> │  blu926/                     │
└─────────────┘                       │  mcp_server  (13 工具+门禁)  │──> recordings/ 日志
                                      │  service     (会话/监视/分析)│
                                      │  driver      (原厂 SDK)      │──> USB CDC (COMx)
                                      └──────────────────────────────┘
```

## 快速开始

要求：Python 3.10+，仪器 USB 已连接。安装依赖：

```bash
pip install -r requirements.txt   # pyserial + mcp
```

```bash
# 1) 环境与设备体检
python -m blu926 doctor

# 2) 硬件自检（VOUT 上接一个已知电阻做负载，如 10K；没有负载可 --load-ohms 0 跳过负载项）
python -m blu926 selftest --load-ohms 10000

# 3) 手动走一遍
python -m blu926 voltage 3300
python -m blu926 on
python -m blu926 read                     # 10K 负载应约 320uA
python -m blu926 off
```

## 接入 AI 客户端（MCP）

stdio 服务器：`python -m blu926 serve`（在项目根目录运行，或写绝对路径）。

ZCode（`.zcode/settings.json` 或 MCP 设置界面）：

```json
{
  "mcpServers": {
    "blu926-power": {
      "command": "python",
      "args": ["-m", "blu926", "serve"],
      "cwd": "<项目根目录绝对路径，例如 D:\\BLU926_PM_TOOL>"
    }
  }
}
```

Claude Desktop / Cursor（`claude_desktop_config.json` / `mcp.json`）同理。会话里把本目录的
[SKILL.md](SKILL.md) 提供给 agent（或复制到 `~/.claude/skills`、`.zcode/skills` 等）即可让它
掌握标准操作序列。

## 工具一览（13 个）

| 工具 | 作用 |
|---|---|
| `blu926_doctor` | 依赖/发现/连接/校准/采样体检 |
| `blu926_device_info` | 端口、序列号、校准、保存电压、主机侧状态 |
| `blu926_voltage_set(mv)` | 设输出电压 500-5000mV |
| `blu926_power_set(on)` | 开/关 DUT 电源 |
| `blu926_current_read` | 一次性电流测量（自动丢弃 settle 瞬态） |
| `blu926_monitor_start` | 后台监视：1ms 桶 + 环形缓冲 + JSONL 日志 |
| `blu926_monitor_status` | 会话状态（采样率/桶数/日志路径） |
| `blu926_monitor_read` | 读最近数据：统计 + 降采样 trace |
| `blu926_monitor_wait_for` | 阻塞等待条件：above / below / stable |
| `blu926_monitor_stop` | 停止：全会话统计 + 电量(uAh) + 日志 |
| `blu926_capture` | 原始采样率短窗捕获 → CSV |
| `blu926_analyze` | 低功耗分析：睡眠/活动分类、峰值、电量 |
| `blu926_selftest` | 硬件自检套件 |

全部返回 `{"ok": true/false, ...}`；失败带 `error` + 可执行的 `hint`。
参数与返回字段详见 [docs/TOOLS.md](docs/TOOLS.md)。

## 安全与策略

- `BLU926_MAX_MV`（默认 5000）：电压上限，防 DUT 过压；越界报错并提示。
- `BLU926_POLICY=read_only`：禁用 voltage_set / power_set / selftest（只读观测模式）。
- 自检脚本结束时总是把输出留在**关闭**状态。
- 设备日志/捕获文件写入 `recordings/`（`BLU926_RECORD_DIR` 可改）。

## 文档

- [SKILL.md](SKILL.md) —— AI agent 操作规程（推荐先读）
- [docs/TOOLS.md](docs/TOOLS.md) —— 全部工具/CLI 参数与返回字段
- [docs/AGENTS.md](docs/AGENTS.md) —— 硬件实测事实、驱动陷阱、开发笔记

## 已知实测结论（详见 AGENTS.md）

- BLU926 规格上限 **20K fps**（100K 是 BLU939 型号才有）；本机实测持续 **~16.5k 帧/秒**
  （突发式 USB 上传）。工具按会话自动实测速率并推算时间轴，不硬编码任何速率，
  勿用原厂 BluClient 的时间戳。
- 10K 负载实测：3.3V→321.9µA、2.0V→195.8µA、5.0V→486.9µA（系统偏差 ≈ -2.4%）。
- 输出关断漏流 ≈ 55nA；关断瞬间的电容放电尖峰是真实电流。
