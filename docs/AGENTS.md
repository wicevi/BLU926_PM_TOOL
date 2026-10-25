# AGENTS.md — BLU926 工具开发笔记（硬件事实 / 实测结论 / 陷阱）

供开发者与后续 AI 会话使用。**以下均为 2026-10 在实机（SN 6F6B6BBB000F，COM21）实测结论，
勿再重新推导。** 原则：新结论当场落在这里。

## 硬件事实

- 淘宝商品：BLU926 低功耗测试仪（USB 源表，**20K 型号；100K 是 BLU939 才有**）。
  厂商 API 包 `blu_api_python_100k_202508161306.zip` —— 包名按 100K 型号家族命名，
  SDK 本身通用，单文件。
- USB：VID `0x15A2` / PID `0x300A`，CDC-ACM 串口，波特率 1M（打开失败自动回退 115200，
  CDC 下波特率实际不影响）。DTR/RTS 需置位。Windows 显示为"USB 串行设备"。
- 串口文本协议（GET_META_DATA=0x19，以 "END" 结尾）：`Calibrated/R0..R5/GS/GI/O/S/I/UG/HW/IA/VDD`。
  实机 R 分流电阻：1008.4 / 99.65 / 9.93 / 0.916 / 0.099 / 0.0142 Ω（量程 0..5，自动切换）。
- 控制命令（均验证可用）：`0x0D hi lo` 设电压(mV, 500-5000)、`0x0C 01/00` 开关 DUT 电源、
  `0x06/0x07` 开始/停止采样。**未使用** `0x11 SET_POWER_MODE`（例程也不发，不发工作正常）。
- 采样帧：4 字节小端 u32。bits[0:14] ADC（×4），bits[14:17] 量程，bits[24:32] 逻辑通道。
  电流(uA) = (adc - O[range]) × (1.8/163840 / R[range]) × 1e6，再过厂商尖峰滤波 EMA。
- **速率：BLU926 规格上限 20K fps**。1MB 接收缓冲 + 紧密排空实测持续 16507 帧/s
  （65.9kB/s），无主机丢包；低于规格上限是 USB 突发上传的均值效果。
  （初版笔记误把它归因为"对抗宣传 100k"，已更正——100K 是 939 型号的事。）
- **上传是突发式的**：约每 64-128ms 一阵 4-8KB，阵间空闲可达 31ms。因此任何
  "按到达时间推每样本时刻" 的做法都会把时间轴搞错（实测回跳 -63ms），必须
  用样本数推算 + 到达时间重同步（见 service._chunk_timeline）。
- 底噪/漏流：输出关闭时中位数 ~55nA（量程 0）。输出关断瞬间输出电容经负载放电，
  放电电流是真实电流（10K 负载时 ~320µA 起衰减，几十 ms 内结束）。
- 精度：10K 负载三点实测 97.4-97.9%，隐含 R≈10.31kΩ —— 系统偏差 ≈ -2.4%
  （负载电阻 ±5% 与校准容差量级），对比规格书时留 3% 余量。

## 原厂驱动（blu926/driver.py，vendored 未改）的陷阱

1. `BluClient` 的时间戳与 `RateAdjuster` 按 100k 基率换算——本机差 6 倍，
   **时间轴数学一律不要用 BluClient**；service 层自建（BLU2_MP + get_samples + 墙钟）。
2. `get_adc_result` 的尖峰滤波 EMA（rolling_avg/rolling_avg4/prev_range）是实例状态，
   **跨会话残留**——新会话前必须重置（service._reset_filter_state），否则关断后
   首个会话的 max 会带上会话前的旧电平。
3. `BLU2_MP.start_measuring` 会先 stop+排空；settle 语义要自己实现：
   **settle 结束后必须排空 fetcher 队列积压**，否则 settle 期数据（含瞬态与
   EMA 收敛段）会涌入统计窗口，且 rate 虚高 30%+。
4. `BLU2_MP.__del__` 会向已关闭串口写命令并 logging.error 刷屏——service._close_dev
   里用实例级 `_write_serial` 覆写消音。
5. fetcher 只在 local_buffer ≥ 4000B 时入队（buffer_chunk_seconds=0.01），
   监视延迟 ≈ 一个 chunk；`buffer_max_size_seconds=2` 防队列堆积丢老数据。
6. `toggle_DUT_power` 的 ON 是 `0x0C 0x01`（注意宏名叫 TRIGGER_SET 但值是 0x01）。

## 工具层设计结论（按 agent 工具设计方法论：统一契约 / 策略门禁 / doctor / 监视器 / wait_for）

- 统一契约 `{ok, error, hint}` + `@functools.wraps` 装饰器（保住参数 schema 推断）——OK。
- mcp 2.3.0：`from mcp.server.mcpserver import MCPServer`，`@mcp.tool()` 返回 dict
  直接可结构化输出；1.x 回退 fastmcp 兼容已写好。
- read_only 策略门禁在工具装饰器层实现（env 每次调用时读）——CLI 不受门禁（是操作员的手）。
- 监视器用 1ms 桶聚合 + deque 环形缓冲 + JSONL 全量日志：120s 会话 ≈ 数 MB 日志，
  工具返回只给统计 + ≤300 点 trace，不刷爆上下文。
- 等待/触发（wait_for）轮询 50ms，用窗口平均防抖；超时返回 `triggered:false` 而非报错。
- 电量 = Σ(uA)/fps（fps 用会话全局平均，抗突发）；capture CSV 时间轴 =
  样本数推算 + 到达重同步，实测 2s 窗口误差 <2%。

## 验证记录（全绿）

- `python -m blu926 selftest --load-ohms 10000` → 11/11 PASS（三点负载精度、线性度
  隐含 R=10308Ω、监视 2084 桶、wait_for 双向触发、CSV 捕获、关断 55.5nA）。
- `python tests/smoke_mcp.py` → 14/14 PASS（真 stdio 客户端走协议：13 工具枚举、
  负用例 9000mV 拒绝、控制往返 321.92µA、monitor 全流程、read_only 门禁双向验证）。

## 限制 / 待办

- `current_read`/`capture`/`monitor` 互斥（单串口），跨进程并发需用户协调。
- 电压>5000mV、duration>60s 等护栏固定；DUT 电压方案切换（如电池模拟）未封装。
- 逻辑通道 bits[24:32] 已解码但未暴露成工具（DUT GPIO 观测），需要时在
  MonitorSession 加一路聚合即可。
- 未封装原厂触发/平均类命令（0x01-0x05, 0x0A, 0x12, 0x15/16, 0x25）——本机用途未验证。
