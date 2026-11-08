# 部署与故障处理

本文记录独立 NTP 对时网关在嵌入式 Linux 设备上的部署步骤、现场校验项
和已经处理过的问题。

## 1. 安装前检查

- Python 3.8 以上，标准库即可，不需要 pip 安装依赖。
- 确认设备可写 `/home/ntp-gateway` 和 `/etc/init.d`。
- 确认旧程序 `openGWEnterprise`、`goWDTARM` 可以停止。
- 记录原网卡地址、网关和 DNS，避免修改网络后失联。
- 确认 RS485 物理端子与 `/dev/ttySx` 的对应关系。

查看硬件串口：

```sh
cd /home/ntp-gateway
python3 app.py --list-serial
```

## 2. 安装

```sh
cd /tmp/ntp-gateway-install
./install.sh
```

安装脚本会停止旧程序、安装新服务、替换 `/etc/htset/autorun` 并启动
`S55ntp-gateway`。

首次安装且 `config.json` 不存在时，串口列表由当前硬件自动生成。

## 3. 登录密码

首次登录账号为 `admin`，初始密码保存在：

```text
/home/ntp-gateway/initial-password.txt
```

初始密码来源优先级：

1. 环境变量 `NTP_GATEWAY_DEFAULT_PASSWORD`。
2. `/home/ntp-gateway/default-password.txt`。
3. 自动生成随机密码。

如果页面登录失败，可直接重置：

```sh
cd /home/ntp-gateway
python3 app.py --reset-password
/etc/init.d/S55ntp-gateway restart
```

重置后密码写回 `initial-password.txt`。

## 4. 串口识别

识别优先级：

1. `hardware_profile.json`。
2. `/proc/tty/driver/serial`。
3. `/sys/class/tty`。
4. `/dev/ttyUSB*`、`/dev/ttyACM*` 回退。

活动控制台会自动排除，板载 UART 存在时不会混入 USB 串口。

更换硬件后重新生成串口列表：

```sh
cd /home/ntp-gateway
python3 app.py --reset-ports
/etc/init.d/S55ntp-gateway restart
```

## 5. 安装后校验

```sh
cd /home/ntp-gateway
python3 app.py --check
/etc/init.d/S55ntp-gateway status
tail -n 30 logs/console.log
```

Web 页面重点检查：

- 导航为总览、NTP、网络、RS485、系统 5 项。
- 总览显示系统时间毫秒、NTP 状态和网卡地址。
- 总览与网络页的已激活网卡显示绿色圆点。
- RS485 页面按实际硬件列出串口。
- 每路可选择按秒间隔或每天定点。
- 选择按秒间隔时不显示每天发送时间。
- 选择每天定点时不显示常用周期和自定义秒数。
- NTP 页的“调度策略”与 NTP 配置一并保存。
- 运行日志位于“系统 → 运行日志”，可按级别筛选和搜索关键字。
- 网络、RS485、NTP 在未修改配置时保存按钮为禁用状态。

## 6. 已处理问题

### 6.1 页面提示 `Cannot read properties of undefined (reading 'config')`

原因：串口状态接口返回的配置对象为空或尚未加载完成。

处理：前端对空配置做明确报错并提示刷新页面；后端状态接口始终附带
端口配置。升级后需确认浏览器没有缓存旧版 `app.js`。

### 6.2 `COM1 失败：bytes must be in range(0, 256)`

原因：构造 P3 报文时向字节数组写入了超出 `0..255` 的数值。

处理：报文构造统一按无符号字节和 CRC16 处理，发送前校验帧长度。
升级后先执行“测试发送”，确认现场总线无异常后再启用定时发送。

### 6.3 服务停止时报 `TypeError: 'Event' object is not callable`

原因：`PortWorker` 继承了 `threading.Thread`，却把线程内部保留方法
`_stop` 覆盖成了 `Event`。

处理：内部事件改名为 `_stop_event`，服务重启和关机可正常回收线程。

### 6.4 Web 登录密码与 `initial-password.txt` 不一致

原因：历史安装或手工改密后，配置文件哈希与初始密码文件不同步。

处理：

```sh
cd /home/ntp-gateway
python3 app.py --reset-password
/etc/init.d/S55ntp-gateway restart
```

然后使用 `initial-password.txt` 登录。

### 6.5 新硬件串口数量不对

检查：

```sh
python3 app.py --list-serial
cat /proc/tty/driver/serial
cat /sys/class/tty/console/active
```

如果厂家原理图对端口有固定命名，创建
`/home/ntp-gateway/hardware_profile.json`，参考
`hardware_profile.example.json`。

### 6.6 修改网卡地址后无法访问

Web 网络配置采用两阶段提交：

1. 应用配置后进入 180 秒确认倒计时。
2. 使用新地址重新登录并确认。
3. 未确认会自动回滚。

现场修改前应保留串口或本地控制台作为恢复通道。

## 7. 回滚

```sh
/home/ntp-gateway/rollback.sh
```

回滚会停止新服务、恢复原始 `/etc/htset/autorun` 并启动旧程序。

## 8. 现场注意

- 自动识别只能确认 Linux 串口节点，不能替代原理图核对物理端子。
- 首次启用 RS485 前必须确认从站地址、波特率、校验和接线。
- 不建议在未确认 P3 报文和 CRC 前开启长期自动发送。
- 修改网络、密码和串口配置前先备份 `config.json`。
