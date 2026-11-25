# PX4 uXRCE Basic Control (PX4 v1.17/main)

本机 ROS 2 节点将 Swarm Controlpad 的 Zenoh GET 转为 PX4 指令。节点启动后等待操作请求。将 AivudaOS 的 `agent_id` 与遥测节点设成同一个名字，Controlpad 的 TARGETS 填入这个名字。每架飞机使用独立的 PX4 命名空间或 DDS 域。

## 消息版本

| 用途 | VehicleStatus | VehicleCommand | VehicleCommandAck |
|---|---|---|---|
| 无后缀指令和回执 | v5 | v0 | v0 |
| uav1 实测 | v5 | v1 | v2 |

按本地 `px4_msgs` 的 `MESSAGE_VERSION` 选择 DDS 话题，v0 使用无后缀名。应用从新鲜的 VehicleStatus 读取目标 system/component ID，按指令、源地址和时间匹配飞控 ACK。指令已发送但 2 秒内没有终态 ACK 时，返回失败。定位、起飞前检查及参数错误也会返回原因。

## 控制键

以下后缀均接在 `<agent_id>/control/` 之后。

| 后缀 | 请求体 | 行为 |
|---|---|---|
| `arm` | `{"value":true,"confirm":true}` 或 `{"value":false}` | 解锁或上锁 |
| `disarm` | `{}` | 普通上锁 |
| `set_mode` | `{"mode":"AUTO.LOITER"}` | 选择 OFFBOARD、POSCTL、ALTCTL、STABILIZED、AUTO.LOITER、AUTO.LAND 或 AUTO.RTL |
| `takeoff` | `{"target_altitude_m":1.5}` | 从已着陆的位置向上计相对高度，要求已解锁、检查通过和有效本地定位。默认爬升 0.5 m/s，默认高度上限 10 m |
| `goto` | `{"target":"1, 0.5, 1.2, 90"}` 或 `{"x":1,"y":0.5,"z":1.2,"yaw_deg":90}` | 飞至动捕场地的绝对位置并悬停，要求已解锁、已离地、定位有效且未触发 failsafe |
| `land` | `{}` | 请求 PX4 NAV_LAND |
| `cancel` | `{}` | 将起飞或 GoTo 的位置和航向设定值冻结为当前实测位姿，并请求 AUTO.LOITER |
| `register_extra` | `{}` | 通过 PX4 ROS 2 Control Interface 注册 ACEswarm Hold，返回飞控分配的 nav_state |
| `set_mode` | `{"mode":"EXTRA"}` 或 `{"mode":"EXTERNAL4"}` | EXTRA 指向本节点注册的模式，EXTERNAL1 至 EXTERNAL8 指向各已注册且可选择的槽位 |

在 Controlpad 点击 **Register Extra** 后选 **EXTRA · ACEswarm Hold**，点击 **SetMode**。应用进程管理注册的生命周期，重启后可重新注册。如果首次请求返回 `state=registering`，再次点击 Register Extra 可读取同一次注册的结果。外部模式使用飞控分配的编号，注册本身保持当前飞行模式。该模式需要有效本地定位，激活后保持当前位置。[PX4 ROS 2 Control Interface](https://docs.px4.io/v1.17/en/ros2/px4_ros2_control_interface)

进入 OFFBOARD 前按 20 Hz 发送当前位置和控制心跳，连续预热至少 1.1 s。确认 OFFBOARD 后，起飞使用 NED 坐标逐步减小 down，到达设定高度后继续发送悬停目标。起飞成功回执表示 PX4 已接受切换并开始爬升。实际高度由飞控遥测确认。[PX4 Offboard 模式](https://docs.px4.io/main/en/flight_modes/offboard)

### GoTo 的坐标和回执

在 Controlpad 的 **GoTo** 输入框填写 `x, y, z` 或 `x y z yaw_deg`，确认后发送至所选飞机。默认 ENU 以米计，x 向东、y 向北、z 向上，原点需与 PX4 本地估计对齐。ENU 航向 0° 朝东、90° 朝北。不填航向时保持当前悬停设定航向。两种请求体均可附加 `"frame":"ned"`，此时 x 向北、y 向东、z 向下，航向从北顺时针计。例如 `{"frame":"ned","target":"0.5 1 -1.2 0"}` 与表中示例等价。

目标水平围栏以动捕原点为中心。`uav1` 的 50 × 30 m 场地使用 `goto_fence_shape: rectangle`，`field_east_size_m: 50` 和 `field_north_size_m: 30` 是两个方向的全长。允许的 ENU 目标范围是 x ∈ [−25, 25] m、y ∈ [−15, 15] m，含边界。例如 `-12.5, 0, 1.5` 在场内。NED 请求按相同的东、北边界检查。通用安装包默认使用 `circle`，圆形半径由 `max_xy_radius_m` 决定（默认 3 m）。

绝对高度范围从 `min_goto_height_m`（默认 0.3 m）到 `max_takeoff_height_m`（默认 10 m），含边界。水平和垂直速度分别由 `xy_speed_m_s` 和 `climb_rate_m_s` 限制，默认均为 0.5 m/s。航向沿最短路径以最高 0.6 rad/s 转动。

本节点已在 OFFBOARD 悬停时，可直接更新目标。其他模式下，节点先以实测位置预热 1.1 s，收到 PX4 接受回执且确认 OFFBOARD 后才开始移动。`ok: true` 表示目标已接受，是否抵达应以遥测为准。节点到点后以 20 Hz 持续悬停。Cancel 或 Land 会立即冻结轨迹。定位基准重置或 failsafe 会停止 OFFBOARD 输出。

## 构建与打包

在与飞控匹配的 ROS 2、`px4_msgs` 和 `px4_ros2_cpp` 环境中构建。Python 运行环境需要 `rclpy`、`PyYAML` 和 `eclipse-zenoh` 1.x。

```bash
colcon build --packages-select uxrce_basic_control --cmake-args -DCMAKE_BUILD_TYPE=Release
source install/local_setup.bash
colcon test --packages-select uxrce_basic_control --return-code-on-test-failure
src/uxrce_basic_control/aivudaos/pack.sh artifacts
```

打包脚本收集本机编译的外部模式可执行文件和 PX4 接口库。uav1 使用 ROS 2 Jazzy、aarch64；其他 ROS 2 发行版、架构或消息 ABI 需要用对应环境重建外部模式。AivudaOS 默认加载 `~/ros2_ws/install/local_setup.bash`，可用 `UXRCE_PX4_SETUP` 指定其他 overlay，用 `UXRCE_PYTHON` 指定包含 Zenoh 的 Python。
