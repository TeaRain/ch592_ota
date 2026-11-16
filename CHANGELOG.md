# Changelog

## 0.6.0

配合固件 1.4.1，采样阈值可以远程调整（默认 100，可设 20~2000）：

- 新增 `number.*_pulse_threshold`（草稿）、`button.*_write_pulse_threshold`
- 新增服务 `ch592_ota.set_threshold` / `ch592_ota.cancel_threshold`
- 设备端新增 `0x87` 命令，阈值存 DataFlash 块 3；`INFO` byte13–14 上报当前阈值，
  能力位 bit3、协议版本 6。`sensor.*_ota_status` 增加 `device_threshold` /
  `threshold_pending` / `threshold_result`
- 待写入优先级扩展：阈值与燃气值可以同时排入队列，按 B 后**先写阈值、再写燃气**
  （阈值不复位，燃气写入才复位），两者都不会触发自动升级
- 原「取消待写入」按钮改为 `Cancel pending writes`，一次清空两种待写入
  （`unique_id` 未变，现有实体保留）

## 0.5.1

- 固件 < 1.4.0 时调用 `set_gas` 给出明确提示：
  「设备固件不支持写燃气值（需要 1.4.0 及以上），请先升级固件」（原来的
  `设备返回错误状态 0xFE` 太隐晦）

## 0.5.0

配合固件 1.4.0，可以在 HA 里直接写燃气值，不用再长按 +/-：

- 新增 `number.*_gas_target`（草稿，**不会自己写设备**）、
  `button.*_write_gas_value`、`button.*_cancel_pending_gas_write`
- 新增服务 `ch592_ota.set_gas` / `ch592_ota.cancel_gas`
- **待写入优先级**：设备不在 OTA 模式时点写入 → 记为待写入（15 分钟）；
  长按 B 后集成**先写燃气值、跳过自动升级**。升级进行中点写入会被拒绝
- `sensor.*_ota_status` 增加 `gas_pending` / `gas_result` 属性；
  `number` 实体带 `device_value_m3`（设备当前读数）
- 顺带解析 BTHome `0x4C` 燃气对象，草稿默认显示设备当前值

设备要求：固件 ≥ 1.4.0（`INFO` 能力位 bit2）。老固件调用会报「设备返回错误状态」。

## 0.4.9

- 本地固件文件缺失时不再打 warning（云端清单已成为主力，本地镜像只是兜底）。
  只有配置里**显式填了固件路径**时才提示，否则走 debug。

## 0.4.8

- 新增开发用服务 **`ch592_ota.reload_code`**：从磁盘重新导入集成代码并重载配置
  条目，**不用重启 HA**（几秒完成）。改了 `ble_ota.py` / `coordinator.py` 之类
  的代码后：复制文件 → 开发者工具 → 动作 → `ch592_ota.reload_code` → 继续测。
  注意：`manifest.json` 的版本号、以及新增平台（如新实体类型）仍需真正重启；
  攒够一次改动后建议重启一次。

## 0.4.7

- **修复升级成功后卡在 98 % 报失败**：普通 BTHome 广播的 `service_uuids` 里也会带
  `0xFFE0/0xFEE0`（代理在之前 OTA 连接时做 GATT 发现后缓存下来的），于是每条普通
  广播都被当成 OTA 广播：`firmware_version` 不再更新、`ota_mode` 也不再清零。
  结果固件明明已经是 1.3.1，集成还认为设备是 1.3.0，等满 180 秒报「未看到目标版本」。
- 现在用 **BTHome 的 0xFCD2 服务数据**做判据：OTA 固件从来不发它。
  - 有 fee0 且**没有** fcd2 → OTA 广播
  - 有 fcd2 → 普通广播（更新版本、清除 OTA 模式）

## 0.4.6

- **修复 0.4.4 引入的 `NameError: OtaError is not defined`**：`coordinator.py`
  没有导入 `OtaError`，导致任何上传异常都被这个 NameError 覆盖，真实原因看不到
- **HA 重启后不再自动升级遗留的 OTA 模式**：本次启动必须先看到过普通 BTHome 广播，
  才会认为 OTA 广播是「刚按了 B」；否则只在 `auto_note` 里说明原因
- **连接失败不再整体重传**：只有已经传出去数据（进度 > 10%）之后的断链才重传 3 轮，
  连不上就立刻报错并提示「请重新长按 B 键」，不再卡在 1% 好几分钟

## 0.4.5

- **升级过程中持续采样电压**：每传 16 KB 重新读一次设备 `INFO`（约 14 次），
  `vdd_mv` 属性在上传期间会跟着刷新，不再只在连接时采一次
- 新增 `vdd_min_mv`：本次上传过程中见到的最低电压，升级结束后保留，
  用来判断电池余量（新电池满电静态 ~3.0 V，负载下允许短暂下探）
- 采样失败只记 debug，绝不影响上传

## 0.4.4

- **链路中断后自动整体重传**：上传过程中断链（不是命令报错）时会重新连接、
  重新擦写上传，最多 3 轮。设备掉线后仍会在 OTA 模式停留 8 分钟，而一次
  128 字节上传只要 ~35 秒，重传比放弃划算得多
- CRC32 校验前停 0.5 秒：设备计算 216 KiB 校验期间会阻塞蓝牙任务，
  先让链路安静一下再发命令

## 0.4.3

- `sensor.*_ota_status` 增加三个实测属性：`chunk`（本次实际分片）、
  `vdd_mv`（设备在 OTA 模式测到的电压）、`duration_s`（本次升级总耗时），
  另加 `mtu`、`verify_chunk`、`device_protocol`
- 这些属性在每次升级开始时重置，升级结束后保留，方便对照记录

## 0.4.2

- PROGRAM 分片上限 240 → **128**。ESPHome 代理对超过 128 字节的写入会退化成
  prepare/execute 长写（每片要十几次往返，慢且中途容易断），实测 240 字节跑到
  12% 就失败、然后整个上传从 1% 重新来一遍。128 字节是单包写入，也不再跨
  256 字节页。

## 0.4.1

- CRC32 校验不通过时自动退回逐字节校验（会报出第一个不一致的偏移）。
  这样即使设备端 CRC 命令有问题也不会变成死路，只多花几分钟。

## 0.4.0

配合固件 1.3.0，把升级时间压到 1~2 分钟：

- **校验改成一次往返**：设备新增 `0x85` 命令，自己算 Image B 的 CRC32（与
  `zlib.crc32` 一致）；客户端比对一次即可，省掉整镜像重传的逐字节校验
  （原来约占总时间 40%）。旧固件不支持时自动退回逐字节校验
- **OTA 模式电压上报**：设备在 `INFO` 响应里带上 PA9 实测电压，
  `sensor.*_ota_status` 的 `vdd_mv` 属性显示升级过程中的真实电压
- 能力位 bit1 = CRC32 校验，协议版本 4

## 0.3.7

- 修复升级完成后进度卡在 98 % 两三分钟：只要设备已经广播出目标版本就直接判成功，
  不再额外等一条新的广播（开机那条广播常常在记录基准计数之前就到了，
  于是要多等到下一次 60 秒广播才收尾）

## 0.3.6

- **修复校验阶段卡死**：VERIFY 分片从 240 退回 64。`FLASH_ROM_VERIFY` 和
  `FLASH_ROM_WRITE` 用同一套 256 字节页机制，240 字节校验写入会卡住
  （实测 1 分钟只前进 1%），PROGRAM 的 240 字节分片不受影响
- 校验失败时报出具体偏移，例如 `校验失败 @0x03A40: 设备返回错误状态 0x01`
- 连接失败时的错误信息补上处理建议：「设备已退出 OTA 模式，请重新长按 B 键 3 秒后重试」

## 0.3.5

- **修复长按 B 不自动升级**：OTA 广播改由 0xFEE0 判定，且**先于** BTHome 解析，
  不再被同一地址的 BTHome 广播覆盖回普通模式
- 不再用设备名判断 OTA 模式：HA 会把这个地址的设备名缓存成 `GasMeter OTA`，
  普通广播也会带这个名字
- `binary_sensor.*_ota_mode` 新增 `auto_note` 属性，直接显示自动升级没启动的
  原因（选项关闭 / 已有任务 / 没有清单 / 没有新固件）或「开始自动升级到 x.y.z」

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
