# Remote Wake Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 HA CH592 OTA 集成 0.8.1，使普通唤醒连接覆盖完整 60 秒广播周期，并暴露每次失败的真实诊断。

**Architecture:** 普通唤醒直接调用真实 `BleakClient.connect()`，以最长 65 秒期限和 1 秒退避覆盖下一广播窗口，协调器把 240 秒总剩余期限传入并约束每次尝试。上传、事件等待和模式切换保持；最终错误补充次数和最后原因，状态 Sensor 暴露相同字段，普通唤醒失败不误报已退出 OTA。

**Tech Stack:** Python 3、Home Assistant、bleak-retry-connector、unittest、HACS、Git/GitHub/Gitee。

**Spec:** `docs/superpowers/specs/2026-11-26-remote-wake-window-design.md`

## Global Constraints

- 直接在 `/home/ops/nas/projects/Broadcaster-CH592/dist/gitee` 当前 master 工作副本执行，不使用 worktree。
- 保留 `const.py`、strings/translations 中已有换行符差异，不纳入提交；65 秒常量放在干净的 `ble_ota.py`。
- 不修改固件、不触发 OTA、不修改燃气校准，不把自动化测试等同于普通窗口实机验收。
- 版本为 0.8.1；只暂存本任务源码、测试、文档和 manifest。

## Review Focus

- 普通唤醒 65 秒是否误改正式上传 30 秒：Task 1 分别检查两条连接路径。
- 取消和 240 秒总上限是否保留：Task 1 跑现有协调器可靠性全套测试。
- 最后错误是否被笼统超时覆盖：Task 1 用实际连接异常验证最终消息和 Sensor 属性。
- 既有工作区换行符差异是否混入发布：Task 2 核对提交文件和 GitHub/Gitee 内容。
- 实机是否仍有第二层代理/GATT 问题：发布说明明确待 0.8.1 重试，不能宣称实机已修复。

---

### Task 1: 用失败测试固定跨窗口连接和诊断

**Files:**
- Modify: `custom_components/ch592_ota/ble_ota.py:515-551`
- Modify: `custom_components/ch592_ota/coordinator.py:979-1034`
- Modify: `custom_components/ch592_ota/sensor.py:108-132`
- Modify: `tests/test_remote_ota.py`
- Modify: `tests/test_coordinator_reliability.py`

**Interfaces:**
- Produces: `ble_ota.REMOTE_OTA_CONNECT_TIMEOUT = 65.0`；状态属性 `wake_attempts`、`wake_error`。
- Preserves: 正式 OTA 上传连接 `timeout=30.0`、总唤醒上限 240 秒、取消和新广播序号语义。

- [ ] **Step 1: 添加三项失败测试**

普通唤醒测试记录 `establish_connection` 的 timeout 并断言为 65.0；协调器测试在一次 `connect timeout` 后到达总 deadline，断言异常包含 1 次和原错误；Sensor 测试写入 attempts=2/error=`proxy disconnected`，断言属性原样可见。

- [ ] **Step 2: 运行测试并确认旧代码失败**

Run: `python3 -I -m unittest discover -s tests -p 'test_remote_ota.py' -v` 和协调器对应测试。
Expected: 新断言分别因 timeout 10、笼统最终错误和缺少属性而失败。

- [ ] **Step 3: 最小实现三处生产修改**

新增普通唤醒专用 65 秒常量并只用于 `async_request_ota`；最终超时消息包含次数和最后错误；状态 Sensor 添加两个字段。

- [ ] **Step 4: 运行定向与全量测试**

Run: 两个定向测试文件，再运行 `python3 -I -m unittest discover -s tests -v`。
Expected: 全部通过，测试数从 58 增至 61。

### Task 2: 文档、独立审查和 HACS 发布

**Files:**
- Modify: `custom_components/ch592_ota/manifest.json`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Add: 本计划和设计文档
- Git tag: `v0.8.1`

**Interfaces:**
- Consumes: Task 1 及最终审查修复后的 64 项测试和生产行为。
- Produces: HACS 可发现的 GitHub v0.8.1 tag/Release 及 Gitee 备份。

- [ ] **Step 1: 更新版本与实机边界文档**

记录 300 ms/60 秒与旧 10 秒超时的实际失败、真实 connect/窗口内重试/总期限修复、诊断字段和 64 项测试；明确仍需升级集成后实机重试。

- [ ] **Step 2: 全量复核并独立审查**

重跑 HA 64 项及固件 14 项，检查 manifest 0.8.1、既有工作区修改未入提交。独立审查发现 connector 参数不控制真实 connect、立即错误提前返回、总期限和按键提示问题后，已各以失败测试修复并全量复验。

- [ ] **Step 3: 提交、推送和创建 Release**

以 `fix: cover normal advertising window during OTA wake` 开头并带规定署名提交。推送 Gitee master/tag，创建 Gitee Release；使用现有 GitHub 通道同步并创建 v0.8.1 Release。

- [ ] **Step 4: 回读 HACS 发布内容**

确认 GitHub/Gitee tag 指向目标提交，线上 manifest 为 0.8.1，tag 源归档包含修复后的三个生产文件。最终给出 URL、测试证据和实机待验项，不触发设备升级。
