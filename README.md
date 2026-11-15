# CH592 OTA

Home Assistant 自定义集成：通过 Home Assistant 的蓝牙代理（ESPHome Bluetooth
Proxy）在线升级 **CH592 BLE 燃气表广播器**（WCH BackupUpgrade / OTA）。

设备端使用 WCH 备份升级方案：OTA 镜像写入 Image B，校验通过后 IAP 在重启时
拷贝到 Image A，任何一步失败都会继续运行旧固件。版本号通过 BTHome v2 文本对象
`0x53` 广播，升级成功后可以在 HA 里直接确认。

## 功能

- HA 原生 **固件更新实体**（`update.*`）：显示 `当前版本 → 新版本`，一键安装、
  显示进度，升级后自动核对版本号
- OTA 实体并入 BTHome 设备页：燃气、电量、信号、版本、OTA 状态在同一页
- `binary_sensor` 指示设备是否已进入 OTA 模式
- 分片自适应（默认 64 字节，失败自动降 32 / 16），适配 ESPHome 代理
- 固件文件校验：首字 JAL 检查 + CRC32（来自 `.bin.json` 边车文件）

## 依赖

- Home Assistant `bluetooth` 集成
- ESPHome 蓝牙代理，需要可连接模式：

```yaml
esp32_ble_tracker:
  scan_parameters:
    active: true
bluetooth_proxy:
  active: true
```

## 安装

把 `custom_components/ch592_ota` 整个目录放到 Home Assistant 的
`/config/custom_components/ch592_ota`，重启 HA，然后：

**设置 → 设备与服务 → 添加集成 → 搜索 “CH592 OTA”**

### 方式一：HACS / 市场自定义仓库（推荐）

HACS 只识别 GitHub 仓库，所以添加 GitHub 镜像：

1. HACS / 市场 → 右上角 ⋮ → **自定义仓库**；
2. 添加 `https://github.com/TeaRain/ch592_ota`，类型选 **集成（Integration）**；
3. 搜索 “CH592 OTA” 安装，然后重启 HA。

HACS 会按默认分支或版本选择来更新集成。

### 方式二：git clone（Gitee 主仓库）

在 HA 的 Terminal / SSH 里：

```bash
cd /config
git clone https://gitee.com/zigbridge/ch592_ota.git /tmp/ch592_ota_repo
cp -r /tmp/ch592_ota_repo/custom_components/ch592_ota /config/custom_components/
rm -rf /tmp/ch592_ota_repo
```

GitHub：`https://github.com/TeaRain/ch592_ota.git`。

### 方式三：下载 ZIP

- Gitee：`https://gitee.com/zigbridge/ch592_ota/repository/archive/master.zip`
- GitHub：`https://github.com/TeaRain/ch592_ota/archive/refs/heads/master.zip`

解压后把 `custom_components/ch592_ota` 复制到 `/config/custom_components/`。

> HACS 的自定义仓库只支持 GitHub 上的公共仓库；Gitee 仓库需要手动安装/更新。

## 配置

| 字段 | 示例 | 说明 |
|------|------|------|
| 蓝牙地址 | `0C:3D:5E:54:BC:6F` | BTHome 设备的 MAC，大写 |
| 设备名称 | `CH592` | 备用名称；OTA 实体会并入 BTHome 设备页 |
| 固件路径 | `www/firmware/ch592_ota.bin` | 相对 `/config`，必须在 config 目录内 |

固件目录：

```
/config/www/firmware/ch592_ota.bin
/config/www/firmware/ch592_ota.bin.json
```

`.bin.json` 由 `tools/make_ota_images.py`（见固件工程仓库/发行包）生成，包含
`version`、`size`、`crc32`；集成用它决定最新版本和校验固件。

### 固件自动检查（0.2.0）

集成默认每 30 分钟拉取一次固件清单：

```
https://gitee.com/zigbridge/ch592-gas-broadcaster/raw/master/firmware/manifest.json
```

清单里有 `version`、`size`、`crc32`、`sha256`、`file`、`notes`。集成把它和设备
通过 BTHome `0x53` 上报的版本比较，发现新版本时 `update.*` 实体自动变成
“有更新”（`on`），点“安装”后集成直接下载镜像、校验大小/CRC32/SHA-256，再走 OTA，
**不需要本地放固件文件**。

- 修改清单地址：**设置 → 设备与服务 → CH592 OTA → 配置**，可换成 GitHub raw
  或其他地址；留空则关闭自动检查。
- 清单拉取失败时（例如仓库是私有），更新实体仍可用，`manifest_error` 属性会给出
  原因，此时回退到本地 `.bin`。
- 固件仓库：https://gitee.com/zigbridge/ch592-gas-broadcaster

## 升级

1. 长按设备 B 键 **3 秒**，设备复位并广播 `GasMeter OTA`（服务 `0xFEE0`），
   此时 `binary_sensor.*_ota_mode` 变 `on`。
2. 在 **设置 → 系统 → 更新**、设备页面，或仪表板卡片里点“安装”。
3. 升级完成后设备自动重启，版本号变为新固件版本。

也可以调用服务（需要临时换固件/版本/分片时）：

```yaml
action: ch592_ota.flash
data:
  firmware: www/firmware/ch592_ota.bin
  version: "1.2.5"
  chunk: 64      # 可选，16 的倍数，16~64，默认 64
```

取消升级：`button.*_abort_ota` 或 `ch592_ota.abort`。

## 实体

| 实体 | 说明 |
|------|------|
| `update.<device>_firmware` | 固件更新（安装 + 进度） |
| `binary_sensor.<device>_ota_mode` | 是否处于 OTA 模式 |
| `sensor.<device>_ota_status` | `idle` / `running` / `rebooting` / `success` / `failed` / `aborted` |
| `sensor.<device>_ota_progress` | 上传进度 0–100 % |
| `sensor.<device>_firmware_version` | BTHome `0x53` 报告的固件版本 |
| `button.<device>_abort_ota` | 取消升级 |

`<device>` 跟随设备名，实体 ID 以设备页面实际生成结果为准。

仪表板示例见 [`dashboards/ch592_ota.yaml`](dashboards/ch592_ota.yaml)。

## 在线更新集成

- 用 HACS / 市场安装的：直接在 HACS 里点“更新”，然后重启 HA。
- 用 git clone 安装的，在 HA 的 Terminal / SSH 里：

```bash
cd /config
rm -rf /tmp/ch592_ota_repo
git clone --depth 1 https://gitee.com/zigbridge/ch592_ota.git /tmp/ch592_ota_repo
cp -rf /tmp/ch592_ota_repo/custom_components/ch592_ota /config/custom_components/
rm -rf /tmp/ch592_ota_repo
```

然后在“设置 → 系统 → 重启”里重启 Home Assistant（或重启“CH592 OTA”集成）。

## 常见问题

**OTA 停在 10%，报 `Insufficient authorization (8)`**

`8` 实际是 ESPHome 代理的 `ESP_GATT_CONN_TIMEOUT`（链路超时），不是权限错误。
240 字节的大分片会让 CH592 在第一条 PROGRAM 写入后复位。0.1.1 起默认分片改为
64 字节，失败自动降 32 / 16。设备复位后会回到旧固件继续广播，重新长按 B 即可再试。

**找不到可连接设备**

- 确认已长按 B 3 秒，设备名应为 `GasMeter OTA`；
- 普通广播模式下的设备不可连接；
- 确认蓝牙代理在线且距离足够近。

**升级后版本没变**

- `.bin.json` 里的 `version` 必须与新固件一致；
- 版本号没改的镜像，即使升级成功界面上也看不出变化。

**升级中途断电**

- `END` 之前断电：设备仍运行旧固件；
- `END` 之后断电：IAP 下次上电继续把 Image B 拷贝到 Image A。

## 版本

见 [CHANGELOG.md](CHANGELOG.md)。
