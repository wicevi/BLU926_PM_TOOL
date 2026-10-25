# BLU926 工具参考（MCP 工具 + CLI）

所有 MCP 工具与 CLI 命令共享同一契约：

```json
{"ok": true,  "...payload...": 0}
{"ok": false, "error": "发生了什么", "hint": "下一步该做什么"}
```

电流机器字段单位一律 `uA`（微安），并附人类可读字符串（`"321.92uA"`、`"55.5nA"`）。
环境变量：`BLU926_PORT`（固定端口）、`BLU926_MAX_MV`（电压上限，默认 5000）、
`BLU926_POLICY`（`full`/`read_only`）、`BLU926_RECORD_DIR`（日志目录）、
`BLU926_MAX_CAPTURE_S`（capture 上限，默认 60）。

## 会话与状态模型

- 设备串口懒连接，串口错误自动重连重试一次。
- 主机侧跟踪 `power`（on/off，无法从设备回读）、`voltage_mv`（最后一次设定值）。
- 监视会话全机唯一：运行中时 `current_read`/`capture` 拒绝执行。
- `voltage_set` 只改设定值，`power_set(true)` 才作用于 DUT；settle/统计窗口
  不包含上电瞬态（瞬态要靠 monitor 捕获）。

## MCP 工具

### blu926_doctor()
体检：pyserial / mcp 依赖、设备发现（VID 0x15A2 / PID 0x300A）、连接与校准、
200ms 采样试测。返回 `checks[]`（每项 `name/ok/detail/hint?`）+ `core_ok`。
**任何异常先调它。**

### blu926_device_info()
→ `port, serial_number, calibrated_flag, shunt_R{0..5}, saved_vdd_mv`（固件保存的电压）、
`tracked_power, tracked_voltage_mv, monitor_running`。

### blu926_voltage_set(mv: int)
500-5000mV（受 `BLU926_MAX_MV` 上限）。→ `voltage_mv, voltage`。
负例：`9000` → `ok:false`，error 说明超上限，hint 指引。

### blu926_power_set(on: bool)
开关 DUT 电源输出。→ `power, voltage_mv`，`hint` 提示用 current_read 验证或先开 monitor。

### blu926_current_read(settle_ms=200, duration_ms=500)
一次性测量：丢弃 settle 期数据（含上电瞬态与滤波器收敛），统计窗口数据。
→ 统计字段（见下）+ `power, voltage_mv, sample_rate_hz, settle_ms, duration_ms`。

### blu926_monitor_start(max_buffer_s=120, log=true)
后台持续采样。1ms 桶（avg/min/max/sum/n）→ 环形缓冲（保留最近 max_buffer_s 秒）
+ JSONL 日志（`recordings/mon_*.jsonl`，含 header 行与 ms 行）。立即返回；
重复调用幂等（返回 already_running）。**测量低功耗行为时，先 start 再上电/跑固件。**

### blu926_monitor_status()
→ `running, elapsed_s, buckets, samples, sample_rate_hz, ring_seconds, log`。

### blu926_monitor_read(duration_ms?, max_points=300, include_trace=true)
读环形缓冲最近 `duration_ms`（缺省=全部）。
→ 统计字段 + `trace{t_ms[], avg_uA[], min_uA[], max_uA[]}`（最多 max_points 点，
相对时间 ms）+ `running`。空数据返回 `empty:true` + hint。

### blu926_monitor_wait_for(condition, threshold_ua?, tolerance_pct=5, window_ms=200, timeout_s=30)
阻塞等待，机器判定 `triggered:true/false`：
- `"above"`：窗口平均 > threshold_ua（等待唤醒/发射）
- `"below"`：窗口平均 < threshold_ua（等待进入睡眠）
- `"stable"`：窗口内 max-min ≤ max(0.2uA, median×tolerance_pct%)
返回触发值与 `window_avg/window_min/window_max`。超时不是错误（`ok:true, triggered:false`）。

### blu926_monitor_stop()
停止会话。→ 全会话统计 + `charge_uAh/charge_mAh`（按样本积分，电池寿命估算用）+
`log` 路径 + `sample_rate_hz`。环形缓冲只留尾部，全量数据在 JSONL。

### blu926_capture(duration_s=5, settle_ms=200)
原始采样率（BLU926 规格 20K 上限，实测持续 ~16.5k/s）捕获 ≤60s → `recordings/cap_*.csv`（`t_ms,ua`）+ `cap_*.json`
元数据。→ 统计 + `charge_uAh + csv, meta` 路径。用于 ms 桶太粗的边缘/尖峰波形分析。

### blu926_analyze(source="monitor", path?, sleep_threshold_ua?)
数据源：`monitor`（活动会话环形缓冲）/ `jsonl`（监视日志）/ `csv`（capture 文件）。
→ 统计 + `duration_s + charge_uAh`；给 `sleep_threshold_ua` 时追加分类：
`sleep_bucket_fraction`（桶 max < 阈值占比）、`sleep_current`（睡眠桶统计）、
`active_bucket_fraction`、`active_peak`、`peaks[]`（Top5 电流峰值事件，间隔≥50ms，
带时间戳——定位固件哪段在耗电）。

### blu926_selftest(load_ohms=10000)
硬件自检（约 20s，会开关电源）：发现/校准/关断底噪/三点电压负载精度/线性度/
监视会话/条件触发/CSV 捕获/关断漏流。`load_ohms=0` 跳过负载相关项。
→ `items[]`（`name/pass/detail`）+ `passed/total`。结束必为输出关闭。

## 统计字段（各测量工具通用）

`n, avg_uA, median_uA, min_uA, max_uA, p01_uA, p99_uA, stdev_uA` +
`avg/median/min/max`（人类可读字符串）。
稳态电流看 `median_uA`；瞬态/峰值看 `max_uA`、`p99_uA`。

## CLI（`python -m blu926 <cmd>`，输出同契约 JSON，`--text` 人类可读）

| 命令 | 等价工具 | 备注 |
|---|---|---|
| `doctor` | blu926_doctor | |
| `info` | blu926_device_info | |
| `voltage <mv>` | blu926_voltage_set | |
| `on` / `off` | blu926_power_set | |
| `read [--settle-ms N] [--duration-ms N]` | blu926_current_read | |
| `monitor --seconds N [--no-log] [--read-last-ms N] [--trace-points N]` | start→read→stop 一条龙 | CLI 每次是新进程，monitor 为阻塞式 |
| `capture --seconds N` | blu926_capture | |
| `analyze [--file x.jsonl|x.csv] [--sleep-threshold-ua N]` | blu926_analyze | 无 --file 分析不了（CLI 无常驻会话），用 MCP 的 monitor 源 |
| `selftest [--load-ohms R]` | blu926_selftest | |
| `serve` | — | 启动 MCP stdio 服务器 |
