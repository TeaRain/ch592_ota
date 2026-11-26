# ESPHome GATT Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 HA 0.8.2，消除普通/OTA 模式切换时 ESPHome 复用旧 GATT 数值句柄导致的唤醒和上传失败。

**Architecture:** 在 HA 已选择真实 backend 后清代理缓存，严格断开并无缓存重连；按代理 cache 身份去重。清缓存、断连和取消均有明确期限与连接所有权。

**Tech Stack:** Python 3、Home Assistant、bleak、bleak-retry-connector、ESPHome Bluetooth Proxy、unittest、HACS。

**Spec:** `docs/superpowers/specs/2026-11-26-esphome-gatt-cache-design.md`

## Global Constraints

- 直接在当前 `dist/gitee` master 工作副本执行，不使用 worktree；保留 const、strings/translations 的既有工作区差异。
- 版本 0.8.2；只提交本次源码、测试、manifest 和文档。
- 不修改固件、不触发 OTA、不修改燃气校准，不把自动化通过当作实机验收。

## Review Focus

- HA wrapper 连接前 `_backend=None`，不得以预连接 backend 清缓存的测试结果代替真实行为。
- 普通与 OTA 两个方向都必须重新发现服务，不能只修 `0x88` 后在 INFO 再次遇到旧句柄。
- 中间断连失败或取消不得遗失仍存活的 client；只有确认断开后才允许重连。
- clear 必须受普通剩余期限或 fresh 30 秒期限约束，多代理不能漏清或无限重复清理。
- HACS ZIP 必须从最终提交重建，用户已有换行符修改不得混入。

---

### Task 1: 以真实 HA wrapper 行为固定缓存和清理边界

**Files:**
- Modify: `custom_components/ch592_ota/ble_ota.py`
- Test: `tests/test_remote_ota.py`

- [x] **Step 1: 添加旧句柄、clear 期限、严格断连和取消所有权测试**

使用连接前 `_backend=None`、连接后选择 ESPHome backend 的替身，覆盖 52→16、16→52、clear 挂起、disconnect 错误和 disconnect 期间取消。

- [x] **Step 2: 确认各项测试在旧实现失败**

分别观察到 `Invalid handle`、连接次数不符合、clear 越过期限、断连错误被吞和 `_client=None`。

- [x] **Step 3: 实现连接后清缓存、无缓存重连和 client 登记**

每个新代理 cache 清一次；严格确认断开；清缓存有期限；外层通过回调立即持有 client。

- [x] **Step 4: 全量回归和独立审查修正**

HA 70/70 通过；审查提出的两项 Important 均以新增失败测试修正，再次全量通过。

### Task 2: 版本、产物和双平台发布

**Files:**
- Modify: `custom_components/ch592_ota/manifest.json`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Add: 本计划和设计文档
- Git tag: `v0.8.2`

- [ ] **Step 1: 更新版本与实机边界文档**

记录 0.8.1 的真实 `Invalid handle 52`、依赖源码根因、最终连接/清理行为和 70 项测试，保留实机待验边界。

- [ ] **Step 2: 运行发布前全量验证**

运行 HA 70 项、固件 14 项及 Python 编译和 scoped diff check，确认仅有本任务文件进入提交。

- [ ] **Step 3: 提交、推送并创建 Gitee/GitHub v0.8.2 Release**

使用规定署名提交，正常推送 master 和 tag，不 force push，经现有镜像/Actions 流程发布 GitHub。

- [ ] **Step 4: 从最终提交重建并核验 HACS 产物**

生成 23 个文件成员的 ZIP 和 SHA-256 sidecar；逐字节比较 Git blob、ZIP、双平台 raw 和 GitHub tag 源归档，确认 manifest 为 0.8.2。
