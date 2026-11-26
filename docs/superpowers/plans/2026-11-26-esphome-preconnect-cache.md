# ESPHome 预连接缓存修复实施记录

**Goal:** 修复 HA 0.8.2 普通窗口 240 秒空错误，并避免为跨模式 GATT 刷新强制进行二次 BLE 连接。

**Spec:** `docs/superpowers/specs/2026-11-26-esphome-preconnect-cache-design.md`

## Global Constraints

- 直接在 `dist/gitee` 当前工作副本修改；不纳入用户既有 const/strings/translations 改动。
- 保持协调器 240 秒总期限、普通单次 65 秒、fresh 清缓存 30 秒和上传 30 秒/3 次参数。
- 不硬编码 GATT 句柄，不修改固件、燃气校准或设备状态。
- 所有生产修复先取得失败测试证据；独立审查 Important 必须修正后才能发布。

## 已完成任务

- [x] 读取 0.8.2 实机状态和历史，确认一次唤醒 240.1 秒、`wake_attempts=1`、空 `wake_error`，广播持续且设备仍 1.6.1。
- [x] 阅读 HA 2026.11.3、habluetooth 7.1.2、bleak-esphome 4.0.0、bleak-retry-connector 4.7.1、aioesphomeapi 46.25.0 的相关实现，验证 connector/cache/API 契约。
- [x] 新增 6 项预清理与期限回归并在旧实现上观察失败；实现所有可见 ESPHome 路径的 host/remote 预清理、cache 身份去重和单次 65 秒期限。
- [x] 保留未识别 connector 的 0.8.2 回退；fresh 上传与配置传递 hass，非 fresh 上传路径不变。
- [x] 完整 HA 76 项和固件 14 项通过后进行独立只读审查。
- [x] 修正审查 Important：无 ESPHome 导入/本地唤醒测试、普通和 fresh 回退的 `clear_cache() is False` 测试均 RED→GREEN。
- [x] 对 fresh 阈值配置做漏传 hass 的定向 mutation，测试由 2 次连接失败后恢复为 1 次连接通过。
- [x] 最终 HA 80/80、固件及发布交叉检查 14/14、Python 编译、manifest JSON 和 scoped diff check 通过。
- [x] 更新 manifest、CHANGELOG、README 和本设计/实施记录，版本为 0.8.3。

## 发布边界

发布只包含上述代码、测试和文档。独立 HACS ZIP 必须从最终提交重建并逐字节核验，Gitee/GitHub tag、raw、源归档和 ZIP sidecar 必须一致。未进行 0.8.3 实机普通窗口验证；不触发设备 OTA，不修改燃气校准。
