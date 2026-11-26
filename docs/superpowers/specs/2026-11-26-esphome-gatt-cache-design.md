# ESPHome 普通/OTA GATT 缓存切换设计

## 实机问题与根因

HA 0.8.1 的普通窗口升级已能连接设备，但两次写 `0x88 ENTER_OTA` 均返回 `Bluetooth GATT Error handle=52 error=1 Invalid handle`，上传为 0，设备保持 1.6.1。普通固件只注册 GAP、GATT 和 OTAProfile，`0xFEE1` 句柄为 16；OTA 固件额外注册 DeviceInfo 和 SimpleProfile，句柄为 52。ESPHome 缓存了另一模式的服务树，UUID 查找虽成功，数值句柄却已经失效。

HA 2026.11.3 使用 bleak 3.0.2、bleak-retry-connector 4.7.1、bleak-esphome 4.0.0、habluetooth 7.1.2。ESPHome 的 `_get_services()` 在 REMOTE_CACHING 下即使 `dangerous_use_bleak_cache=False` 也会命中已有 host cache。HaBleakClientWrapper 又在 `connect()` 前把 `_backend` 保持为 None，因此连接前读取 backend 并清缓存不会作用于真实代理。

## 发布方案

发布 HA 0.8.2。普通唤醒与 fresh 上传/配置先建立连接以取得 HA 选择的真实 backend，通过 wrapper 公共 `clear_cache()` 清该代理的 host/remote cache，严格断开后再连接；第二次使用 `dangerous_use_bleak_cache=False` 或 `use_services_cache=False`，让 CONNECT_V3_WITHOUT_CACHE 返回当前模式的真实句柄。按共享 cache 对象身份记录本次已清理的代理，再次选中同一代理时直接使用新连接，选中另一代理时先清其缓存。

清缓存必须有明确期限。中间断连不能沿用最终 best-effort 清理：只有确认 `not client.is_connected` 才允许丢失旧引用并重连。每个已建立 client 立即通过回调登记给外层，取消或异常时外层 finally 仍能清理。

不采用硬编码句柄写入，不修改固件 GATT 表，不通过延长普通广播窗口增加常态功耗。普通 65 秒、协调器 240 秒、上传每次 establish 30 秒和非 fresh 缓存路径保持不变。

## 验证与边界

- 用延迟 backend 的 HA wrapper 替身复现 OTA 52 → 普通 16，以及普通 16 → OTA 52；旧实现失败，清缓存并重连后成功。
- 分别验证 clear 挂起受期限约束、中间 disconnect 错误阻止重连、disconnect 期间取消后外层仍持有 client 并可清理。
- 跑完整 HA 70 项与固件 14 项发布交叉检查，重建 23 文件 HACS ZIP 和 sidecar，核对双平台 tag、raw 文件和源归档。
- 独立审查提出的取消遗留连接和吞断连错误两项 Important 均以 RED→GREEN 修正，不再进行第二轮形式审查。
- 不触发设备 OTA，不修改燃气校准。0.8.2 发布不等于实机验收，用户升级集成并重启 HA 后仍需重试普通窗口。
