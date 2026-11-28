# GasPulse 蓝牙燃气表

Home Assistant 自定义集成：通过 Home Assistant 的蓝牙代理（ESPHome Bluetooth
Proxy）采集和管理 **GasPulse 蓝牙燃气表**（CH592 硬件，WCH BackupUpgrade / OTA）。

设备端用 WCH 备份升级方案：OTA 镜像写入 Image B，校验通过后 IAP 在重启时拷贝到
Image A；任何一步失败都会继续跑旧固件。版本号通过 BTHome v2 文本对象 `0x53`
广播，升级完成后 HA 里能直接看到新版本。

- HACS / GitHub 集成仓库：https://github.com/TeaRain/gaspulse-ha
- Gitee 代码备份：https://gitee.com/zigbridge/gaspulse-ha（仅备份，不用添加到 HACS）
- 固件仓库：https://github.com/TeaRain/gaspulse-firmware ｜ Gitee 备份：https://gitee.com/zigbridge/gaspulse-firmware
- 旧 GitHub 地址由平台重定向，旧 Gitee raw 固件地址在兼容期内继续保留

## 当前版本与历史实测

> 当前为 **0.9.2 维护版**：GitHub 已改用 `gaspulse-ha` / `gaspulse-firmware`，旧地址保留重定向和固件回退。HA domain、集成目录、服务、实体 ID 与 OTA 协议均不变。

| 项目 | 值 |
|------|-----|
| 集成版本 | **0.9.2** |
| 固件版本 | **1.6.2** |
| 一次升级耗时 | 免按键新增普通窗口等待；上传仍用协议 8 单次 CRC32，实际总耗时尚待实机记录 |
| 供电实测 | CR2032 新电池，OTA 负载下 **3.04 V**，全程无下探 |
| 结论 | **CR2032 可以完成 OTA**；之前跑不下来是耗时太长（约 20 分钟） |

设备：MAC `0C:3D:5E:54:BC:6F`，HA 里是 BTHome 设备 “BTHome sensor BC6F”。

## 它是怎么工作的

**普通窗口免按键（固件 1.6.0+ / 集成 0.8.3+）**

- 普通 BTHome 周期窗口为 **300 ms、50 ms 间隔**，约 6 帧/分钟；采样周期不变。
- 点击安装后先下载并强制校验 size/CRC32/SHA-256，再等待新的普通广播并主动连接。
- 0.8.3 普通唤醒在真实 `client.connect()` 上恢复每次最长 **65 秒**期限，立即失败时在同一窗口内退避重试，覆盖下一分钟广播；协调器同时用 240 秒总剩余期限约束整个唤醒过程。正式上传连接参数仍为 30 秒。
- 0.8.3 在首次连接前通过真实 ESPHome connector 清理所有可见代理的旧 GATT 缓存，再以无缓存连接重新发现服务；普通 `0xFEE1` 句柄 16 与 OTA 句柄 52 不混用。未识别的连接路径保留连接后清缓存回退，代理明确清缓存失败会停止写入。
- `升级状态` 提供 `wake_attempts` 和 `wake_error`，最终超时同时显示次数和最后实际错误。
- 普通 GATT 只接受固定 `0x88 ENTER_OTA` 帧，设备写现有 OTA 请求、应答后复位；
  不开放普通模式 ERASE/PROGRAM/END，也不改变 IAP。
- 收到新的 OTA 广播后预清缓存，以无缓存连接重新发现 GATT，再复用原有上传；有待写阈值时仍先写阈值。
- 普通控制连接约 5 秒超时，等待唤醒最长 240 秒，取消会终止等待；没有常驻连接。
- 理论广播增量预算约 0.2～1 µA，**不是实测值**；Role 睡眠和主动扫描开销待测。

**设备端**（`peripheral.c` 的 OTA 分支）

- 长按 B 键 3 秒 → 写 DataFlash 标记 → 复位 → 启动可连接的 OTA 外设，
  广播服务 `0xFEE0`（特征 `0xFEE1`），不再发 BTHome 广播
- 命令：`0x80` PROGRAM、`0x81` ERASE、`0x82` VERIFY、`0x83` END、`0x84` INFO、
  `0x85` CRC32（1.3.0 起）、`0x86` 写燃气值（1.4.7 起停用）、`0x87` 写采样阈值；
  `0x88` 仅用于普通模式请求进入 OTA
- `END` 后切换 ImageFlag 并复位，IAP 把 Image B 拷到 Image A

**集成端**

- 用 `write without response` 流式写 PROGRAM，每 32 片读一次状态
  （以前是每片都「写 + 读」，这是原来 20 分钟的主要来源）
- 0.7.2 恢复 0.4.0 的单次 CRC32 快速校验：固件 1.4.8 / 协议 8 使用查找表快速
  计算；旧固件继续走有边界的逐块 VERIFY，避免阻塞式 CRC 拖断代理连接
- 自动升级默认关闭；在配置中显式开启后，也只有当前原始 BLE 包确认为 OTA 才会自动下载并开始升级，手动点击安装不受该开关影响

## 分片规则（重要，不要随意改）

| 阶段 | 分片 | 原因 |
|------|------|------|
| PROGRAM | **128**（上限 `OTA_CHUNK_MAX`） | ESPHome 代理对超过 128 字节的写会退化成 prepare/execute 长写，每片十几次往返，既慢又容易断链（240 字节实测跑到 12% 就失败） |
| VERIFY | **64**（`VERIFY_CHUNK_MAX`） | `FLASH_ROM_VERIFY` 与 `FLASH_ROM_WRITE` 共用 256 字节页机制，240 字节校验会卡死（实测 1 分钟只走 1%） |
| 设备能力 | `INFO` byte9 | bit0 = PROGRAM 按 256 字节页拆分（固件 ≥ 1.2.9）；bit1 = 支持 `0x85` CRC32（≥ 1.3.0）。老固件不上报能力位时自动按 64 字节传 |
| 失败回退 | 128 → 64 → 32 → 16 | 每降一档都会重新擦写整片，代价高，所以默认值要选对 |

## 安装

### 方式一：HACS / 市场（推荐）

HACS 只识别 GitHub 仓库：

1. HACS → 右上角 ⋮ → **自定义仓库**；
2. 添加 `https://github.com/TeaRain/gaspulse-ha`，类型选 **集成（Integration）**；
3. 搜索 “GasPulse 蓝牙燃气表” 安装，然后重启 HA。

**HACS 自定义仓库只填写 GitHub。** Gitee 仅作代码备份和固件清单/BIN 下载回退；
手动安装也可以使用 Gitee 备份（见方式二/三），但它不是 HACS 来源。

### 方式二：git clone

```bash
cd /config
rm -rf /tmp/ch592_ota_repo
git clone --depth 1 https://gitee.com/zigbridge/gaspulse-ha.git /tmp/ch592_ota_repo
# 旧 Gitee 备份 zigbridge/ch592_ota 冻结于 0.9.0，仅作历史回退，不要用于新装
cp -rf /tmp/ch592_ota_repo/custom_components/ch592_ota /config/custom_components/
rm -rf /tmp/ch592_ota_repo
```

### 方式三：下载 ZIP

- Gitee（新备份，公开）：`https://gitee.com/zigbridge/gaspulse-ha/repository/archive/master.zip`
- GitHub：`https://github.com/TeaRain/gaspulse-ha/archive/refs/heads/master.zip`
- 旧 Gitee 备份 `zigbridge/ch592_ota` 冻结于 0.9.0，仅兼容期回退用，不用于新装。

解压后把 `custom_components/ch592_ota` 复制到 `/config/custom_components/`。

### 0.8.x / 0.9.0 老用户升级到当前版本

- 在 GitHub HACS 中正常更新即可；**不要删除集成、设备或配置条目**，已有实体 ID、历史数据和自动化不需要修改。
- 如果旧 HACS 来源因缓存不跟随 GitHub 重定向而无法更新，把自定义仓库地址替换为 `https://github.com/TeaRain/gaspulse-ha` 并重新下载；不要卸载已有配置。
- 手动安装仍覆盖 `/config/custom_components/ch592_ota/`。该目录和 `ch592_ota.*` 服务是兼容标识，请勿自行改成 `gaspulse`。
- Gitee 不需要添加到 HACS；旧 Gitee 固件 raw 地址由集成自动用于回退。

装好之后：**设置 → 设备与服务 → 添加集成 → 搜索 “GasPulse 蓝牙燃气表”**。

需要 HA 的 `bluetooth` 集成，以及可连接的 ESPHome 代理：

```yaml
esp32_ble_tracker:
  scan_parameters:
    active: true
bluetooth_proxy:
  active: true
```

## 更新集成（不用每次都重启 HA）

复制文件后调用新服务，**1~2 秒**生效：

```
开发者工具 → 动作 → ch592_ota.reload_code
```

仍需真正重启 HA 的情况：

- 改了 `manifest.json`（版本号、依赖）
- 新增实体平台 / 新增文件（模块第一次导入）
- 连续热重载多次后出现重复实体之类的怪现象

建议：攒几次小改动热重载验证，稳定后重启一次收尾。

## 配置

| 字段 | 示例 | 说明 |
|------|------|------|
| 蓝牙地址 | `0C:3D:5E:54:BC:6F` | BTHome 设备的 MAC，大写 |
| 设备名称 | `CH592` | 备用名称；OTA 实体会并入 BTHome 设备页 |
| 固件路径 | `www/firmware/ch592_ota.bin` | 相对 `/config`，必须在 config 目录内 |
固件路径只是**兜底**：清单能拉到就以云端为准，拉不到才用本地 `.bin`。
只用云端升级的话，把路径留空、本地文件删掉即可（0.4.9 起不会再刷 warning）；
要保留离线兜底路径的话，记得每次发布固件时同步覆盖本地文件，否则会变成旧镜像。

### 固件自动检查

集成默认每 30 分钟拉取固件清单。0.9.1 起依次使用新 GitHub、新 Gitee、旧 Gitee 和旧 GitHub；
GitHub 是主入口，Gitee 是备份。曾手动填写过项目内置旧地址的安装也会自动使用完整回退列表；
其他自定义 URL 不变。

清单里有 `version`、`size`、`crc32`、`sha256`、`file`、`notes`。发现新版本时
`update.*` 自动变成「有更新」。安装时如果某一镜像的 BIN 下载失败或校验不符，
集成会使用**同一份清单的版本、大小、CRC32 和 SHA-256**尝试下一镜像，不会改刷
另一份未核验固件。**不需要本地放固件文件**。想立刻检查：按
`button.*_check_for_updates`。

## 升级

### 推荐：固件 1.6.0+ 直接点安装

1. 在 **设置 → 系统 → 更新**、设备页面或仪表板卡片点「安装」；
2. 集成下载并校验固件，自动等待下一分钟普通窗口，连接并请求进入 OTA；
3. 设备复位后出现 OTA 广播，集成先写任何待写阈值，再上传、CRC 校验并发送 END；
4. 收到目标版本的新 BTHome 广播才显示成功。

等待窗口时进度会显示 `waiting_window / connecting_control / requesting_ota /
waiting_ota_ad`。错过短窗会等后续广播重试，整个唤醒等待上限 240 秒；点「取消升级」
会取消等待或上传。首版不恢复 HA 重启前尚未完成的安装任务。

### 旧固件首次升级 / B 键备用

1. 固件 1.5.0 及更早版本没有普通 GATT 入口，先长按 B **3 秒**或等待三天定时窗口；
2. 默认只更新 OTA 模式状态；需要升级时点击「安装」，也可在集成配置中显式开启自动升级；
3. 之后的日常升级即可直接点安装，不再按 B。

如果已是最新版本，进入 OTA 模式不会做任何升级，设备约 5 分钟后自动退出。
自动升级开关位于 **设置 → 设备与服务 → GasPulse 蓝牙燃气表 → 配置 → 设备进入 OTA 模式时自动
升级**，默认关闭。

> HA 重启后如果设备还停留在此前的 OTA 模式里，集成不会仅凭遗留广播自动升级；
> 必须先在本次启动中看到过普通 BTHome 广播。即使已开启自动升级，HA 未提供可靠的
> 当前原始 BLE 广播时也不会自动刷写；用户明确点击安装的手动路径不受影响。

### 手动服务

临时指定固件/分片时用服务（普通模式免按键同样要求完整 size/CRC32/SHA-256 sidecar）：

```yaml
action: ch592_ota.flash
data:
  firmware: www/firmware/ch592_ota.bin
  version: "1.4.1"
  chunk: 128      # 16 的倍数，16~240；默认按设备能力自动选
```

取消升级：`button.*_abort_ota` 或 `ch592_ota.abort`。

## 燃气真实读数、校准与统计（0.7.0）

集成把设备 `0x4C` 当作原始累计计数，提供三个传感器：

| 实体 | 用途 |
|------|------|
| `sensor.*_raw_gas_counter` | 设备原始计数，只用于诊断和增量计算 |
| `sensor.*_gas_total` | 燃气表真实读数，用于仪表板显示 |
| `sensor.*_gas_consumption_total` | 统计累计值，用于 Derivative、Utility Meter 和自动化 |

### 校准

1. 等收到一次普通 BTHome 广播，在 `number.*_gas_target`（显示为
   **机械表实际读数（输入后自动校准）**）输入机械燃气表的完整真实读数；
2. 按回车或点击输入框外提交数值即执行校准，不需要单独的写入按钮。集成计算并持久化
   `offset = 真实表读数 - 当前 raw`，随后 `gas_total` 按有效原始增量增加。
   **不需要按 B、不需要进入 OTA，也不会改写设备 DataFlash 或原始计数。**

0.7.6 起删除旧 `button.*_write_gas_value` 及其注册表残留；`ch592_ota.set_gas`
服务仍保留为校准接口。

`offset` 可以为负数。`ch592_ota.set_gas` 保留为兼容别名，但现在也只执行 HA
校准，不再写设备，也不再产生 OTA 待写任务。

### 统计与防跳变

- 显示当前机械表读数使用 `gas_total`；
- Derivative 和 Utility Meter 的源使用 `gas_consumption_total`；
- 校准只改变真实读数，不改变统计消耗，因此不会人为制造一次用气；
- 旧广播重放、异常回退或设备计数归零时保持两个输出值，并暴露
  `raw_reset_detected` 属性；重新校准后才允许真实读数向下变化；
- 原始计数按 uint32 自然回绕，模数为 2^32。

## 采样阈值校正（0.6.0 + 固件 1.4.1）

不同表型、玻璃罩脏污、安装距离都会影响红外采样判据，现在阈值可以远程调整，
不用改固件。固件 1.4.9 起普通 BTHome 广播增加 `0x3E count32` 当前阈值；P/N 每调整
一次以及松手保存后都会在约 300 ms 内补发广播，HA 的 **当前采样阈值（P/N按键调整）**
直接显示当前值，平时仍保持 60 秒广播。原生 BTHome 也可能显示一个通用 Count 实体。
固件 1.5.0 起，P/N 按下立即 ±1，前 3 秒每 300 ms ±1，3 秒后 ±10，9 秒后 ±50；
松手不重复步进。N 到下限 20、P 到上限 2000 后停止变化。

1. `number.*_pulse_threshold` 填目标值（20~2000，默认 100），按回车或点击输入框外提交；
2. 不再需要单独的 HA 写入按钮：
   - 设备已在 OTA 模式 → 立即写入（写 DataFlash，**不复位**）；
   - 不在 OTA 模式 → 持久保存为「等待 OTA 写入」，可按 B 立即触发，也可等待
     每 3 天的定时 OTA 窗口；HA 重启不会清除待写值；
   - 阈值与固件升级同时存在时，**先写阈值，成功后继续同一窗口的固件升级**；
     写入失败保留待写值和错误，本次窗口不继续固件升级；
3. 取消：`button.*_cancel_pending_writes` 或 `ch592_ota.cancel_threshold`。

0.7.6 起删除旧 `button.*_write_pulse_threshold`；0.7.7 起待写值不再 15 分钟过期。
`ch592_ota.set_threshold` 服务保留。

```yaml
action: ch592_ota.set_threshold
data:
  value: 120     # 20~2000
```

设备端把阈值存在 DataFlash **块 3（0x3000）**，写入后读回校验，非法值回 `0xFE`，
校验失败回 `0xFF`，旧值不受影响。阈值写完后设备**不复位**，可以继续 OTA 操作；
燃气真实读数只在 HA 校准，不再通过 OTA 写设备。

调参方向：阈值越低越灵敏，可能把噪声计成脉冲（读数虚增）；越高越保守，可能漏计数。
实测余量 190~460，默认 100 已经留足余量，一般只在换表型或环境变化时微调 80~300。
`sensor.*_ota_status` 的 `device_threshold` 属性显示设备当前生效值。

## 从旧燃气模板迁移

先校准并核对 `gas_total`，再把 Derivative、Utility Meter 和自定义统计的源改为
`gas_consumption_total`。不要重复添加 `offset + raw` 模板，也不要同时保留两套
防回退计算。现有 Derivative / Utility Meter helper 可以保留以延续其历史。

观察几次脉冲并确认无负速率、重复 offset 或校准跳变后，再停用旧
`sensor.gas_total_guarded` 模板。若切换源时周期值出现跳变，先记录当前周期值，
必要时用 `utility_meter.calibrate` 修正，不要直接删除历史。

## 实体与观测属性

| 实体 | 说明 |
|------|------|
| `update.<device>_firmware` | 固件更新（安装 + 进度） |
| `binary_sensor.<device>_ota_mode` | 是否处于 OTA 模式 |
| `sensor.<device>_ota_status` | `idle` / `running` / `rebooting` / `success` / `failed` / `aborted` |
| `sensor.<device>_ota_progress` | 上传进度 0–100 % |
| `sensor.<device>_firmware_version` | 设备通过 BTHome `0x53` 报告的固件版本 |
| `button.<device>_abort_ota` | 取消升级 |
| `button.<device>_check_for_updates` | 立即检查固件更新 |
| `number.<device>_gas_target` | 机械表实际读数（提交即校准，只写 HA offset，无需按 B） |
| `sensor.<device>_current_threshold` | 当前采样阈值（P/N按键调整），固件 ≥1.4.9 实时广播 |
| `number.<device>_pulse_threshold` | 采样阈值（提交后写入或持久等待下次 OTA；按 B 或最长等 3 天） |
| `button.<device>_cancel_pending_writes` | 取消待写入阈值 |

`sensor.<device>_ota_status` 的属性是排查问题的主要入口：

| 属性 | 含义 |
|------|------|
| `phase` | 细分阶段：`connected mtu=… chunk=…` / `erasing` / `uploading` / `verifying` / `waiting_reboot` / `done` |
| `error` | 失败原因 |
| `chunk` / `verify_chunk` / `mtu` | 本次实际使用的分片与 MTU |
| `vdd_mv` / `vdd_min_mv` | 设备在 OTA 模式测到的电压（上传中每 16 KB 采一次），以及整个过程的最低值 |
| `duration_s` | 本次升级总耗时（秒） |
| `device_protocol` | 设备 OTA 协议版本（1.3.x = 4、1.4.0 = 5、1.4.1～1.4.6 = 6、1.4.7 = 7、1.4.8～1.6.0 = 8） |
| `device_features` | 设备 INFO 实际上报的能力位，例如 `0x0D`；用于核对快速 CRC 分支 |
| `device_threshold` | 设备当前采样阈值（≥1.4.1，连接时从 `INFO` 读取） |
| `threshold_pending` / `threshold_result` | 待写入的阈值与最近一次结果 |

`binary_sensor.*_ota_mode` 的属性：

| 属性 | 含义 |
|------|------|
| `auto_note` | 自动升级的结论：「开始自动升级到 x.y.z」或没启动的原因 |
| `ads_total` / `last_name` / `last_service_uuids` / `last_service_data_uuids` | 最近一条广播的样子 |

仪表板示例见 [`dashboards/ch592_ota.yaml`](dashboards/ch592_ota.yaml)。

## 注意事项

1. **设备的 OTA 时间窗**：进入 OTA 模式后，**无连接约 5 分钟**、**总时长约 15 分钟**
   会自动复位退出。失败后重试必须**重新长按 B**。
2. **升级期间没有 BTHome 数据**：OTA 模式下设备只广播 OTA 服务，燃气/电量停在
   最后一次的值，这是正常的。
3. **不要调大分片**：128 是 ESPHome 代理的安全上限；240 会退化成代理长写并在
   中途断链。校验阶段必须保持 64。
4. **电池**：新 CR2032 在 40 秒 OTA 负载下约 3.04 V，可用；旧电池（低于 ~2.9 V
   静置）建议先换。`vdd_min_mv` 是最直接的判断依据（< 2.6 V 基本到极限）。
5. **USB 烧录只擦代码区**：`firmware/Broadcaster_full.hex` 不含 DataFlash
   （`0x70000`），用 WCH-LinkUtility 时不要选 Chip Erase / 全片擦除，否则燃气值丢失。
6. **广播判定以当前原始包为准**：HA 会把该地址的设备名缓存成 `GasMeter OTA`，
   普通 BTHome 广播的合并字段里也可能残留 `0xFFE0/0xFEE0` 或旧 `0xFCD2` 数据。
   0.7.1 起优先解析原始 BLE AD 结构：当前包有 `0xFEE0` 且没有 `0xFCD2` 才是 OTA；
   只有旧版 HA 不提供原始包时才使用合并字段兼容判断。

## 故障排查

| 现象 / 报错 | 含义与处理 |
|-------------|-----------|
| `Failed to connect after 3 attempt(s)` | 设备没进 OTA 模式或已退出。**重新长按 B 3 秒**再试 |
| `校验失败 @0xXXXXX: 设备返回错误状态 0x??` | 写入内容与镜像不一致；先看是否误调大了 VERIFY 分片 |
| 旧版在 CRC32 后报 `is not connected` | 旧固件逐位 CRC 阻塞拖断代理；集成 0.7.2 对协议 ≤7 自动用 VERIFY，固件 1.4.8 起恢复快速 CRC |
| 进度到 12% 又回到 1% | 分片写入失败触发了整体重传（0.4.2 起上限 128，正常不会再出现） |
| 卡在 98% 很久 | 等设备重启后的版本广播。0.4.7 起只要版本已对上就立即判成功；再出现请检查 `firmware_version` 是否在更新 |
| `name 'OtaError' is not defined` | 0.4.4 的 bug，升级到 0.4.6+ |
| `固件已上传，但 180s 内未看到目标版本` | 版本号没刷新：确认 `fw` 是否已变成目标版本（0.4.7 已修判定逻辑） |
| 升级中途断电 | `END` 之前：仍跑旧固件；`END` 之后：下次上电 IAP 继续拷贝 |

## 开发与验证流程

**改 HA 侧代码**（`ble_ota.py` / `coordinator.py` 等）

1. 复制 `custom_components/ch592_ota/` 到 `/config/custom_components/`
2. 调用 `ch592_ota.reload_code`
3. 继续验证

**改固件**

```bash
# 1. 改版本号：APP/include/broadcaster.h 的 FW_VERSION_STRING
# 2. 编译（MounRiver Studio 或命令行 make）
python tools/make_ota_images.py
python tools/publish_firmware.py --repo . --notes "改了什么" --push
# 3. 打 tag + release（GitHub Actions 会为缺失的 v* tag 自动建 release）
```

发布后 HA 里按 `button.*_check_for_updates` 即可看到新版本。

## 版本要点

| 版本 | 关键改动 |
|------|---------|
| 0.1.x | 首个可用版本；分片 240 → 64；原生 update 实体 |
| 0.2.x | 固件清单自动检查、立即检查按钮 |
| 0.3.x | 长按 B 自动升级；OTA 广播判定修正；进度卡 98% 修正 |
| 0.4.2 | PROGRAM 分片上限 128（代理长写退化） |
| 0.4.4 | 断链自动整体重传 |
| 0.4.5 | 上传过程中持续采样电压 + `vdd_min_mv` |
| 0.4.7 | 用 `0xFCD2` 判定广播类型，修复升级成功却报失败 |
| 0.4.8 | `reload_code` 热重载服务 |
| 0.4.9 | 未配置固件路径时本地文件缺失不再告警 |
| 0.5.0 | 燃气值校正：number 草稿 + 写入按钮 + 待写入优先级 |
| 0.6.0 | 采样阈值校正（0x87）：number 草稿 + 写入按钮 + 设备阈值显示 |
| 0.6.1 | 设备页实体重新分组：数值与写入按钮统一放进「配置」 |
| 0.6.2 | 集成图标与标志（`brand/`），HA / HACS 显示燃气表盘图标 |
| 0.6.3 | 曾加入 `Gas total (guarded)` 实体（0.6.4 已移除，改由模板传感器承担） |
| 0.6.4 | 移除 guarded 实体与相关服务/按钮，集成回到纯 OTA/写值职责 |
| 0.7.1 | 原始 BLE 包识别恢复按 B 自动升级；临时全局 VERIFY 避免 CRC32 阶段断链 |
| 0.7.2 | 协议门控恢复单次 CRC32；仅旧固件使用 VERIFY，断链后不做无效回退 |
| 0.7.3 | 中文“燃气表实际读数”保存即校准；旧按钮隐藏但保留自动化兼容 |
| 0.7.4 | P/N 当前阈值约 300 ms 回显；设备页实体统一中文，ID 与接口不变 |
| 0.7.5 | 协议 8 不再被冗余能力位挡在慢速 VERIFY；阈值只读实体修复为 diagnostic |
| 0.7.6 | 检查更新移回控制区；燃气/阈值旧写入按钮删除；阈值输入直接写入或登记待写入 |
| 0.7.7 | 待写阈值跨重启持久化，可等 3 天定时 OTA；阈值成功后同窗口继续固件升级 |
| 0.8.0 | 配合固件 1.6.0 普通 300 ms / 50 ms 窗口免按键 OTA；完整校验、跨分钟重试和 GATT 重新发现 |
| 0.8.1 | 真实 connect 使用 65 秒并在窗口内重试；应用 240 秒总期限，显示唤醒次数和真实错误 |
| 0.8.2 | 清理普通/OTA 模式间 ESPHome 旧 GATT 句柄缓存；严格断连重连并补齐期限、取消与连接所有权 |
| 0.8.3 | 首次连接前预清所有可见 ESPHome 代理缓存；恢复单次 65 秒期限并补齐本地蓝牙与失败回退 |
| 0.8.4 | 自动升级默认关闭；无可靠原始 OTA 广播时禁止自动刷写；修正文档与问题反馈入口 |

固件侧：1.2.9 引入按页拆分的 PROGRAM；1.3.0 引入 `0x85` CRC32 校验与 OTA 模式
电压上报；1.3.1 为电池实测版本；1.4.0 增加 `0x86` 燃气值写入；1.4.1 增加
`0x87` 采样阈值写入（DataFlash 块 3，默认 100，可设 20~2000）；1.4.2 把燃气值
改为追加式日志并逐条读回校验，掉电最多丢一条记录；1.4.3 修正保存失败后的
重试计时（按 300 ms 采样节拍，约 15 秒重试）。完整记录见
[CHANGELOG.md](CHANGELOG.md)。

## 开发与测试

- 常规单测无需 HA 环境：仓库根目录 `python3 -m unittest discover -s tests`，
  只用标准库和手工 stub，`tools/project_verify.py all` 的默认门禁同样零外部依赖。
- 真实 HTTP 故障回退（真 socket、真 aiohttp、loopback 服务器，覆盖清单 404/坏
  JSON/缺字段、BIN 404/大小不符/CRC+SHA 不符/全候选损坏、畸形 size 拦截和
  coordinator→下载全链路）在工程根目录运行：
  `python3 tools/run_http_fault_injection.py`。首次运行自动创建 `.venv-http/`
  并安装钉版 aiohttp；该模块刻意不命名为 `test_*.py`，系统 Python 缺 aiohttp
  时自动 skip，不影响默认门禁。加 `--online` 会额外对四个线上固件镜像做只读
  校验（发布新固件版本后需同步更新脚本内钉死的元数据）。
