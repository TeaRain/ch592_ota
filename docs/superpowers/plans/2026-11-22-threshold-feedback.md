# P/N 阈值回显 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P/N 调整后 300 ms 内在 HA 看到设备当前阈值。

**Architecture:** 固件用标准 BTHome `0x3E count32` 携带阈值并在调整时补发；HA 复用 BTHome 解析和 Coordinator，新增只读中文 Sensor。布局由纯 C 小头文件统一，HA 解析保持向后兼容旧固件。

**Tech Stack:** CH592 C、BTHome v2、Home Assistant Python、unittest、INT_SOFT RISC-V 构建。

**Spec:** `docs/superpowers/specs/2026-11-22-threshold-feedback-design.md`

## Global Constraints

- 固件 1.4.9、HA 0.7.4、OTA 协议 8；不改 ADC、gas、version、CRC、IAP 的语义。
- 版本字符串长度 5 时广播总长必须等于 31，AD length 必须等于 27。
- 平时 60 秒广播不变；只在 P/N 调整和松手保存时补发。

## Review Focus

- 旧固件没有 `0x3E` 时 HA 阈值应为 None，不得影响 gas/version 解析。
- 阈值 20/2000 边界均按 uint32 小端编码，不能截成 8 位。
- 长按连续调整与松手短按都要留下最终值补发。
- 版本偏移必须随新增 5 字节同步移动，不能覆盖阈值或留下旧长度。
- 新实体 unique_id 不得与现有 number `*_threshold` 冲突。

这些分别由 HA 解析测试、C 布局边界测试、按键补发代码检查和实体测试覆盖；真实显示在发布后验收。

---

### Task 1: 固件阈值广播与补发

**Files:**
- Create: `../firmware_repo/APP/include/bthome_threshold.h`
- Modify: `../firmware_repo/APP/broadcaster.c`、`APP/include/broadcaster.h`
- Test: `../firmware_repo/tests/test_threshold_feedback.c`、`test_threshold_feedback.py`

**Interfaces:**
- Produces: `BTHOME_THRESHOLD_OFFSET=19`、`BTHOME_VERSION_OFFSET=24`、`BthomeAdvertDataLength(5)=27`、`BthomeAdvertSize(5)=31`、`BthomeWriteThreshold(data, value)`。

- [ ] 写 C 失败测试：长度/偏移、20/2000 编码；先确认头文件不存在导致失败。
- [ ] 实现布局头文件并接入 broadcaster，P/N adjust 与 save 后设置 `s_ucBroadcasterCnt=1`，版本改 1.4.9。
- [ ] 用批准工具链编译，运行全部 Python/C 与 mret 测试并生成镜像。

### Task 2: HA 阈值解析与中文实体

**Files:**
- Modify: `custom_components/ch592_ota/bthome.py`、`coordinator.py`、`sensor.py`
- Test: `tests/test_bthome_threshold.py`、`tests/test_coordinator_reliability.py`

**Interfaces:**
- Consumes: `fields['threshold']: int`；Coordinator `data['threshold']`。
- Produces: `Ch592CurrentThresholdSensor.native_value -> int | None`，unique_id `*_current_threshold`。

- [ ] 写失败测试：完整 BTHome 同时解析 gas/version/threshold；Coordinator 和中文实体显示 140，旧广播不伪造阈值。
- [ ] 实现 `0x3E` 解析、Coordinator 更新及只读 Sensor。
- [ ] 运行完整 HA 测试，更新 0.7.3 → 0.7.4 文档与 ZIP。

### Task 3: 设备页中文化

- Modify: `binary_sensor.py`、`button.py`、`number.py`、`sensor.py`、`update.py`。
- 将固件版本、升级状态/进度、燃气三值、OTA 模式、检查/取消升级、阈值操作等显示名改为中文。
- 保留所有 unique_id、entity_id、属性键、服务和自动化接口；新增类标签测试。
- 仪表盘使用中文解释原始计数、真实读数、统计用气量和 OTA 状态。

### Task 4: 发布与实机验收

- [ ] 发布固件 1.4.9 和 HA 0.7.4 到两个 GitHub Release，校验 bin/源码哈希。
- [ ] 用户更新后按 P/N，确认约 300 ms 回显、松手最终值及重载持久化；同时观察本次协议 8 快速 CRC 耗时。
