# Jetson 监控

机载 AivudaOS 应用通过 jtop 读取 CPU、GPU、内存、芯片温度、输入功率、时钟及风扇状态。默认每两秒发布一帧到 `<agent_id>/telemetry/jetson`。打开地面站 Panel Hub 的设备卡片即可查看监测摘要。

## 配置时钟和风扇

在机载 AivudaOS 的配置中心打开 **Jetson Monitor**。锁频和风扇都默认为 `keep`（保留当前状态）。要更改某一项，选择目标值，保存配置，然后在应用管理中重启 Jetson Monitor。已配置的目标在每次应用启动、取得首帧有效数据后生效。

- **clocks_mode** 选 `enable` 开启锁频，选 `disable` 关闭锁频。
- **fan_profile** 可选设备的 `quiet`、`cool` 或 `manual` 模式。手动模式通过 **fan_speed_pct** 设置转速百分比，`100` 表示全速。

## 安装

1. 确认机载 jtop 服务正在运行，AivudaOS 运行用户属于 `jtop` 组，Zenoh Bridge 的本机接口 `127.0.0.1:17447` 可用。
2. 执行 `./pack.sh`，在机载 AivudaOS 的应用管理中上传生成的包，启动 Jetson Monitor 并按需启用自启动。应用自动使用 Bridge 的 `local_agent_id`。
3. 给地面站现有设备卡片增加指标时，可通过 `node groundstation/patch-dashboard.cjs 原设置.json uxrce_mocap > 新设置.json` 生成含新控件的卡片设置，再导入 Swarm Dashboard。
