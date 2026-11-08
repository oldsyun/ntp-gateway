# Standalone NTP Gateway

独立运行的 NTP 对时网关服务，适用于 Buildroot / Linux 设备。
不依赖第三方 Python 包，使用 Python 3 标准库实现。

## 功能

- SNTP 客户端，多上级服务器择优，手动同步和同步状态监测。
- NTP 偏差自动补偿：达到 1 ms 时用 `adjtime()` 平滑校正，达到 50 ms 时直接步进。
- 时间有效性状态：`TIME_SYNCED`、`TIME_HOLDOVER`、`TIME_INVALID`。
- 按硬件自动识别串口数量，每个 RS485 端口独立线程、独立调度和状态。
- 每路支持按秒间隔（整秒对齐）或每天定点（精确到秒）两种调度策略。
- P3 Specific 全对时报文；分对时功能已移除。
- 全部定时发送按 epoch 整秒边界触发，报文毫秒字段固定为 0。
- Modbus CRC16 动态计算和响应校验。
- Web 管理、Bearer Token 认证、首次登录强制改密。
- Web 网络配置支持 eth0/eth1 的静态 IP、DHCP、网关与 DNS。
- 网络变更采用临时地址、180 秒确认和失败自动回滚，避免改 IP 后失联。
- 配置原子写入、滚动日志、服务自启动和旧程序回滚。

## Web 控制台

- 导航为 5 项：总览、NTP、网络、RS485、系统。
- 定时任务作为“调度策略”并入 NTP 页，与 NTP 配置一起保存。
- 运行日志并入系统页，支持级别筛选和关键字搜索。
- 网络、RS485、NTP 的保存按钮仅在配置发生变化后启用。
- 总览与网络页使用绿色圆点标识已激活网卡。

## 默认安全策略

串口默认全部禁用。启用前必须核对全对时报文、从站地址和现场接线。
可通过 Web 页面或 API 单独配置并手动测试。

## 当前实现边界

- 网络配置必须通过页面或 API 的 stage → confirm/rollback 流程提交；确认前不会落盘。
- 设备没有 `tzdata`，服务直接使用 `/etc/localtime` 的本地时间，当前为 `CST +0800`。
- 平滑补偿依赖 Linux `adjtime()`；不可用时自动降级为系统时间步进。
- 服务已接入 Buildroot 启动脚本；进程异常退出后的独立监督/看门狗尚未加入。
- P3 自动发送默认关闭，待厂家确认协议后再启用。

## 部署

在设备上执行：

```sh
cd /tmp/ntp-gateway-install
./install.sh
```

安装脚本会：

1. 停止 `goWDTARM` 和 `openGWEnterprise`。
2. 保留原程序及启动脚本，不删除任何现场文件和配置。
3. 安装 `/home/ntp-gateway`。
4. 安装 `/etc/init.d/S55ntp-gateway`。
5. 替换 `/etc/htset/autorun`，不再启动旧程序。
6. 启动独立服务并监听默认 `80` 端口。

首次登录账号为 `admin`，初始密码保存在：

```text
/home/ntp-gateway/initial-password.txt
```

首次登录后必须修改密码。

初始密码来源优先级：

1. 环境变量 `NTP_GATEWAY_DEFAULT_PASSWORD`。
2. `/home/ntp-gateway/default-password.txt`。
3. 未配置以上两项时自动生成随机密码。

`default-password.txt` 已被 Git 忽略，不会提交到代码仓库。

## 硬件串口识别

首次安装且 `config.json` 不存在时，安装程序会按当前硬件生成串口列表，
不再固定为四路 `/dev/ttyS1` 至 `/dev/ttyS4`。

识别优先级：

1. `hardware_profile.json` 或 `hardware-profile.json`。
2. `/proc/tty/driver/serial` 中登记的板载 UART。
3. `/sys/class/tty` 中的板载 UART。
4. 没有板载串口时，回退到 `/dev/ttyUSB*`、`/dev/ttyACM*`。

当前活动控制台（`/sys/class/tty/console/active`）会自动排除。
板载 UART 存在时不会混入 USB 串口。

查看当前硬件识别结果：

```sh
cd /home/ntp-gateway
python3 app.py --list-serial
```

更换硬件或需要重新生成串口列表时：

```sh
cd /home/ntp-gateway
python3 app.py --reset-ports
/etc/init.d/S55ntp-gateway restart
```

`--reset-ports` 会按 `/dev` 节点保留已有端口的波特率、校验和调度配置。

硬件配置文件示例见 `hardware_profile.example.json`，复制为
`/home/ntp-gateway/hardware_profile.json` 后按实际原理图修改：

```json
{
  "serial_ports": [
    {"device": "/dev/ttyS1", "name": "COM1", "description": "RS485-1"},
    {"device": "/dev/ttyS2", "name": "COM2", "description": "RS485-2"}
  ]
}
```

## 回滚

```sh
/home/ntp-gateway/rollback.sh
```

回滚会停止新服务、恢复原始 `/etc/htset/autorun` 并启动
`/home/openGW/openGWEnterprise.sh`。

## 目录

```text
/home/ntp-gateway/
├── app.py
├── config.json
├── default_config.json
├── hardware_profile.json
├── initial-password.txt
├── logs/
│   ├── gateway.log
│   └── console.log
├── web/
└── backup/
```

## API

统一前缀 `/api/v1`，统一响应：

```json
{"code": 0, "message": "success", "data": {}}
```

登录接口返回 Token，后续请求使用：

```text
Authorization: Bearer <token>
```

主要接口：

```text
POST /api/v1/login
GET  /api/v1/status
GET  /api/v1/ntp
POST /api/v1/ntp
POST /api/v1/ntp/sync
GET  /api/v1/serial
POST /api/v1/serial
POST /api/v1/serial/{id}/test
POST /api/v1/serial/{id}/sync
GET  /api/v1/schedule
POST /api/v1/schedule
GET  /api/v1/logs
GET  /api/v1/network
POST /api/v1/network/stage
POST /api/v1/network/confirm
POST /api/v1/network/rollback
POST /api/v1/account/password
GET  /api/v1/config/export
POST /api/v1/config/import
POST /api/v1/system/reboot
```

## 尚未冻结的协议项

- `0x10` 全对时原始报文的 CRC 与厂家样例不一致。
- 仅实现全对时报文，不生成任何分对时请求。
- 物理端子与 `/dev` 串口节点的对应关系需结合原理图核对。
- RS485 DE/RE 是否由驱动 `TIOCSRS485` 控制需现场验证。

在上述项目完成确认前，不得将串口设为自动发送。
