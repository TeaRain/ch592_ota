# Changelog

## 0.3.4

- 配合固件 1.2.9：设备在 `INFO` 响应里上报能力位，支持后 PROGRAM 分片从
  64 提升到 240 字节（按 256 字节 Flash 页拆分由设备侧完成）
- 旧固件（无能力位）继续用 64 字节，不受影响
- 安装失败时的分片回退阶梯仍为 `240/192/128/64/32/16`

## 0.3.3

- **上传大幅提速**（一次 20 分钟 → 预计 4~6 分钟，功耗同步下降）：
  - PROGRAM 阶段改用无响应写入（`write without response`），每 32 片才读一次状态，
    不再每片做一次「写 + 读」往返；代理不支持时自动退回带响应写入
  - VERIFY 阶段分片从 64 字节提到 MTU 允许的 240 字节（校验只比较 RAM 与 Flash，
    不受 256 字节页限制），往返次数减少约 4 倍
  - 状态读取失败时自动重试两次并退避，不再一次读超时就终止整个上传
- `ota_status` 的 `phase` 属性显示实际 `chunk` / `verify` 分片大小

## 0.3.2

- OTA 广播名匹配改为“包含 ota”（不区分大小写），不再要求与
  `GasMeter OTA` 完全一致
- 自动升级失败且上传进度 < 5% 时自动重试一次（代理首次连接失败很常见）；
  已经传上去一部分的失败不重试，避免重复上传

## 0.3.1

- OTA 模式识别更稳：除了 0xFEE0 服务 UUID，还匹配广播名 `GasMeter OTA`
  和 service data 里的 fee0
- `binary_sensor.*_ota_mode` 增加诊断属性：`ads_total`、`last_name`、
  `last_service_uuids`、`last_service_data_uuids`
- 修复某些代理不把 OTA 广播的 UUID 列在上报里的情况下自动升级不触发的问题

## 0.3.0

- **长按 B 即自动升级**：设备进入 OTA 模式（广播 `0xFEE0`）时，若固件仓库有
  更新版本，集成自动下载并开始 OTA，不需要再在 HA 点“安装”
- 新增选项 `auto_install_on_ota_mode`（默认开启），可关闭恢复手动模式

## 0.2.2

- 修复 HA 2026.11 下集成无法加载：`CONNECTION_BLUETOOTH` 应从
  `homeassistant.helpers.device_registry` 导入（`homeassistant.const` 里没有），
  之前导致 `entity.py` ImportError、所有平台加载失败

## 0.2.1

- 新增 `button.*_check_for_updates`：立即拉取固件清单，不用等 30 分钟定时
- 检查失败时按钮会报错并带上原因（例如清单 URL 403）

## 0.2.0

- 新增固件清单轮询（默认 30 分钟）：从 Gitee/GitHub 的固件仓库自动检查新版本
- 有新固件时 `update.*` 实体自动提示；安装时直接下载镜像并校验
  size / CRC32 / SHA-256，无需本地固件文件
- 选项里可配置 `manifest_url`，留空关闭自动检查；拉取失败回退本地固件
- `update.*` 的 `extra_state_attributes` 增加 upstream_version、manifest_error 等
- 固件工程与镜像发布到 https://gitee.com/zigbridge/ch592-gas-broadcaster

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
