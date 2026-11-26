# ESPHome 预连接 GATT 缓存清理设计

## 实机证据与边界

HA 0.8.2 于 2026-11-26 02:51:47 UTC 开始普通窗口唤醒，02:55:47 失败：`wake_attempts=1`、`wake_error=""`、`duration_s=240.1`，上传为 0，设备保持 1.6.1。分钟广播和 RSSI 持续到达，错误文本不再是 0.8.1 的 `Invalid handle 52`。

协调器把 240 秒总剩余期限直接传给一次 `async_request_ota()`，使函数默认的 65 秒单次期限失效；裸 `TimeoutError` 的字符串为空，解释了最终空白错误。现有证据不能判定旧实现具体停在首次连接、清缓存还是第二次连接，不把该推断写成实测结论。0.8.2 必须在连接后清缓存、断开并再次追赶 300 ms 普通窗口，是需要去除的结构性脆弱点。

## 已验证的上游契约

- HA 2026.11.3 提供 `async_scanner_devices_by_address()`。
- bleak-esphome 4.0.0 的 `HaBluetoothConnector.client` 是 `partial(ESPHomeClient, client_data=...)`；其中 cache 与连接后 `ESPHomeClient._cache` 是同一对象。
- aioesphomeapi 46.25.0 的 `bluetooth_device_clear_cache(address, timeout=...)` 走代理 API，不要求先建立 BLE 连接。
- `CACHE_CLEARING` 不可用时仍清 host services/MTU cache；本地 BlueZ 和未识别 connector 不做 ESPHome 私有数据假设。
- `aioesphomeapi` 是 HA ESPHome 集成的依赖，不是本集成的声明依赖，因此只能在识别到 ESPHome 路径后延迟导入。

## 方案

普通唤醒和 fresh 上传/配置先枚举所有可见连接路径，同步清理每个已识别 ESPHome cache 的 services/MTU，再按 feature flag 调用代理 clear-cache RPC。返回已清理 cache 的身份集合；第一次以 `dangerous_use_bleak_cache=False` 或 `use_services_cache=False` 建立连接后，如果 backend cache 已预清，直接使用该连接，不再清缓存、断开和二次连接。

未识别路径保留 0.8.2 的连接后清缓存回退。回退也必须检查 `clear_cache()` 的返回值；明确 `False`、RPC 异常或超时都停止后续写入。普通每次唤醒 capped 65 秒，协调器 240 秒总期限不变；fresh 预清期限为 30 秒，上传 `establish_connection()` 的 30 秒、3 次参数不变。取消与 client 所有权语义保持。

不采用同连接私有 `_get_services()` 重读，因为设备存在约 5 秒空闲断连且该路径依赖更多私有行为；也不继续叠加二次连接重试。

## 验证与实机门槛

10 项新增回归均验证旧行为失败或经定向 mutation 失败后转绿，覆盖普通/OTA 双向句柄、多代理、失败与期限、无 ESPHome 导入、fresh 上传和 fresh 阈值配置。独立审查无 Critical，两项 Important（顶层依赖导入、回退忽略 `False`）全部修正。最终 HA 80/80、固件及发布交叉检查 14/14、Python 编译和 scoped diff check 通过。

这些结果不代替实机。发布后必须通过 HACS 安装 0.8.3、重启 HA、重试固件 1.6.2，并读取 `wake_attempts`、`wake_error`、版本和燃气计数；未触发设备 OTA、未修改校准。
