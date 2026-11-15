# Changelog

## 0.1.2

- 新增 HA 原生固件更新实体 `update.*`：设置 → 更新 / 设备页直接安装，带进度
- OTA 实体通过蓝牙连接并入 BTHome 设备页，燃气/电量/信号/OTA 状态集中在一页
- 新增 `binary_sensor.*_ota_mode`（是否已进入 OTA 模式）
- 新增 `button.*_abort_ota`，取消升级不再需要调用服务
- 修改固件路径后自动重载，更新实体重新读取 `.bin.json`
- 修复 BTHome 广播与 OTA 广播同时出现时 `ota_mode` 被误置回的问题

## 0.1.1

- 默认分片 240 → 64 字节，失败自动降 64 / 32 / 16（修复 10% 处的链路超时）
- `flash` 服务新增 `chunk` 字段（16 的倍数，16~64）
- 单次 OTA 超时 600 s → 1800 s
- `ota_status` 的 `phase` 属性显示实际 MTU / 分片，便于排查
- 读取响应失败包装为可重试错误

## 0.1.0

- 首个版本：通过 HA 蓝牙代理直连 CH592，完成 WCH BackupUpgrade OTA
- BTHome `0x53` 固件版本显示、升级后版本核对
- `ch592_ota.flash` / `ch592_ota.abort` 服务
- 固件首字 JAL 校验与 CRC32 校验
