# CH592 OTA

Home Assistant 自定义集成：通过 Home Assistant 的蓝牙代理（ESPHome Bluetooth
Proxy）在线升级 **CH592 BLE 燃气表广播器**（WCH BackupUpgrade / OTA）。

设备端用 WCH 备份升级方案：OTA 镜像写入 Image B，校验通过后 IAP 在重启时拷贝到
Image A；任何一步失败都会继续跑旧固件。版本号通过 BTHome v2 文本对象 `0x53`
广播，升级完成后 HA 里能直接看到新版本。

- 集成仓库：https://gitee.com/zigbridge/ch592_ota ｜ https://github.com/TeaRain/ch592_ota
- 固件仓库：https://gitee.com/zigbridge/ch592-gas-broadcaster ｜ https://github.com/TeaRain/ch592-gas-broadcaster

## 当前状态（2026-11-15 实测）

| 项目 | 值 |
|------|-----|
| 集成版本 | **0.4.8** |
| 固件版本 | **1.3.1** |
| 一次升级耗时 | **约 35~40 秒**（128 字节分片 + CRC32 一次校验） |
| 供电实测 | CR2032 新电池，OTA 负载下 **3.04 V**，全程无下探 |
| 结论 | **CR2032 可以完成 OTA**；之前跑不下来是耗时太长（约 20 分钟） |

设备：MAC `0C:3D:5E:54:BC:6F`，HA 里是 BTHome 设备 “BTHome sensor BC6F”。

## 它是怎么工作的

**设备端**（`peripheral.c` 的 OTA 分支）

- 长按 B 键 3 秒 → 写 DataFlash 标记 → 复位 → 启动可连接的 OTA 外设，
  广播服务 `0xFEE0`（特征 `0xFEE1`），不再发 BTHome 广播
- 命令：`0x80` PROGRAM、`0x81` ERASE、`0x82` VERIFY、`0x83` END、`0x84` INFO、
  `0x85` CRC32（1.3.0 起）
- `END` 后切换 ImageFlag 并复位，IAP 把 Image B 拷到 Image A

**集成端**

- 用 `write without response` 流式写 PROGRAM，每 32 片读一次状态
  （以前是每片都「写 + 读」，这是原来 20 分钟的主要来源）
- 校验优先用设备端 `0x85` CRC32（一次往返）；固件不支持时退回逐字节 VERIFY
- 识别到 OTA 广播后（默认开启）自动下载并开始升级，不需要在 HA 里点按钮

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
2. 添加 `https://github.com/TeaRain/ch592_ota`，类型选 **集成（Integration）**；
3. 搜索 “CH592 OTA” 安装，然后重启 HA。

Gitee 主仓库需要手动安装（见方式二/三）。

### 方式二：git clone

```bash
cd /config
rm -rf /tmp/ch592_ota_repo
git clone --depth 1 https://gitee.com/zigbridge/ch592_ota.git /tmp/ch592_ota_repo
cp -rf /tmp/ch592_ota_repo/custom_components/ch592_ota /config/custom_components/
rm -rf /tmp/ch592_ota_repo
```

### 方式三：下载 ZIP

- Gitee：`https://gitee.com/zigbridge/ch592_ota/repository/archive/master.zip`
- GitHub：`https://github.com/TeaRain/ch592_ota/archive/refs/heads/master.zip`

解压后把 `custom_components/ch592_ota` 复制到 `/config/custom_components/`。

装好之后：**设置 → 设备与服务 → 添加集成 → 搜索 “CH592 OTA”**。

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

集成默认每 30 分钟拉取固件清单（Gitee → GitHub 兜底）：

```
https://gitee.com/zigbridge/ch592-gas-broadcaster/raw/master/firmware/manifest.json
```

清单里有 `version`、`size`、`crc32`、`sha256`、`file`、`notes`。发现新版本时
`update.*` 自动变成「有更新」，安装时下载镜像并校验大小 / CRC32 / SHA-256，
**不需要本地放固件文件**。想立刻检查：按 `button.*_check_for_updates`。

## 升级

### 推荐：长按 B 就知道升（默认开启）

1. 长按设备 B 键 **3 秒** → 设备复位并广播 `GasMeter OTA`（服务 `0xFEE0`）；
2. 集成看到 OTA 广播 → 如果清单里有新版本，**自动下载并开始升级**；
3. 约 40 秒结束，设备重启回到普通广播，版本号变成新版本。

如果已是最新版本，进入 OTA 模式不会做任何升级，设备 8 分钟后自动退出。

想改回手动：**设置 → 设备与服务 → CH592 OTA → 配置 → 设备进入 OTA 模式时自动
升级** 取消勾选。

> HA 重启后如果设备还停留在此前的 OTA 模式里，集成**不会**自动升级——必须先在
> 本次启动中看到过一条普通 BTHome 广播，才会把 OTA 广播当成「刚按了 B」。

### 手动

在 **设置 → 系统 → 更新**、设备页面或仪表板卡片里点「安装」。

临时指定固件/分片时用服务：

```yaml
action: ch592_ota.flash
data:
  firmware: www/firmware/ch592_ota.bin
  version: "1.3.1"
  chunk: 128      # 16 的倍数，16~240；默认按设备能力自动选
```

取消升级：`button.*_abort_ota` 或 `ch592_ota.abort`。

## 燃气值校正（0.5.0 + 固件 1.4.0）

不用再长按 +/- 刷数值，可以在 HA 里直接把读数写进去。

1. 在 `number.*_gas_target` 里填目标值（单位 m³，0.001 分辨率）——
   **它只是草稿，填了不会写设备**；
2. 点 `button.*_write_gas_value`：
   - 设备已在 OTA 模式 → 立刻写入并复位；
   - 设备不在 OTA 模式 → 记为「待写入」（15 分钟有效），此时长按 B 3 秒，
     集成看到 OTA 广播后会**先写燃气值、不升级**；
3. 想放弃待写入：点 `button.*_cancel_pending_gas_write` 或调
   `ch592_ota.cancel_gas`。

服务方式：

```yaml
action: ch592_ota.set_gas
data:
  value: 3050.79     # m³
```

设备端行为：新记录写进**较旧的那个 DataFlash 槽**（`seq + 1`），读回校验通过才回
成功，然后约 0.2 秒后复位让广播器加载新值。写入失败时旧值不受影响。

> **优先级规则**：有待写入的燃气值时，按 B 只写燃气值、不自动升级；
> `update` 实体仍会显示有新版本，之后再按一次 B 即可升级。
> 升级进行中点写入会被拒绝（避免两个任务抢同一个连接）。

## 实体与观测属性

| 实体 | 说明 |
|------|------|
| `update.<device>_firmware` | 固件更新（安装 + 进度） |
| `binary_sensor.<device>_ota_mode` | 是否处于 OTA 模式 |
| `sensor.<device>_ota_status` | `idle` / `running` / `rebooting` / `success` / `failed` / `aborted` |
| `sensor.<device>_ota_progress` | 上传进度 0–100 % |
| `sensor.<device>_firmware_version` | 设备通过 BTHome `0x53` 报告的固件版本 |
| `button.<device>_abort_ota` | 取消升级 |
| `button.<device>_check_for_updates` | 立即检查固件清单 |
| `number.<device>_gas_target` | 要写入的燃气值（草稿，不写设备） |
| `button.<device>_write_gas_value` | 写入燃气值 / 记为待写入 |
| `button.<device>_cancel_pending_gas_write` | 取消待写入 |

`sensor.<device>_ota_status` 的属性是排查问题的主要入口：

| 属性 | 含义 |
|------|------|
| `phase` | 细分阶段：`connected mtu=… chunk=…` / `erasing` / `uploading` / `verifying` / `waiting_reboot` / `done` |
| `error` | 失败原因 |
| `chunk` / `verify_chunk` / `mtu` | 本次实际使用的分片与 MTU |
| `vdd_mv` / `vdd_min_mv` | 设备在 OTA 模式测到的电压（上传中每 16 KB 采一次），以及整个过程的最低值 |
| `duration_s` | 本次升级总耗时（秒） |
| `device_protocol` | 设备 OTA 协议版本（1.3.x = 4） |

`binary_sensor.*_ota_mode` 的属性：

| 属性 | 含义 |
|------|------|
| `auto_note` | 自动升级的结论：「开始自动升级到 x.y.z」或没启动的原因 |
| `ads_total` / `last_name` / `last_service_uuids` / `last_service_data_uuids` | 最近一条广播的样子 |

仪表板示例见 [`dashboards/ch592_ota.yaml`](dashboards/ch592_ota.yaml)。

## 注意事项

1. **设备的 OTA 时间窗**：进入 OTA 模式后，**无连接 8 分钟**、**总时长 24 分钟**
   会自动复位退出。失败后重试必须**重新长按 B**。
2. **升级期间没有 BTHome 数据**：OTA 模式下设备只广播 OTA 服务，燃气/电量停在
   最后一次的值，这是正常的。
3. **不要调大分片**：128 是 ESPHome 代理的安全上限；240 会退化成代理长写并在
   中途断链。校验阶段必须保持 64。
4. **电池**：新 CR2032 在 40 秒 OTA 负载下约 3.04 V，可用；旧电池（低于 ~2.9 V
   静置）建议先换。`vdd_min_mv` 是最直接的判断依据（< 2.6 V 基本到极限）。
5. **USB 烧录只擦代码区**：`firmware/Broadcaster_full.hex` 不含 DataFlash
   （`0x70000`），用 WCH-LinkUtility 时不要选 Chip Erase / 全片擦除，否则燃气值丢失。
6. **广播判定不用设备名**：HA 会把该地址的设备名缓存成 `GasMeter OTA`；普通
   BTHome 广播的 `service_uuids` 里也可能带着 `0xFFE0/0xFEE0`（代理做过 GATT
   发现后的缓存）。集成用 **`0xFCD2` BTHome 服务数据**作为判据：
   有 fee0 且没有 fcd2 = OTA 广播。

## 故障排查

| 现象 / 报错 | 含义与处理 |
|-------------|-----------|
| `Failed to connect after 3 attempt(s)` | 设备没进 OTA 模式或已退出。**重新长按 B 3 秒**再试 |
| `校验失败 @0xXXXXX: 设备返回错误状态 0x??` | 写入内容与镜像不一致；先看是否误调大了 VERIFY 分片 |
| `CRC32 校验未通过，退回逐字节校验` | 设备 CRC 与本地不一致，会自动改用逐字节校验并报出第一个不一致的偏移 |
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

固件侧：1.2.9 引入按页拆分的 PROGRAM；1.3.0 引入 `0x85` CRC32 校验与 OTA 模式
电压上报；1.3.1 为电池实测版本；1.4.0 增加 `0x86` 燃气值写入。完整记录见
[CHANGELOG.md](CHANGELOG.md)。
