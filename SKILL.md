---
name: blu926-power
description: Control the BLU926 USB power profiler (source meter) for low-power firmware work - switch DUT power on/off, set the output rail 0.5-5V, measure current from nA to mA, monitor current over time and analyze sleep/active behaviour and charge. Use when doing low-power development, measuring sleep current or duty cycle, estimating battery life, or whenever BLU926 MCP tools (blu926_*) or the `python -m blu926` CLI are available.
---

# BLU926 功耗分析仪操作规程（AI agent 用）

给 DUT 供电并测量其电流的低功耗测试仪（USB 源表，输出 0.5-5V，电流测量 nA~mA，
实际采样率 ~16.5k 样本/秒（BLU926 规格 20K 上限），自动量程）。电流机器字段单位一律 **uA**。

## 前置条件

- 仪器已 USB 连接电脑；VOUT+ / VOUT- 接到 DUT（无 DUT 时不要开输出检查）。
- 同一时刻只有一个程序能占用设备串口（厂商 GUI / 串口监视器会冲突）。
- 出现任何异常，先跑 `blu926_doctor`（或 CLI `python -m blu926 doctor`）。

## 标准动作（MCP 工具；CLI 等价命令见括号）

**上电 + 快速验证**
```
blu926_voltage_set(mv=3300)        # 设电压，500-5000mV，受 BLU926_MAX_MV 上限约束
blu926_power_set(on=true)          # 开输出（开的是 DUT 电源，不是采样）
blu926_current_read()              # 一次性读数：avg/median/min/max/p01/p99 (uA)
```
（CLI：`python -m blu926 voltage 3300` → `on` → `read`）

**低功耗测量会话（推荐流程：先开监视再上电，避免漏掉启动瞬态）**
```
blu926_monitor_start()             # 后台持续采样，1ms 桶聚合 + JSONL 日志，立即返回
blu926_power_set(on=true)          # 此时上电可捕获 inrush
  ... 触发/复现固件行为（烧录、按键、跑固件）...
blu926_monitor_read(duration_ms=5000)      # 摘要统计 + 降采样 trace（≤300 点，不刷上下文）
blu926_monitor_wait_for(condition="below", threshold_ua=50, timeout_s=30)   # 等待进入睡眠
blu926_monitor_stop()              # 结束：返回全会话统计 + 电量(uAh) + 日志路径
blu926_analyze(source="jsonl", path=<log>, sleep_threshold_ua=50)  # 睡眠/活动分类、峰值、电量
```
（CLI：`python -m blu926 monitor --seconds 10` 一次跑完并输出摘要）

**精细波形**：`blu926_capture(duration_s=5)` 原始采样率落 CSV，再用
`blu926_analyze(source="csv", path=...)` 分析。

**收尾**：`blu926_power_set(on=false)`（自检脚本会自动这么做）。

## 判读要点

- `median_uA` 是稳态电流的可靠指标；`max_uA`/`p99_uA` 反映瞬态（上电 inrush、
  量程切换、无线发射突发都是真实电流）。
- 睡眠判定阈值 `sleep_threshold_ua` 要设在底噪之上（本底 ~0.1uA 级），
  例如睡眠电流的 2-10 倍。
- 精度参考：10K 负载实测 3.3V→322uA（隐含 R=10.3K，-2.4% 系统偏差，
  电阻/校准容差量级）。对比规格书电流时留 3% 余量。
- 电量：`charge_uAh`（会话/窗口累计），用于电池寿命估算。

## 注意事项

- 工具契约：成功 `{"ok":true,...}`；失败 `{"ok":false,"error","hint"}`，hint 是下一步。
- 监视会话运行中 `blu926_current_read`/`blu926_capture` 会被拒绝——先 stop 或改用 monitor_read。
- 策略门禁：环境变量 `BLU926_POLICY=read_only` 时 voltage_set/power_set/selftest 被拒。
- 电压上限：`BLU926_MAX_MV`（默认 5000 = 硬件上限），防止给 DUT 过压。
- 设备记录与日志在 `recordings/`（可用 `BLU926_RECORD_DIR` 改）。

## 排障

| 症状 | 处置 |
|---|---|
| 找不到设备 | 检查 USB 线/供电；doctor 会列出当前可见 COM 口 |
| 打不开串口 | 关掉占用端口的程序（厂商上位机、串口终端） |
| 电流读数全 0 / no samples | 确认 power 已开、DUT 接在 VOUT 上；跑 doctor 的 sampling 项 |
| 读数异常大/小 | 确认电压设置与 DUT 匹配；确认接线（串联在电源回路里） |
| 其余一切 | 先 `blu926_doctor`，按 hint 行动；硬件自检：`python -m blu926 selftest --load-ohms <R>` |

详细工具参数/返回字段：`docs/TOOLS.md`。硬件实测事实：`docs/AGENTS.md`。
