# 燃气校准界面简化设计

## 目标

把“输入校正值”和“写入校准”从两个含义不清、设备页分离的英文实体，改成一个直观操作：
`燃气表实际读数（保存即校准）`。

## 已选方案

用户选择合并成一项，而不是保留输入和按钮两步操作。

- 保留 `number.*_gas_target` 的 unique_id、entity_id 和 NumberEntity 类型。
- 保存数值时立即执行 `async_calibrate_gas()`；输入的是机械表完整真实读数，不是 offset。
- 校准仍只更新 HA offset / gas_total，不通过 BLE 写设备，不改变 raw 或统计 consumption。
- 保留 `button.*_write_gas_value` 及其 unique_id、现有 `async_press()` 行为，避免破坏已有自动化；
  通过实体注册表把它设为 integration hidden，不再作为设备页操作入口。
- 不修改设备页其他实体，不通过更改 unique_id 追求排序。

## 备选方案及取舍

保留中文步骤名“输入 → 按钮”可以维持明确确认，但 HA 默认设备页无法保证 number 与 button
严格相邻，不能完整解决用户指出的问题，因此未选。

## 验收

1. 设置 number 为 0.900，raw=1.000 时，gas_total 立即变为 0.900，offset=-0.100，
   consumption 保持原值，raw 不变，全程无 BLE 操作。
2. 原校准按钮加载后被标记为 integration hidden，但其 press 仍可完成校准。
3. 全部 HA 回归测试通过；发布新版本后在实际 HA 设备页确认只显示一个中文校准入口。
