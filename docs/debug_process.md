# PicoBoard Button Debug 过程总结

## 最终参数（已写入默认值）

```
button_idx = 9
mask       = 0x02
invert     = True   （active-low，空闲时 bit=1，按下时 bit=0）
press_ms   = 0      （立即确认，帧间隔 ~156ms，短按仅 1 帧）
press_frames = 1
release_ms   = 180
release_frames = 2
```

---

## 完整 Debug 路线图

### 阶段 1 — 建立可观察性

**问题**：按键状态跳变但看不到原始数据，无法区分是解析问题还是硬件问题。

**方案**：
- 加 `--serial-log` 开关，控制串口 TX/RX 打印
- 加 `--serial-log-file`，将每帧 hex、边沿事件、values 快照落盘为 JSONL，供离线分析
- 日志事件类型：`config / rx / tx / button_edge / values / parser_protocol`

**收益**：后续所有调参均有日志支撑，避免靠主观感受猜测。

---

### 阶段 2 — Legacy 协议解析鲁棒化

**问题**：strict F8 04 + marker 对齐率低，部分帧被丢弃导致按键信号漏采。

**方案**：
- 引入 `_legacy_packet_score()` 打分函数，基于多个 expected marker 字节累分
- 对齐率 threshold = 7 分（满分 11 分）
- 头对齐先找 `F8 04`，再做打分，容忍 marker 轻微漂移

**收益**：alignment_ratio 从 ~0.85 提升到 0.99+，基本消除错包。

---

### 阶段 3 — 模拟量回卷修复（light/sound 假跌落）

**问题**：无操作时 light/sound 值突然从 ~46 跌到 ~2，视觉上"无触摸有变化"。

**根因**：legacy 协议模拟量用 7+7 bit：id 字节高位 + val 字节低 7 位。val 从 127 回卷到 0 时 id+1，若只读 val 会误解为跌落。

**方案**：`_legacy_analog()` 检测 `id == last_id + 1` 时对 val 补 +128，保持连续输出。

---

### 阶段 4 — WebSocket 竞态修复

**问题**：防抖后 `button_edge` 日志正确，但 WebSocket 发出的 `values.button` 仍跳变。

**根因**：`serial_thread` 修改了 `pico.values["button"]`，WS handler 线程并发读取时拿到的是原始噪声值。

**方案**：
- 新增 `pico._stable_button` + `set_stable_button()` / `get_values()`（覆盖输出）
- `serial_thread` 防抖确认后调用 `set_stable_button()`，WS 始终读稳定值

---

### 阶段 5 — 非对称防抖

**问题**：对称防抖（120ms/2f）无法同时满足"低误触发"和"高检出率"——加大门限漏检，缩小门限误触发。

**方案**：press/release 独立阈值：
- press（快速确认）：默认 80ms / 1f
- release（慢速确认）：默认 180ms / 2f

**实现**：`serial_thread` 按 `candidate_button_state` 选对应阈值。

---

### 阶段 6 — Button 候选位扫描

**问题**：byte 12 检出率低（源位活动很少），byte 7/11 噪声极大，需要系统性找最优位。

**方案**：新增 `--button-scan-candidates` 并行扫描多个字节，每帧统计各位的 transitions / rise / fall / ones_ratio，定期输出 `button_scan_summary` 事件。

**分析方法**：
1. 按"预期 8 次按压 → 理想 rise=8, fall=8"对候选位打分
2. 检查空闲期 transitions 确认噪声水平

**结论**（`serial_20260503_054511.jsonl`）：

| idx | rise | fall | ones% | 结论 |
|-----|------|------|-------|------|
| **9** | **8** | **8** | 97% | ✅ 最优，active-low |
| 17 | 7 | 7 | 2% | ❌ 空闲误触发 7 次 |
| 12 | 1 | 1 | 99.7% | ❌ 漏检严重 |
| 7/11 | 80/90 | 79/90 | ~52% | ❌ 纯噪声位 |

**关键发现**：byte 9 的 ones%=97% 说明是 active-low——空闲时 bit=1，按下时 bit=0。需要 invert 逻辑。

---

### 阶段 7 — Invert 支持 + Argparse 默认值 Bug 修复

**问题 1**：byte 9 是 active-low，代码没有 invert 支持。

**修复**：新增 `legacy_button_invert` 参数，`_parse_legacy_packet` 中：
```python
raw_bit = 1 if (packet[button_idx] & mask) else 0
values["button"] = 1 - raw_bit if invert else raw_bit
```

**问题 2**：argparse `--legacy-button-invert` 用 `store_true` + `default=False` 覆盖了全局常量 `True`；`--button-press-debounce-*` 用 `default=None` + symmetric fallback 覆盖了 80ms 默认。

**修复**：
- `--legacy-button-invert` 改为 `BooleanOptionalAction`，`default=LEGACY_BUTTON_INVERT`
- press/release debounce 参数直接 `default=BUTTON_PRESS_DEBOUNCE_MS` 等全局常量，移除 symmetric fallback

---

### 阶段 8 — Press 阈值最终调优

**问题**：改完 invert + 正确默认值后，`serial_20260503_055212.jsonl` 仍只检出 4/8。

**根因分析**：
```
帧间隔:    ~156ms
短按持续:  ~154ms（约 1 帧）
press_ms:  80ms

第 1 帧：按键变为 1，elapsed=0ms < 80ms → 不触发
第 2 帧：按键已变回 0（释放），候选重置 → 永远触不了
```

**仿真验证**（模拟防抖逻辑在离线日志上重放）：
- 80ms/1f → 4/8 检出 ✅ 与实测一致
- 0ms/1f  → 8/8 检出 ✅ 完美

**修复**：`BUTTON_PRESS_DEBOUNCE_MS = 0`（第一帧立即确认按下，由 release 的 180ms/2f 防止虚假释放）

---

### 阶段 9 — Scratch 3.0 / TurboWarp 扩展接入

**背景**：这个开发板原本不支持 Scratch 3.0 架构。协议调通后，需要把桥接数据真正接到可用的图形化编程环境里。

**方案**：
- 开发 `extension.js`（JavaScript 扩展脚本），在 TurboWarp 中通过“本地文件加载”直接注入扩展
- 扩展通过 WebSocket 连接本地桥接服务（`ws://127.0.0.1:8765`），实时读取 `slider/light/sound/button/A/B/C/D`
- 对外提供 reporter/boolean block，确保与 Scratch 使用习惯一致

**关键修复（重复注册）**：
- 本地文件重复加载扩展时，会出现多实例并存、重复连接、数据竞争等问题
- 通过全局单例键（`window._picoboardExtensionInstance`）实现“先卸载旧实例，再注册新实例”
- 卸载时主动关闭旧 WebSocket，避免僵尸连接持续重连

**结果**：TurboWarp 可稳定本地加载扩展，且多次加载不会重复注册，完成“协议打通 → Scratch3 可用”的闭环。

---

## 关键方法论

### 分析闭环
```
修改参数 → 采集日志（前N秒空闲 + 按键M次）→ 离线分析脚本
    ↓
  对齐率、raw 翻转次数、稳定边沿数、空闲误触发
    ↓
  参数回写默认值 → 下一轮
```

### 核心度量指标
| 指标 | 健康值 | 说明 |
|------|--------|------|
| alignment_ratio | ≥ 0.995 | legacy 包对齐质量 |
| raw_transitions | == 2×N | N 次按压对应 N rise + N fall |
| stable_edges | == 2×N | 防抖后应与 raw 一致 |
| idle_false_triggers | == 0 | 空闲期零误触发 |
| hold_ms | 100~800ms | 正常短按节奏 |

### Argparse 默认值原则
- 全局常量存默认值（单一真相来源）
- argparse `default=` 直接引用全局常量
- 布尔 flag 用 `BooleanOptionalAction` 而非 `store_true`（支持 `--no-xxx`）

### 防抖参数设计原则
- `press_ms` 要 < 最短真实按压持续时间（通常 < 1 帧间隔）
- `release_ms` 要 > 最长真实抖动窗口（通常 2 帧间隔）
- 帧率（~6Hz @ 156ms/frame）是核心约束，阈值必须对齐帧粒度

---

## 文件索引

| 文件 | 用途 |
|------|------|
| `PicoBoardBridge.py` | 主桥接程序，含协议解析、防抖、WS、日志 |
| `extension.js` | TurboWarp 本地扩展脚本，负责 block 注册、WS 接入、重复注册防护 |
| `scripts/analyze_log.py` | 日志综合分析（config/edges/raw/alignment） |
| `scripts/scan_candidates.py` | 候选 button 位离线扫描评分 |
| `scripts/simulate_debounce.py` | 离线仿真防抖逻辑，验证参数 |
| `logs/*.jsonl` | 采集日志，格式 serial_YYYYMMDD_HHmmss.jsonl |
