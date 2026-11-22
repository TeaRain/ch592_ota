# 燃气校准界面简化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将燃气校准合并为一个中文、保存即生效的 number，同时隐藏但保留旧按钮兼容。

**Architecture:** 复用现有 `Ch592OtaCoordinator.async_calibrate_gas()`，不改燃气模型或 BLE。NumberEntity 保存时直接校准；旧 ButtonEntity 通过 entity registry 标记 integration hidden，unique_id 与 press 行为不变。

**Tech Stack:** Home Assistant custom integration、Python unittest。

**Spec:** `docs/superpowers/specs/2026-11-22-gas-calibration-ui-design.md`

## Global Constraints

- 保留 `number.*_gas_target` 与 `button.*_write_gas_value` 的 unique_id 和现有实体兼容。
- 输入是机械表完整真实读数；校准不得修改 raw、consumption 或调用 BLE。
- 不调整其他设备页实体或 OTA 代码。

## Review Focus

- 未收到 raw 时保存应抛出既有 HomeAssistantError，不得伪造 gas_total。
- 重复保存相同真实读数只重置 offset/total，不得累计 consumption。
- 旧按钮隐藏后，已有自动化调用 press 仍应校准。
- HA 重载后注册表隐藏状态与 number 实体身份保持不变。
- 校准输入的小数到整数单位换算继续使用 round(value*1000)，避免浮点截断。

以上场景由 Task 1 的立即校准、原按钮兼容及既有 GasAccounting 测试覆盖；实际 HA 设备页显示在发布后人工验收。

---

### Task 1: 合并校准操作并隐藏旧按钮

**Files:**
- Modify: `custom_components/ch592_ota/number.py`
- Modify: `custom_components/ch592_ota/button.py`
- Test: `tests/test_coordinator_reliability.py`
- Modify: `custom_components/ch592_ota/manifest.json`、`CHANGELOG.md`、`README.md`

**Interfaces:**
- Consumes: `await coordinator.async_calibrate_gas(actual_units: int)`.
- Produces: `Ch592GasTargetNumber.async_set_native_value(value: float)` 保存即校准；旧按钮 `async_press()` 保持不变。

- [ ] **Step 1: 写失败测试**

新增 `test_saving_calibration_number_calibrates_immediately`：raw=1000、consumption 已有增量后保存 0.900，断言 total=900、offset=-100、raw=1000、consumption 不变。新增 `test_legacy_calibrate_button_is_hidden_but_still_works`：加载按钮后断言 registry `hidden_by=integration`，再 press 断言校准成功。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -I tests/test_coordinator_reliability.py -v`
Expected: number 保存后 total 尚未校准；registry 没有 integration hidden 记录。

- [ ] **Step 3: 实现最小改动**

Number 名称改为 `燃气表实际读数（保存即校准）`，setter 保存整数单位并 await 校准。Button 在 `async_added_to_hass()` 中通过 `homeassistant.helpers.entity_registry` 将自身标为 `RegistryEntryHider.INTEGRATION` 隐藏，保留 listener 和 press。

- [ ] **Step 4: 全量验证**

Run: `python3 -I -m unittest discover -s tests -p 'test_*.py' -v`
Expected: 全部测试通过。

- [ ] **Step 5: 发布**

版本改为 0.7.3，更新变更记录和校准说明，构建 ZIP，提交并同步 GitHub Release。
