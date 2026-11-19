# ACEswarm H16 地面站

ACEswarm 以独立 Android 应用安装在遥控器上，包名为 `dev.aceswarm.h16`。适配目标是 H16 的 Android 7.1（API 25）和 Chromium 52 系统 WebView。

## 在遥控器上使用

- 点击桌面的「应用」可搜索这台遥控器上有启动入口的应用。已安装的 QGroundControl 和 NoMachine 默认出现在桌面上。点击后由 Android 启动对应应用。
- 「控制台」（Console）打开原有的 Aivuda 地面站 APK。连接飞机时，沿用该客户端内配置的服务器地址。未安装该客户端时，控制台会显示网页地址输入框。
- 首次打开「应用商店管理」（AppStore Admin）时，填写 H16 能访问的 AivudaAppStore 服务地址。商店界面随 APK 安装，账号、应用包和发布操作由选定的服务器管理。以后可在「系统 → 应用商店地址」中修改地址。
- 「打开页面」可以访问 HTTP(S) 地址。在网页顶部点击「桌面」可返回工作台。底部任务栏可以重新打开最近使用的应用或网页。Android 进程运行期间最多保留三个网页的现场。
- 在「系统」中切换主题、语言或全屏。原生应用的切换和文件下载由 Android 接管。

## 构建和安装

Linux 构建环境需要 Node.js 22.12+ 或 24、npm、JDK 17 或更高版本、Android SDK Platform 29、Build Tools 34.0.0 和 Platform Tools。如使用自定义 SDK 位置，请设置 `ANDROID_SDK_ROOT`。在仓库根目录运行以下命令。

```sh
./android/build.sh
adb -s <H16设备序列号> install -r android/build/aceswarm-h16.apk
```

脚本从 `electron/` 直接构建桌面界面，按需初始化仓库锁定的 AivudaAppStore 子模块，并将其 Vue 前端编译成 H16 WebView 能运行的版本。脚本在生成 APK 时验证签名和对齐。开发版签名密钥保存在 `${XDG_DATA_HOME:-$HOME/.local/share}/aceswarm-android/`。正式签名可设置 `ACESWARM_SIGNING_KEY` 和 `ACESWARM_KEY_PASSWORD`，后续覆盖升级需沿用签名密钥。

## 平台适配范围

原版渲染器负责桌面、菜单、应用搜索、收藏、主题、语言及地址输入。Android 宿主提供已安装应用列表、原生应用启动、最近使用记录，以及独立的网页 WebView。远端页面与本地桌面的原生接口隔离。

H16 本地运行 Android 应用。Linux 机载应用、AivudaOS 后端和 AppStore 后端运行在对应主机上。Electron 的 Dockview 分屏、MCP 服务、桌面录屏和 GPU 浮层仅在桌面版提供。普通远端网站需要兼容 H16 的系统 WebView；工作台和内置商店前端均在构建阶段完成 Chromium 52 适配。
