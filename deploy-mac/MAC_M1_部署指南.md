# ZERO STUDIO · M1 Mac 监控机 + Windows 5070 Ti 修图机 极简部署指南

这套方案让你的 **M1 Mac 24 小时低功耗托管全部在线业务**（约 5~10W 耗电、静音不发热），你的 **Windows 5070 Ti 主机彻底解放，平时直接关机，只有修图返图时才开机**。

---

## 一、两台电脑分工一览

| 电脑 | 承担任务 | 运行状态 |
| :--- | :--- | :--- |
| **M1 Mac（监控机）** | 预约网站 + 数据库 (`data.db`) + 一体化中控台 + 公网穿透 (`frpc`) + QQ 监控 (`NapCatQQ`) + 展后 SD 卡拷卡到 NAS + 轻量 Win11 虚拟机挂微信 `4.1.8` | 24 小时常开（脚本已自带防休眠） |
| **Windows 5070 Ti（修图机）** | 挂载 NAS / Mac 共享盘 → 读原片、AI 选片/仿色/Lightroom 修图 → 导出成片 → 打开 Mac 中控台一键推送返图 | 平时关机，修图才开 |

---

## 二、第 1 步：把项目拷到 M1 Mac 上（3 分钟）

1. 先停掉 Windows 主机上的 `启动_ZERO_STUDIO.bat`（**防止两台电脑同时连 FRP 冲突**）。
2. 把整个 `D:\zero-studio-automation` 文件夹拷到 M1 Mac（例如放到 `~/zero-studio-automation`）：
   - **必拷文件**：`.env`、`configs/im_rules.json`、`apps/booking-site/data.db`、`apps/booking-site/uploads/`。
   - **可不拷的大目录**：`apps/booking-site/node_modules`、`infra/napcat`（Mac 端会自动重装原生依赖）。

---

## 三、第 2 步：在 M1 Mac 上一键启动核心服务（网页 + 数据库 + 中控台 + 公网穿透）

打开 Mac「终端」，首次安装基础环境（如果已经装过直接跳过）：
```bash
brew install node python@3.11
```

然后进入项目目录，一键启动：
```bash
cd ~/zero-studio-automation
chmod +x deploy-mac/*.sh
bash deploy-mac/start_mac_monitor.sh
```

看到 **`✅ M1 Mac 监控机已全部启动！`** 就完成了：
- 本机中控台：`http://127.0.0.1:3080`
- 客户公网预约页：`http://<你的VPS公网IP或域名>:8080`
- 手机/平板公网中控台：`http://<你的VPS公网IP或域名>:8081`

*(脚本自动调用了 `caffeinate -dims`，Mac 息屏不会休眠断网。)*

---

## 四、第 3 步：在 M1 Mac 上启动 QQ 监控（NapCatQQ）

1. 在 M1 Mac 上安装 **[OrbStack](https://orbstack.dev)**（M1 Mac 上最轻量的 Docker 运行工具，仅占几百 MB 内存）。
2. 终端执行：
   ```bash
   bash ~/zero-studio-automation/deploy-mac/setup_mac_napcat.sh
   ```
3. 浏览器打开 `http://127.0.0.1:6100/webui` 扫码登录 QQ，在网络配置里填：
   - **WebSocket 客户端**：`ws://host.docker.internal:8090/ws/napcat`
   - **HTTP 服务端**：端口 `3002`

---

## 五、第 4 步：在 M1 Mac 上挂 PC 微信自动回复（免费方案）

因为 `wxauto4` 只支持 Windows 版微信 `4.1.8.107`，在 M1 Mac 上最稳的方案是装一个**极简 Win11 ARM 虚拟机**专挂微信：

1. 在 M1 Mac 下载安装 **[VMware Fusion Pro](https://www.vmware.com/products/desktop-hypervisor/workstation-and-fusion)**（Broadcom 官方对个人完全免费）或 Parallels Desktop。
2. 分配 **2 核 CPU + 4GB 内存** 安装 Win11 ARM（Win11 ARM 原生支持无缝运行 x64 版微信）。
3. 在虚拟机里：
   - 安装 `infra/weixin_4.1.8.107.exe` 并运行 `禁止微信自动更新.bat`；
   - 正常登录微信并保持主窗口打开；
   - 运行 `im-gateway`（只跑这 1 个服务，把 `.env` 里的 `BOOKING_API_URL` 改成 Mac 的局域网 IP，例如 `http://192.168.1.10:3000`）。
4. 回到 Mac 端的 `.env`，把 `IM_GATEWAY_URL` 改成虚拟机的 IP（例如 `http://192.168.1.25:8090`），重跑一次 `bash deploy-mac/start_mac_monitor.sh` 即可打通全链路！

---

## 六、第 5 步：M1 Mac 拷卡 + Windows 5070 Ti 专心修图

1. 在 M1 Mac 的 **系统设置 → 通用 → 共享 → 文件共享** 打开 SMB，把你的外接硬盘或 NAS 目录共享出来（或者两台电脑都挂载同一台 NAS）。
2. 在 Mac 的 `.env` 里将 `NAS_ROOT` 改为该路径（如 `/Volumes/StudioNAS`）。
3. **日常工作流**：
   - 漫展回来 → 把 SD 卡插到 M1 Mac → 中控台点「一键拷卡入库」（自动按场记二维码/角色分文件夹存入 NAS）。
   - 需要修图时 → 打开 **Windows 5070 Ti** → 访问共享盘直接用 5070 Ti 修图、导出成片到对应订单的交付目录 → 在浏览器打开 Mac 中控台点 **「推送返图」**，M1 Mac 自动发微信/QQ 通知客户收图！

---

## 七、进阶省钱必开：LM Studio 本地小模型前置守门员（0 Token 拦截闲聊）

系统已内置「**本地小模型先判断 → 相关才转 DeepSeek 云端 API 生成回复**」双级路由：

1. 在 M1 Mac 打开 **LM Studio**，搜索下载轻量模型（推荐任选其一，M1 1秒出结果）：
   - `DeepSeek-R1-Distill-Qwen-1.5B`（Q4_K_M，仅占 ~1.1GB 内存，已自动剥离 `<think>` 思考过程）
   - 或 `Qwen2.5-3B-Instruct`（Q4_K_M，不带长思考，速度更快 ~0.3 秒）
2. 在 LM Studio 左侧点 **Developer（本地服务）** → 加载该模型 → 点击 **Start Server**（默认监听 `http://127.0.0.1:1234/v1`；如果 Win11 虚拟机要连它，打开「Serve on Local Network」）。
3. 修改 `.env` 里的一行开关并重启 `im-gateway`：
   ```env
   LOCAL_LLM_ENABLED=true
   LOCAL_LLM_BASE_URL=http://127.0.0.1:1234/v1
   LOCAL_LLM_MODEL=auto
   ```
   *(如果 `im-gateway` 跑在 Win11 虚拟机里，把 `127.0.0.1` 换成 Mac 的局域网 IP 即可。)*
   - **效果**：日常闲聊由 M1 Mac 本地 0 成本直接拦截；只有判定为摄影约拍相关消息，才会调用 DeepSeek 云端 API 用真人短句气泡回复；若 LM Studio 没开，自动无缝降级到云端 DeepSeek，绝不漏单。
