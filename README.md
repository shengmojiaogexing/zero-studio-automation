<div align="center">

# ZERO STUDIO Automation · 二次元摄影工作室全链路自动化系统

**面向二次元 Cosplay 摄影工作室的「IM 拓客接单 → 自助预约 → 双校验拷卡 → 场记聚类选图 → AI 仿色 + PS 精修 → 人工终审交付」全链路自动化平台。**

支持 Windows 单机一键托管，以及 **M1 Mac 24 小时低功耗监控机 + Windows 独显随身修图机** 双机协同；
原片全程只读不改动，成片保留 16-bit 可编辑 PSD 图层，绝不跳过摄影师人工终审。

[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![Status](https://img.shields.io/badge/status-WIP%20%E9%9D%9E%E6%9C%80%E7%BB%88%E7%89%88-orange)](#当前版本说明非最终版)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#快速开始)
[![Node.js](https://img.shields.io/badge/node.js-18%2B-green)](#快速开始)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Android-lightgrey)](#快速开始)
[![PS%20%2F%20MCP](https://img.shields.io/badge/AI%20Retouch-%E6%97%A9%E6%9C%9F%E5%AE%9E%E9%AA%8C%20%C2%B7%20%E5%9F%BA%E6%9C%AC%E4%B8%8D%E5%8F%AF%E7%94%A8-red)](#功能现状与更新路线图)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E8%B5%9E%E8%B5%8F%E6%94%AF%E6%8C%81-ff69b4?logo=githubsponsors)](#支持与赞赏)

</div>

---

> [!CAUTION]
> **使用前请先阅读完整免责声明：[`DISCLAIMER.md`](DISCLAIMER.md)**
> 本项目为**个人、非官方、非商业**的开源摄影自动化研究工具，按 GPL-3.0 协议「按现状（AS-IS）」提供，**不提供、不分发**任何商业软件或闭源客户端（如 Photoshop、微信/QQ 安装包等）。使用第三方 IM 接口存在平台风控与封禁风险，请严格遵守各平台用户协议及当地法律法规，一切使用后果由使用者自行承担。

> [!WARNING]
> **当前版本说明（非最终版 / WIP）—— ⚠️ 自动修图模块目前基本不可用**
> 本项目仍处于持续迭代阶段，**并非最终完整版**：
> 1. **⚠️ 自动修图模块（`pipelines/retouch-engine/`）目前基本不可用**：
>    - 虽然 Photoshop COM / MCP 工具调用、原生 ACR 16-bit 解码、全尺寸导出、检查点回溯与 PSAI 蒙版贴片等**底层通道已打通**，但**实际自动调色、肤色还原与跨场景通用出片质量目前仍基本不可用**；
>    - 行为准则与通用修图规范（`configs/retouch_skills/*.md`）正在等待作者人工重写与打磨，**请勿将当前自动修图结果直接用于商业交付**。
> 2. **Photoshop 的 MCP 调用与局部 AI 融合链路**仍在实验演进中，仅作技术架构验证与测试实验室调试使用。
> 3. **接单人设 Skill 与提示词**（私聊真人化接单、群聊拓客打分、本地小模型前置守门）当前为可用初版，多场景话术与边界兜底**尚未完整，需等待后续更新**。

> [!IMPORTANT]
> **数据安全与合规红线**
> - **原片绝对只读**：拷卡与修图管线严格只读相机 SD 卡与 NAS 原始 `.ARW` / `.CR3`，绝不原地修改或自动删卡。
> - **人工终审门禁**：所有 AI 仿色与 PS 精修产物均进入 `REVIEW_PENDING` 等待摄影师审图放行，绝不未经审核直接交付客户。
> - **IM 防封与隐私**：默认开启 `DRY_RUN_ADD_FRIEND=true`（加好友冷却队列演练模式）；`.env`、`configs/im_rules.json`、`data.db`、`infra/napcat/` 均已加入 `.gitignore`，严禁将真实密钥、账号或个人聊天名单提交至公开仓库。

---

## 📊 功能状态速览

| 状态 | 模块 / 功能 | 当前说明 |
| :---: | :--- | :--- |
| ✅ **现已可用** | **客户自助预约与选图终审站** | 自助预约下单、预约二维码、品牌动态配置、2048px 选图 H5、人工终审与返修版本链 |
| ✅ **现已可用** | **只读双校验拷卡与扫码分人聚类** | SD 卡只读双校验拷卡、**START/END 双二维码**自动分人聚类、废片初筛、动作指导手机端查看 |
| ✅ **现已可用** | **可视化中控台与双机协同** | 网页中控台、Setup Wizard 初始化向导、Windows/Mac 双机公网同步（FRP + Worker）、Android 移动端 |
| ✅ **现已可用** | **IM 统一接单基础通道** | NapCatQQ / PC 微信 / OpenClaw 微信收发、LM Studio 本地 0 Token 闲聊守门 + DeepSeek 接单回复 |
| ✅ **现已可用** | **Photoshop 底层驱动与实验室** | Windows COM / MCP 30+ 原子工具、原生 ACR 16-bit 解码、全尺寸 PSD/JPG 导出、Skill 测试实验室 |
| ❌ **基本不可用** | **AI 自动修图成片质量** | 底层通道已通，但**实际自动调色、肤色还原与跨场景出片质量基本不可用**，待作者手写商业行为准则与修图规范 |
| ⚠️ **实验 / 待完善** | **局部 AI 蒙版贴片 & 复杂 IM 客服** | PSAI 局部生图蒙版融合已接通底层接口但未批量验收；改期/售后/互勉初筛与防封话术待补全 |
| 🗺️ **后续添加** | **手写修图规范 & 多风格 Skill 矩阵** | 风格中立通用行为准则 + 亮调低反差 / 日系 / 汉服 / 暗调等多风格可插拔 Skill 与实拍基准集 |
| 🗺️ **后续添加** | **客户对话总结与审美偏好记忆** | 自动总结客户沟通要点，记忆其喜欢的影调、肤色质感与雷点，并在后续约拍与修图中自动应用 |
| 🗺️ **后续添加** | **拍摄纠纷证据整理与公关声明辅助** | 仅整理使用者自有订单/聊天/交付时间线，辅助起草客观脱敏的澄清声明与公关回应（严禁爬取曝光他人隐私或自动发帖，须人工核验发布） |
| 🗺️ **后续添加** | **一键云端交付与返修工作台** | 终审放行后自动打包全尺寸成片、生成交付页并同步通知客户 |

> 完整功能拆解与更新进度见下方 [📊 功能现状与更新路线图](#功能现状与更新路线图)。

---

## 📖 目录

- [这是什么](#这是什么)
- [核心特性](#核心特性)
- [系统架构与工作原理](#系统架构与工作原理)
- [模块与服务矩阵](#模块与服务矩阵)
- [快速开始](#快速开始)
- [命令行与常用脚本](#命令行与常用脚本)
- [完整提示词与 Skill（不完整 / 非最终版）](#完整提示词与-skill不完整--非最终版)
  - [一、Cosplay 修图与仿色 Skill（不完整非终极版）](#一cosplay-修图与仿色-skill不完整非终极版)
  - [二、Gemini 多模态初次仿色提示词与安全钳位表](#二gemini-多模态初次仿色提示词与安全钳位表)
  - [三、摄影师看图反馈 → 增量修订提示词与 JSX 白名单](#三摄影师看图反馈--增量修订提示词与-jsx-白名单)
  - [四、接单人设 Skill 与 IM 三大提示词（不完整非终极版）](#四接单人设-skill-与-im-三大提示词不完整非终极版)
- [Photoshop 自动化与 MCP 修图调用现状（非最终版）](#photoshop-自动化与-mcp-修图调用现状非最终版)
- [项目结构](#项目结构)
- [测试](#测试)
- [常见问题](#常见问题)
- [功能现状与更新路线图](#功能现状与更新路线图)
  - [✅ 现在可用功能](#-现在可用功能)
  - [⚠️ 尚未完善 / 基本不可用功能](#️-尚未完善--基本不可用功能)
  - [🗺️ 后续会添加的功能](#️-后续会添加的功能)
- [支持与赞赏](#支持与赞赏)
- [开源组件与致谢清单](#开源组件与致谢清单)
- [许可协议与免责声明](#许可协议与免责声明)

---

## 🎯 这是什么

漫展与 Cosplay 约拍的日常痛点非常集中：群聊刷屏找摄容易漏单、私聊重复问档期很耗精力、展后几千张 RAW 拷卡分人耗时极长、客户选图靠网盘来回传、修图与多轮返修缺乏可追溯版本链。

本项目把个人/小型二次元摄影工作室的完整业务流做成了一套**本地优先、可控可回滚**的自动化系统：

```text
传统人工流：
  群里翻聊天 ──▶ 手动记档期 ──▶ 手动建文件夹拷卡 ──▶ 发网盘挑图 ──▶ 手动调色/反复改图 ──▶ 手动发返图

ZERO STUDIO 自动化流：
  IM 拓客接单 ──▶ 自助预约锁档 ──▶ 插卡双校验入库 ──▶ 场记二维码 & GPU 聚类
       ──▶ 客户 H5 选图 ──▶ AI 仿色 + PS 精修 ──▶ 看图提意见增量修订 ──▶ 人工终审一键返图
```

> [!NOTE]
> AI 在本系统中只承担「辅助筛选、话术拟稿、参数级仿色建议与增量修订」角色，**不替代摄影师的审美终审**，也**绝不对人脸和服设做整图 AI 重绘**。

---

## ✨ 核心特性

| 特性 | 说明 |
|---|---|
| 🤖 **IM 统一网关与接单大脑** | 支持 QQ（NapCatQQ OneBot v11）、PC 微信（`wxauto`）、企业微信客服；内置「LM Studio 本地小模型 0 Token 拦截闲聊 → DeepSeek 云端真人短气泡回复」双级路由 |
| 📅 **自助预约与锁档网站** | 作品展示、漫展活动/日期/时段容量管理、防刷口令、预约二维码生成、客户自助 H5 选图页与交付页 |
| 💾 **工业级单次流式双校验拷卡** | 支持 `.ARW` / `.CR3` / `.JPG`，4MB 流式写 NAS `.tmp` + SHA-256 源/目标校验 + 原子落盘 + `manifest.json`，并高速提取 2048px WebP 预览图 |
| 🧠 **场记二维码 + GPU 角色聚类** | 自动识别首图场记二维码切段绑单、OpenCV 过滤过暗/过曝/虚焦废片、CUDA（自动回退 CPU）多模态角色聚类与合影多标签回写 |
| 🎨 **AI 仿色 + PS 零干预精修** *(非最终版)* | 只读 NAS RAW 套正式 XMP 转 16-bit TIFF → Gemini 多模态热加载 Skill 输出安全钳位 JSON → PS 原生调整层保留可编辑 PSD → 局部 Patch 羽化去杂物 |
| 🔁 **自然语言看图反馈增量修订** | 摄影师在预览图下写一句意见（如「肤色再透亮一点」「暗部轻抬」），在上一版 PSD 上叠加调整层或安全 ExtendScript，形成可回滚版本链（`v1 → v2 → v3`） |
| 🎛️ **统一可视化中控台 + 移动端** | FastAPI BFF（`127.0.0.1:3080`），HttpOnly 签名 Cookie 鉴权，集中管理 6 大设置中心、Skill 热编辑、单图仿色实验室、终审放行；配套 Android 控制台与 Windows 随身修图客户端 |
| ⚡ **双机省电部署架构** | M1 Mac（5~10W）24 小时托管网站/中控台/FRP/IM 监控，Windows 5070 Ti 主机平时关机、仅修图时开机通过公网 HMAC 领任务并实时回传成片 |

---

## ⚙️ 系统架构与工作原理

```text
┌───────────────────┐      ┌──────────────────────┐      ┌──────────────────────────┐
│  QQ / 微信 / 企微  │ ───▶ │  im-gateway (:8090)  │ ───▶ │  booking-site (:3000)    │
│  NapCat / wxauto  │      │  LM Studio + DeepSeek│      │  Express + SQLite        │
└───────────────────┘      └──────────────────────┘      └─────────────┬────────────┘
                                                                       │
        ┌──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│  ingest-cull-cluster │ ───▶ │  客户 H5 选图 (/gallery)  │ ───▶ │  retouch-engine (Worker) │
│  双校验拷卡 + GPU聚类  │      │  2048px WebP 选片锁单     │      │  XMP→TIFF→AI→PS→PSD      │
└──────────────────────┘      └──────────────────────────┘      └─────────────┬────────────┘
                                                                              │
        ┌─────────────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│  control-center      │ ───▶ │  摄影师终审门禁           │ ───▶ │  客户交付 (/delivery)     │
│  (:3080) + FRP 穿透   │      │  通过放行 / 意见增量修订  │      │  IM 自动推送返图通知       │
└──────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
```

关键安全边界：
- **内网隔离**：`booking-site` 的 `/api/internal/*` 与 `im-gateway` 的 `/api/gateway/*` 仅监听本机回环并校验 `X-API-Key`，前端浏览器永不接触内部密钥。
- **公网修图协同**：Windows 随身修图机走 `/api/worker/*` + HMAC 签名鉴权，从 Mac 公网主站领取 `SELECTED` 订单与修订任务，修完实时回传新版预览图与 PSD。

---

## 🧩 模块与服务矩阵

| 模块 | 路径 | 技术栈 | 默认端口 | 核心职责 | 当前状态 |
|---|---|---|---:|---|:---:|
| **预约与主业务站** | `apps/booking-site` | Node.js + Express + SQLite | `3000` | 首页、作品集、自助预约、选图 H5、终审页、内部任务队列与数据库 | ✅ 可用 |
| **统一可视化中控台** | `apps/control-center` | Python + FastAPI | `3080` | 登录鉴权、6 大设置中心、Skill/XMP 热管理、单图仿色实验室、手动导入与终审 | ✅ 可用 |
| **IM 统一接单网关** | `services/im-gateway` | Python + FastAPI + OpenAI SDK | `8090` | QQ/微信/企微收发、二维码解析、LM Studio 前置守门、DeepSeek 接单与拓客 | 🚧 接单 Skill 持续完善 |
| **拷卡与分拣聚类管线** | `pipelines/ingest-cull-cluster` | Python + OpenCV + PyTorch(可选) | — | 热插拔/手动拷卡、SHA-256 双校验、WebP 预览、场记二维码、废片筛除、GPU 聚类 | ✅ 可用 |
| **AI 仿色与 PS 修图引擎** | `pipelines/retouch-engine` | Python + Gemini/OpenAI Vision + PS COM/UXP | — | XMP→16-bit TIFF、热加载 Skill 仿色、PS 自动化调色/导出、反馈增量修订 | 🚧 非最终版（PS MCP/Skill 迭代中） |
| **Windows 随身修图客户端** | `apps/win-launcher` | Python + Tkinter (PyInstaller `.exe`) | — | 一键启停修图 Worker、检测本机 PS COM 与 Mac 主站连通性、实时日志查看 | ✅ 可用 |
| **Android 移动中控 App** | `apps/android-console` | Android (Java / Gradle) | — | 手机端快捷访问中控台、审图与状态查看 | ✅ 可用 |
| **Mac 24h 监控部署套件** | `deploy-mac` | Shell + `launchd` + OrbStack | — | M1 Mac 一键启动、防休眠守护、NapCat 容器化、Win11 ARM 微信节点指引 | ✅ 可用 |
| **内网穿透与 IM 底座** | `infra/tunnel` · `infra/napcat` | FRP (TCP/XTCP) + NapCatQQ | `7000/8080/8081` | 公网预约页/中控台安全穿透、QQ OneBot v11 协议底座 | ✅ 可用 |

---

## 🚀 快速开始

### 前置要求

- **Python 3.10+**（推荐 3.11 / 3.12）
- **Node.js 18+**（用于 `apps/booking-site`）
- **Adobe Photoshop**（仅修图机需要；Windows 推荐 CC 2024/2025/2026 + `pywin32`）
- 可选：`exiftool`（高速提取 RAW 内嵌预览与 EXIF）、NVIDIA CUDA（GPU 聚类加速）、LM Studio（本地 0 Token 闲聊守门）

### 1. 克隆与配置环境变量

```bash
git clone <你的仓库地址>
cd zero-studio-automation
cp .env.example .env
```

按需编辑 `.env`（详细注释见 [`.env.example`](.env.example)）：
- 基础安全：`INTERNAL_API_KEY`、`ADMIN_USER`、`ADMIN_PASSWORD`、`CONTROL_CENTER_SECRET`
- 接单大脑：`DEEPSEEK_API_KEY`（可选 `LOCAL_LLM_ENABLED=true` 接入 LM Studio）
- 视觉仿色：`VISION_BASE_URL` / `VISION_API_KEY` / `VISION_MODEL` 或原生 `GEMINI_API_KEY`
- 存储与 PS：`NAS_UNC_PATH` / `NAS_DRIVE_PATH`、`PS_BACKEND=auto`、`PUBLIC_BASE_URL`

### 2. 安装依赖

```bash
# Node 预约主站
cd apps/booking-site && npm install && cd ../..

# Python 各模块依赖
pip install -r apps/control-center/requirements.txt
pip install -r services/im-gateway/requirements.txt
pip install -r pipelines/ingest-cull-cluster/requirements.txt
pip install -r pipelines/retouch-engine/requirements.txt
```

### 3. 启动方式

#### 方式 A：Windows 单机全栈一键启动

双击根目录 **`启动_ZERO_STUDIO.bat`**，或在终端运行：

```powershell
python studio_supervisor.py
```

`studio_supervisor.py` 会按 `.env` 开关统一拉起并守护 `booking-site`、`im-gateway`、`control-center`、`frpc-tunnel`、`retouch-worker`、`card-watcher`，并输出健康检查状态与日志（`logs/`）。

启动后访问：
- 本地可视化中控台：`http://127.0.0.1:3080`
- 本地预约网站：`http://127.0.0.1:3000`
- 预约后台管理：`http://127.0.0.1:3000/admin.html`

#### 方式 B：M1 Mac（24h 监控机）+ Windows 5070 Ti（随身修图机）双机部署

适合希望主机平时关机省电、仅修图时开机的场景：
- **M1 Mac**：24 小时托管预约站、数据库、中控台、FRP、QQ/微信监控与拷卡，详见 **[`deploy-mac/MAC_M1_部署指南.md`](deploy-mac/MAC_M1_部署指南.md)**。
- **Windows 修图机**：双击 **`ZeroStudio随身修图机.exe`**（或 `启动_Windows随身修图机.bat`），通过公网 HMAC 自动从 Mac 主站拉取待修任务并实时回传新版预览与 PSD。

---

## 🛠️ 命令行与常用脚本

```bash
# 全栈守护启动 / 停止（Windows）
python studio_supervisor.py
# 或双击：启动_ZERO_STUDIO.bat / 一键停止_ZERO_STUDIO.bat

# 手动拷卡入库（单次指定目录）
python pipelines/ingest-cull-cluster/ingest.py --source "F:\DCIM" --event "BACC" --venue "北京" --auto-cluster

# 热插拔自动拷卡监听
python pipelines/ingest-cull-cluster/ingest.py --watch --auto-cluster --auto-notify

# 修图 Worker 单次执行 / 轮询守护
python pipelines/retouch-engine/worker.py --once
python pipelines/retouch-engine/worker.py --interval 10

# Mac 端一键启动与重启（详见 deploy-mac/）
bash deploy-mac/start_mac_monitor.sh
bash deploy-mac/restart_mac_services.sh
```

---

## 🧠 完整提示词与 Skill（不完整 / 非最终版）

> [!CAUTION]
> **以下全部提示词与 Skill 均为当前阶段的不完整 / 非终极版（WIP）**：
> - **修图 Skill** 与 **接单人设 Skill** 支持热加载（修改后无需重启代码即可生效），目前仅覆盖核心红线与基础场景，后续会持续补充更多光影/肤质/角色/话术细则；
> - 涉及个人聊天名单与私有地址的运行时配置已脱敏，真实运行请通过 `.env` 与 `configs/im_rules.json`（或中控台网页端）配置。

---

### 一、Cosplay 修图与仿色 Skill（不完整非终极版）

> 文件位置：[`configs/retouch_skills/default_cosplay_skill.md`](configs/retouch_skills/default_cosplay_skill.md)
> 加载机制：`pipelines/retouch-engine/gemini_colorist.py` 每次调用视觉模型时自动热读取 `configs/retouch_skills/*.md` 并拼接注入提示词。

<details open>
<summary><b>展开查看当前完整修图 Skill 全文（WIP 非终极版）</b></summary>

```markdown
# ZERO STUDIO · Cosplay 修图与仿色核心规则基线 (Default Skill)

> 本目录 `configs/retouch_skills/*.md` 支持热加载。任何新增的摄影课/后期笔记 `.md` 文件都会在下次调用 Gemini 仿色大脑时自动注入系统提示词，无需修改代码。

## 1. 绝对红线（Hard Constraints）
1. **严禁整图重绘人脸与服设**：绝对禁止对人物五官、妆容、假发发际线、瞳孔美瞳、角色服装花纹、徽章与核心道具执行整图或大面积重绘。
2. **局部生图严格限定**：仅当背景中存在明显穿帮元素（如绿色安全出口牌、路人半身、地面垃圾、杂乱支架）时，才允许设置 `needs_local_inpaint = true`，且 `inpaint_region` 必须紧贴背景杂物小范围裁切（面积严格不超过整图 35%），严禁框选 Coser 本人。
3. **肤色保护红线**：
   - 保持 Cosplay 二次元清透白皙但有血色的自然肤质，严禁过度提亮导致五官轮廓丢失或死白反光。
   - `oranges`（亚洲人肤色主通道）色相偏移严格控制在 `[-8, +8]`，饱和度微调在 `[-18, +15]`，防止脸色发黄、发绿或蜡黄。
   - `skin_luminance` 保守调节，优先保持脸部与颈部明度一致。

## 2. 三大核心预设仿色原则
### A. `cyan_air_event`（苍蓝通透场照）
- **整体基调**：清冷空气感、高光干净微冷、暗部轻抬透亮不发闷。
- **色彩倾向**：略微降低色温（`temp_delta` 约 `-2 ~ -8`），高光微偏青蓝，`blues`/`aquas` 适度提纯清透，压低杂乱环境黄绿反射光（适当降低 `yellows`/`greens` 饱和度）。
- **曲线风格**：柔和 S 曲线，黑场轻提（`[0, 6]` ~ `[0, 12]`），高光保留细腻空气感。

### B. `warm_film_daily`（暖调胶片日常）
- **整体基调**：温柔日系电影感、柔和奶油高光、自然胶片暖调。
- **色彩倾向**：轻微暖色温（`temp_delta` 约 `+2 ~ +8`），暗部微带淡暖或复古青绿，`greens` 向复古偏青黄微调，肤色温暖柔润不过橘。
- **曲线风格**：胶片褪色黑场（`[0, 10]` ~ `[0, 16]`），高光平缓滚落（`[255, 246]`）。

### C. `high_contrast_studio` / `cyber_studio`（高反差棚拍 / 赛博棚拍）
- **整体基调**：立体强明暗光比、深邃纯净暗部、强轮廓高光与戏剧氛围。
- **色彩倾向**：根据画面主光源强化冷暖撞色（如霓虹蓝青 vs 紫红/暖橙轮廓光），保持人物面部受光面干净立体。
- **曲线风格**：明确电影感 S 曲线，压实暗部细节（不死黑），高光锐利通透。

## 3. 参数输出规范
- 所有数值为在正式基础 XMP 转换后的**精细微调增量**，切忌激进拉满。
- 严格输出约定的 JSON Schema，并用中文简明填写 `summary`（不超过 80 字），便于摄影师在终审门禁页快速核验。
```

**🚧 当前尚未完整、待后续补全的修图 Skill 模块：**
- [ ] **精细人像皮肤与结构规则**：中性灰 / 高低频磨皮边界、眼白/瞳孔高光、假发碎发与发际线保留规范
- [ ] **复杂混合光源漫展场馆规则**：室内 LED 偏紫/偏绿、顶光阴影、多灯色温冲突的定向校正策略
- [ ] **材质与服设分级保护**：丝绸/漆皮/金属甲胄/发光特效道具的高光滚落与饱和度保护
- [ ] **参考图精准仿色拆解**：分离「主体肤色保护」与「环境氛围迁移」的双区评估规则
- [ ] **二次元合成（99 元档）专项 Skill**：抠图边缘融光、景深匹配、统一环境反光与氛围雾效规范

</details>

---

### 二、Gemini 多模态初次仿色提示词与安全钳位表

> 代码位置：[`pipelines/retouch-engine/gemini_colorist.py`](pipelines/retouch-engine/gemini_colorist.py)
> 说明：先注入全部已启用 `configs/retouch_skills/*.md`，再拼接当前任务上下文与下方 JSON 契约，并附带 PS 导出的纯画面 1024px sRGB 预览图（及可选参考图）。

<details>
<summary><b>展开查看初次仿色完整提示词模板与代码层安全边界钳位表</b></summary>

#### 1. 任务注入模板

```text
{skills_text}

=== 当前修图任务 ===
- 目标预设代码: {preset_code}
- 预设说明: {preset_notes or '按规则库执行'}
- 角色: {character or '未知'}
- 活动: {event_name or '未知'}
- 第一张图是 PS 导出的纯画面 1024px sRGB 原图预览；{若有参考图：第二张图是目标仿色参考图。 / 无额外参考图。}

{GEMINI_JSON_CONTRACT_PROMPT}
```

#### 2. 强制 JSON 输出契约（`GEMINI_JSON_CONTRACT_PROMPT`）

```text
你必须严格仅输出一个合法的 JSON 对象（不要包含 Markdown 代码块或任何额外解释文字），Schema 如下：
{
  "exposure_delta": 0.0,
  "temp_delta": 0.0,
  "tint_delta": 0.0,
  "hsl_adjustments": {
    "reds": {"hue": 0, "saturation": 0, "luminance": 0},
    "oranges": {"hue": 0, "saturation": 0, "luminance": 0},
    "yellows": {"hue": 0, "saturation": 0, "luminance": 0},
    "greens": {"hue": 0, "saturation": 0, "luminance": 0},
    "aquas": {"hue": 0, "saturation": 0, "luminance": 0},
    "blues": {"hue": 0, "saturation": 0, "luminance": 0},
    "purples": {"hue": 0, "saturation": 0, "luminance": 0},
    "magentas": {"hue": 0, "saturation": 0, "luminance": 0}
  },
  "skin_luminance": 0.0,
  "curves_points": [[0, 0], [64, 64], [128, 128], [192, 192], [255, 255]],
  "needs_local_inpaint": false,
  "inpaint_region": null,
  "inpaint_prompt": "",
  "summary": "中文简明调色摘要（<=80字）"
}
注：inpaint_region 若启用局部去杂物，必须为归一化坐标 [x1, y1, x2, y2] (0.0~1.0)，严禁框选人脸与服设。
```

#### 3. 代码层硬安全边界钳位表（防模型幻觉修崩画面）

| 参数项 | 允许范围 | 说明 |
|---|---|---|
| `exposure_delta` | `[-1.5, +1.5]` | 曝光增量硬上限 |
| `temp_delta` / `tint_delta` | `[-15.0, +15.0]` | 色温 / 色调偏移硬上限 |
| `skin_luminance` | `[-10.0, +15.0]` | 肤色明度保守区间 |
| `hsl.oranges`（亚洲人肤色主通道） | `hue [-8, +8]` · `sat [-18, +15]` · `lum [-12, +15]` | 肤色特严保护，防发黄/发绿/死白 |
| 其余 7 个 HSL 通道 | `hue [-20, +20]` · `sat [-30, +30]` · `lum [-25, +25]` | 常规色彩通道钳位 |
| `curves_points` | 单点偏移 `<= 35`，强制单调递增 | 防止曲线反相/太阳化 |
| `inpaint_region` 面积占比 | `<= 35%`（`0.35`） | 超限直接禁用局部生图并记录 `clamped_fields` |

</details>

---

### 三、摄影师看图反馈 → 增量修订提示词与 JSX 白名单

> 代码位置：[`pipelines/retouch-engine/gemini_colorist.py`](pipelines/retouch-engine/gemini_colorist.py) & [`pipelines/retouch-engine/revision_engine.py`](pipelines/retouch-engine/revision_engine.py)
> 说明：摄影师在预览图下方输入一句自然语言意见，模型只输出「相对上一版 PSD 的增量修改」或一段受安全审查的 ExtendScript（ES3），绝不推翻重做。

<details>
<summary><b>展开查看反馈修订完整提示词与 JSX 安全审查规则</b></summary>

#### 1. 反馈修订任务模板

```text
{skills_text}

=== 反馈修订任务 ===
- 这是第 {version} 版的修订
- 上一版做了什么: {previous_summary or '（未知）'}
- 出片预设: {preset_code or '（未知）'}
- 图：当前预览（也就是上一版修完的样子）
- 摄影师的意见：{feedback_text}

请只针对这条意见给出**增量**修改。

{GEMINI_REVISION_CONTRACT_PROMPT}
```

#### 2. 增量修订契约（`GEMINI_REVISION_CONTRACT_PROMPT`）

```text
你必须严格仅输出一个合法的 JSON 对象（不要包含 Markdown 代码块或任何额外解释文字），Schema 如下：
{
  "deltas": {
    "exposure_delta": 0.0,
    "temp_delta": 0.0,
    "tint_delta": 0.0,
    "hsl_adjustments": {
      "reds": {"hue": 0, "saturation": 0, "luminance": 0},
      "oranges": {"hue": 0, "saturation": 0, "luminance": 0},
      "yellows": {"hue": 0, "saturation": 0, "luminance": 0},
      "greens": {"hue": 0, "saturation": 0, "luminance": 0},
      "aquas": {"hue": 0, "saturation": 0, "luminance": 0},
      "blues": {"hue": 0, "saturation": 0, "luminance": 0},
      "purples": {"hue": 0, "saturation": 0, "luminance": 0},
      "magentas": {"hue": 0, "saturation": 0, "luminance": 0}
    },
    "skin_luminance": 0.0,
    "curves_points": [[0, 0], [64, 64], [128, 128], [192, 192], [255, 255]]
  },
  "jsx": "",
  "summary": "中文说明这次改了什么（<=80字）",
  "confidence": 0.0
}
规则：
1. deltas 里的数值是**相对上一版的变化量**，不是绝对值。只想局部微调时，其余字段全部填 0。
2. 如果用户的意见无法用调色参数表达（例如「把背景换掉」「去掉画面外那个人」「加个光晕」），
   才填写 jsx 字段，里面放可直接在 Photoshop 里执行的 ExtendScript 代码。
   - jsx 必须使用 ES3 语法（var、无箭头函数、无 let/const、无 JSON 对象）
   - 操作对象为 app.activeDocument（当前已打开的是上一版 PSD）
   - 严禁关闭文档、严禁保存到新路径、严禁弹出对话框
3. 用户没有提到的方面一律不要改（保持 0 / 空字符串）。
4. confidence 是 0.0~1.0 的置信度；低于 0.3 时说明你没把握，仍要返回最保守的修改。
```

#### 3. 代码层 JSX 安全审查规则（`_sanitize_revision_jsx`）
- 最大长度 `<= 20000` 字符，且**必须**显式引用 `app.activeDocument`；
- 严禁出现任何以下危险操作（命中即拒绝执行 `jsx`，仅应用安全调色增量）：
  `close` / `saved` / `saveAs` / `app.open` / `app.system` / `system.call` / `execute(` / `$.evalFile` / `new File(` / `.remove()` / `alert(` / `confirm(`。

</details>

---

### 四、接单人设 Skill 与 IM 三大提示词（不完整非终极版）

> 代码位置：[`services/im-gateway/brain.py`](services/im-gateway/brain.py) · 运行时热配置：`configs/im_rules.json`（已 gitignore）
> 说明：包含 **私聊真人化接单人设**、**私聊强制 JSON 业务契约**、**群聊拓客意向打分** 与 **LM Studio 本地小模型前置守门员** 四部分。

<details>
<summary><b>展开查看私聊接单人设 Skill + 强制 JSON 契约（WIP 非终极版）</b></summary>

#### 1. 私聊接单人设提示词模板（`private_system_prompt` / `configs/im_rules.example.json`）

```text
你是「ZERO STUDIO」商业摄影工作室的主理摄影师，正在通过微信/QQ 与客户私聊沟通约拍并锁档。

【铁律：绝不重复追问】
1. 先看「已登记信息 slots」，里面**已经有值的字段一律不要再问**。
2. 同一条信息客户已经说过、或已通过二维码/网站预约提交过，就以它为准，直接确认，不要让他再说一遍。
3. 客户发来图片（尤其是本工作室预约网站的预约成功截图或预约二维码）时，说明他已经预约好了——不要再让他重报 CN/角色/日期。
4. 缺什么只问什么，**一次最多问 1 个问题**，绝不连环追问、绝不把「cn 角色 哪天几点」整串甩给客户。

【收集锁档信息（按此优先级，缺哪个问哪个）】
cn（怎么称呼）→ character（拍什么角色/风格）→ date（哪天）→ event_name（哪场活动/漫展）→ time（大概几点）
remarks（备注）可选，客户不说就不要问。

【说话风格（自然去 AI 味）】
1. 称呼亲切克制，避免过度客套与客服套话。
2. 每次 1~3 条极短气泡，每条 2~12 个字，像真人敲键盘分条发。
3. 干脆、实在、响应明确。

【工作室核心资料（可在 configs/im_rules.json 中按需自定义，问到才说并拆成短气泡）】
1. 套餐与报价：请参考工作室预约页最新价目表（默认支持场照、合成、棚拍与外景定制）。
2. 拍摄配置：专业多灯布光、现场动作引导与基础修图交付。
3. 档期与返图周期：以 `active_events` 已排期档期与预约页公示周期为准。
```

#### 2. 私聊系统强制判断与输出契约（`PRIVATE_JSON_CONTRACT_SUFFIX`）

```text
【系统强制判断与输出格式（请勿忽略）】
1. 先判断「最新客户消息」结合上下文，是否与摄影接单业务相关（约拍、场照、合成、互勉、档期、漫展、报价、返图、器材、角色/Cos，或正在继续之前的约拍沟通）。客户发来预约截图/二维码也属于相关。
2. 【漫展档期防漏单铁律】：`active_events` 里是摄影师当前**已排期并接单的漫展及具体日期**。只要对方私聊问「去不去 XX 漫展 / 某天去吗 / 这几天去不去 / 哪几天在展子 / 接不接」，**必须**将 `is_photography_related` 设为 `true`（绝不静默！）：
   - 对方问的漫展或日期落在 `active_events` 接单日期内（且未锁满）：立刻干脆回复确认会去并接单（如 `["去的", "那天接的", "老大拍啥角色"]`），顺势问缺项推进锁单；
   - 对方问的日期不在 `active_events` 内：简短回复那天不去，并顺带报出 `active_events` 里最近可约的漫展名称与日期。
3. 若只是与摄影/漫展/约拍完全无关的日常闲聊、广告、纯表情包：将 "is_photography_related" 设为 false，"replies" 设为 []，"reply" 留空字符串 ""（保持静默）。
4. 相关时才输出 1~3 条极短气泡到 "replies"。
5. 已经登记在 "current_slots" 里的字段，**不要再问**，也不要在 replies 里重复索要。
6. 若客户发来图片（has_images=true）且内容是预约截图/二维码：把 "received_booking_image" 设为 true，回复只做确认，不要追问已提交的信息。
请严格仅输出一个合法 JSON 对象：
{
  "is_photography_related": true,
  "relevance_reason": "简短理由",
  "received_booking_image": false,
  "asked_field": "本次唯一追问的字段名，没追问则填空字符串",
  "extracted": {
    "cn": "",
    "character": "",
    "event_name": "",
    "date": "",
    "time": "",
    "remarks": ""
  },
  "replies": ["短句1", "短句2"],
  "reply": "短句1\n短句2"
}
```

</details>

<details>
<summary><b>展开查看群聊拓客打分提示词 + LM Studio 本地前置守门员提示词（WIP 非终极版）</b></summary>

#### 3. 群聊拓客意向评估提示词模板（`group_system_prompt`）

```text
你是「ZERO STUDIO」商业摄影工作室的群聊拓客意向评估助手。
请分析一条来自二次元/漫展群聊的发言，判断发言人是否是正在寻找摄影师约拍场照、合成、棚拍或外景的潜在客户。
注意排除：同行摄影师自己发接单广告、纯闲聊吐槽、买卖二手假发服装、与约拍无关的日常聊天。

严格仅输出合法 JSON：
{
  "intent_score": 0到100的整数（80及以上代表明确在找摄影师的优质潜在客户）,
  "is_potential_client": true或false,
  "city": "城市，未知填空字符串",
  "event_name": "漫展或活动名称，未知填空字符串",
  "character": "角色名称，未知填空字符串",
  "shoot_time": "拍摄日期或时间，未知填空字符串",
  "reason": "简短判定依据",
  "add_friend_remark": "当 intent_score>=80 时生成一句自然、不生硬、不超过40字的加好友验证话术，自然打招呼并提及对方活动或角色"
}
```

#### 4. LM Studio 本地小模型前置守门员提示词（`LOCAL_GATEKEEPER_PROMPT`）

```text
你是 ZERO STUDIO 摄影工作室的本地前置守门员。
你的唯一任务：判断对方发来的最新消息（结合简短上下文），是否与「摄影约拍业务」有关。
有关的情况包括但不限于：
- 咨询约拍、场照、合成、棚拍、外景、价格、档期、漫展活动、返图、选图、器材、角色/Cos、预约网站操作
- 正在继续上一轮约拍对话（例如只回复了日期、时段、角色名、“ok”、“可以”、“2张”等）
无关的情况：
- 亲友日常闲聊、吃饭游戏吐槽、群发祝福、广告推销、与摄影完全无关的私事

请严格只输出一个词：YES 或 NO（不要输出其他解释）。
```

**🚧 当前尚未完整、待后续补全的接单人设 Skill 模块：**
- [ ] **改期 / 取消 / 迟到 / 临时加拍**：自动同步修改订单状态与冲突检测话术
- [ ] **返图催图 / 选图引导 / 售后客诉**：自动查询真实订单进度（已拷卡/待选图/精修中/已交付）再回复，避免胡说
- [ ] **互勉试妆图多模态初筛**：结合视觉模型辅助评估服化道与 IP 匹配度并通知摄影师人工确认
- [ ] **动态档期与价格热注入**：将漫展档期、报价、器材信息完全解耦为结构化配置，不再硬编码在提示词中

</details>

---

## 🎨 Photoshop 自动化与 MCP 修图调用现状（非最终版）

> [!NOTE]
> **Photoshop 自动化 / MCP 修图目前还不是最终版**。当前已实现可用、稳定、无弹窗阻塞的「基础调色 + 增量修订 + 局部补丁」管线，但距离完整的 **PS MCP 工具化智能精修闭环** 仍有多个阶段待补全。

| 能力维度 | 当前已实现（现状） | 后续目标（PS MCP 最终版规划） | 状态 |
|---|---|---|:---:|
| **PS 驱动方式** | Windows COM + ExtendScript（`ps_com_bridge.py`，自动静默启停 PS + 空闲自动退出）& UXP 队列轮询桥接（`ps_bridge.py` + `uxp_bridge_snippet.js`） | 封装标准 **Photoshop MCP Server**，向 Agent 暴露结构化、可观测的图层/蒙版/滤镜/导出工具集 | 🚧 演进中 |
| **基础调色执行** | 曝光（Exposure）、色彩平衡（Color Balance）、曲线（Curves）、8 通道 HSL + 肤色提亮、可选 Action 动作组，均以非破坏性调整层写入 16-bit PSD | 补充 Camera Raw 智能滤镜参数级同步、可选颜色（Selective Color）、渐变映射、LUT 混合不透明度控制 | 🚧 待完善 |
| **人像精修能力** | 可通过预设 `ps_action_set / ps_action_name` 调用本地录制的 Photoshop Action | 结合 MCP + 语义分割蒙版，实现可控的瑕疵修复、中性灰/双曲线、自然液化、头发/背景分离调色 | 🧩 规划中 |
| **局部去路人 / 杂物** | 仅裁切归一化 `inpaint_region`（面积 `<= 35%`）调用生图 API，带羽化独立图层贴回 PSD | 接入更精准的对象检测/蒙版生成与光影融边校验 | 🚧 演进中 |
| **多轮修订闭环** | 摄影师写意见 → `revision_engine.py` 在上一版 PSD 上叠加调整层或安全 ExtendScript → 版本号递增（`v1 → v2 → ...`） | AI 视觉自检（修前/修后对比评分 + 肤色/过曝自动回退）+ MCP 多步工具链自主编排 | 🧩 规划中 |

---

## 🗂️ 项目结构

```text
zero-studio-automation/
├── studio_supervisor.py            # ★ 全栈统一进程守护（拉起并监控 6 大核心服务）
├── .env.example                    # 环境变量模板（真实 .env 已 gitignore）
├── 启动_ZERO_STUDIO.bat            # Windows 单机全栈一键启动 / 停止脚本
├── 启动_Windows随身修图机.bat       # Windows 随身修图机启动脚本
├── apps/
│   ├── booking-site/               # 预约展示网站 + SQLite 数据库 + 内部/Worker API
│   ├── control-center/             # 统一可视化中控台 (FastAPI BFF + 单图仿色实验室)
│   ├── win-launcher/               # Windows 随身修图机桌面客户端源码 (Tkinter)
│   └── android-console/            # Android 移动中控 App (Gradle)
├── services/
│   └── im-gateway/                 # IM 统一接单网关（QQ NapCat / 微信 wxauto / 企微 + DeepSeek + LM Studio）
├── pipelines/
│   ├── ingest-cull-cluster/        # 自动拷卡、SHA-256 双校验、WebP 预览、场记二维码、GPU 聚类
│   └── retouch-engine/             # XMP→16-bit TIFF、Gemini 仿色、PS COM/UXP 桥接、反馈修订引擎
├── configs/
│   ├── retouch_skills/             # ★ 热加载修图 Skill 规则库（default_cosplay_skill.md）
│   ├── presets_map.json            # 预设代码 ↔ XMP ↔ 参考图 ↔ PS Action 映射
│   ├── preview_xmp/                # 选图预览专用 XMP（与正式修图严格分离）
│   ├── retouch_xmp/                # 正式修图基础 XMP
│   └── retouch_refs/               # 仿色参考图目录
├── deploy-mac/                     # M1 Mac 24h 监控机 + Win11 ARM 微信节点部署脚本与指南
└── infra/
    ├── tunnel/                     # FRP 公网穿透配置（frps / frpc / XTCP）
    └── napcat/                     # QQ NapCatQQ 运行目录（本地敏感数据已 gitignore）
```

---

## 🧪 测试

各模块均带有独立自动化测试脚本，可按模块或 Sprint 回归运行：

```bash
# Sprint 1 / 7 / 8 & PS 桥接（booking-site）
node apps/booking-site/test_sprint1.js
node apps/booking-site/test_sprint7.js
node apps/booking-site/test_sprint8_manual_ingest.js
node apps/booking-site/test_ps_bridge.js
python apps/booking-site/test_loopback_guard.py
python apps/booking-site/test_worker_sync.py

# Sprint 2：拷卡双校验、场记二维码与聚类
python pipelines/ingest-cull-cluster/test_sprint2.py

# Sprint 3：IM 网关与私聊自动接单
python services/im-gateway/test_sprint3.py
python services/im-gateway/test_private_intake.py

# Sprint 4：Gemini 仿色钳位、PS 修图、反馈增量修订与公网 Worker
python pipelines/retouch-engine/test_sprint4.py
python pipelines/retouch-engine/test_revision.py
python pipelines/retouch-engine/test_worker_public.py

# Sprint 5 / 6 / 8 / 9：中控台鉴权、设置中心、手动导入与源校验
python apps/control-center/test_sprint5.py
python apps/control-center/test_sprint6.py
python apps/control-center/test_sprint8_manual_ingest.py
python apps/control-center/test_sprint9_source_check.py
```

---

## ❓ 常见问题

<details>
<summary><b>为什么 Photoshop 桥接有两种模式（COM 和 UXP）？推荐用哪种？</b></summary>

Windows 修图机推荐默认 `PS_BACKEND=auto`（自动优先使用 **COM + ExtendScript 零干预模式**），无需手动提前打开 Photoshop 或加载 UXP 面板，有任务自动静默拉起、空闲自动退出。UXP 队列轮询模式保留用于跨平台或已加载 UXP 插件的场景；后续将统一演进到标准 PS MCP 调用。

</details>

<details>
<summary><b>修图 Skill 修改后需要重启服务吗？</b></summary>

不需要。`configs/retouch_skills/*.md` 每次调用视觉模型时都会热读取；也可以直接在中控台网页端在线新建、编辑或禁用 `.md` 规则文件。

</details>

<details>
<summary><b>没有配置 Gemini / Vision API Key 能体验单图仿色实验室吗？</b></summary>

可以。`apps/control-center/sandbox_retouch.py` 在未配置 API Key 或网络不可达时会自动降级为基于原图/参考图直方图统计的本地安全仿色方案，并在结果中明确标注降级模式。

</details>

<details>
<summary><b>LM Studio 没开会影响微信/QQ 接单吗？</b></summary>

不会。`LocalGatekeeperClient` 检测到本地 LM Studio 未启动或超时时，会自动无缝降级为云端 DeepSeek 判断与回复，确保不漏单。

</details>

---

## 📊 功能现状与更新路线图

> 本节如实说明项目当前各模块的实际可用度，避免误判直接投入商业生产。

### ✅ 现在可用功能

以下模块已完成核心代码、联调与自动化测试，可直接使用或二次部署：

- **客户自助预约与选图交付站（`apps/booking-site`）**：
  - 客户自助下单预约、自动生成预约二维码、品牌名称/Logo/联系方式动态配置；
  - 客户选图 H5 画廊、在线选片提交、摄影师人工终审页与修订版本链记录。
- **双校验只读拷卡与场记分人聚类（`pipelines/ingest-cull-cluster`）**：
  - SD 卡 → NAS / 本地高速盘双校验只读拷卡（原片零修改、绝不自动删卡）；
  - 场记 **START / END 双二维码** 自动分组聚类、2048px WebP 选图预览生成与废片初筛；
  - 客户动作参考指导简报（Pose Briefing）存储与手机端查看。
- **统一可视化中控台与一键部署（`apps/control-center`、`setup_studio.py`）**：
  - 网页端中控台、Setup Wizard 交互式初始化向导、一键桌面启动器与 Android 移动中控台；
  - Windows 单机托管及 Mac 监控机 + Windows 随身修图机公网同步（FRP + Worker 同步通道）。
- **IM 统一接单网关基础通道（`services/im-gateway`）**：
  - NapCatQQ（OneBot v11）、PC 微信桌面端及 OpenClaw 微信双通道消息收发与通知；
  - LM Studio 本地小模型 0 Token 闲聊守门 + 云端 DeepSeek 接单回复基础闭环（默认开启防封冷却演练）。
- **Photoshop 底层自动化与测试实验室基础设施（`pipelines/retouch-engine`）**：
  - Windows COM + ExtendScript 静默驱动 Photoshop 2025、原生 Adobe Camera Raw 16-bit RAW/XMP 解码；
  - 30+ 标准化 PS MCP 原子工具（`FastMCP`）、全尺寸 16-bit PSD + 全尺寸 sRGB JPG 导出、检查点存档与回溯机制；
  - 可自由勾选 Skill 的图形化测试实验室（`测试_PS修图实验室.bat`）与 PSAI 插件 MCP 桥接安装脚本。

### ⚠️ 尚未完善 / 基本不可用功能

- **❌ AI 自动修图成片质量（基本不可用）**：
  - 虽然 Photoshop 驱动、原生 ACR 解码与 MCP 工具底层已通，但**当前自动调色、肤色还原、反差控制与跨场景通用出片效果仍基本不可用**，极易出现偏灰、偏色或肤色不自然；
  - 通用行为准则与商业级修图规范正在等待作者人工手写重构，**请勿将当前自动修图产物直接用于商业交付**。
- **⚠️ 局部 AI 生图 + 蒙版融合与穿帮消除（实验期）**：
  - 已打通调用 PSAI 插件生成贴片并套用蒙版（`skin` / `bbox` / `subject` / `background`）的底层接口，但实际人像皮肤融合质感、边缘自然度与复杂穿帮消除尚未经过大批量实拍验收。
- **⚠️ 接单人设 Skill 与复杂客服边界（初版待补全）**：
  - 当前仅覆盖基础档期询问与约拍引导，针对改期、取消、定金售后、互勉试妆初筛与复杂防封风控策略尚未完善。

### 🗺️ 后续会添加的功能

- [ ] **作者手写通用行为准则与商业修图规范**：从零重构风格中立的全局修图行为准则（光影还原、肤色标准、材质保护、审片红线），解决跨场景通用性问题。
- [ ] **多风格化修图 Skill 矩阵**：在通用底座之上，逐步补齐漫展亮调低反差、日系通透、汉服暖调胶片、暗调情绪/赛博、棚拍质感等可插拔风格化 Skill。
- [ ] **局部 AI 精修 + 蒙版无损工作流落地**：完善调用 PSAI 插件进行皮肤质感精修与局部穿帮（路人/灯架/柔光箱）移除，自动通过羽化蒙版只保留所需区域并严守脚底投影保护。
- [ ] **精修质量评估基准集（Benchmark）**：建立覆盖不同机位、光源与题材的实拍测试集，量化对比每次 Skill 与规范迭代的真实出片提升。
- [ ] **完整 IM 客服与订单闭环**：接入实时订单状态查询、自动改期/提醒、互勉试妆自动初筛、成片交付自动推送与企业微信客服深度适配。
- [ ] **客户对话智能总结、偏好记忆与修图自动应用**：自动提炼与每位客户的私聊沟通要点，建立长期客户审美偏好档案（喜欢的影调、肤色质感、构图/动作习惯与雷点），在下次约拍与后期修图时自动带入客户偏好并匹配对应 Skill。
- [ ] **拍摄纠纷自有证据链整理与合规公关声明辅助**：针对拍摄或履约纠纷，仅基于使用者自身合法持有的订单/定金/聊天/交付记录梳理客观时间线，辅助起草克制、脱敏的事实澄清说明与回应草稿（**严禁爬取/曝光他人隐私或自动化发帖网暴，对外发布须经当事人人工核验脱敏并自行承担责任**）。
- [ ] **一键云端/网盘交付与客户返修工作台**：打通终审通过后自动打包全尺寸成片、生成带提取码/有效期的交付页并同步通知客户。

---

## ☕ 支持与赞赏

如果这个开源项目帮到了你，或者你期待后续商业修图规范与全链路自动化能力的持续更新，欢迎通过以下方式支持作者：

1. **点一个免费的 ⭐ Star**：这是对持续开源迭代最大的鼓励；
2. **GitHub 赞赏（Sponsor）**：点击本仓库顶部右侧的 **「❤️ Sponsor」** 按钮（已配置 `.github/FUNDING.yml`）；
3. **微信 / 支付宝扫码赞赏**：请作者喝杯咖啡，助力开源项目持续维护；
4. **提交建议 / PR / 贡献修图规范**：欢迎在 Issues 中分享你的摄影工作流痛点或提交风格化 Skill 建议。

<div align="center">

| 微信赞赏 | 支付宝赞赏 |
| :---: | :---: |
| <img src="docs/assets/sponsor_wechat.jpg" width="230" alt="微信赞赏码" /> | <img src="docs/assets/sponsor_alipay.jpg" width="230" alt="支付宝赞赏码" /> |

<sub>⚠️ 所有赞赏纯属自愿无偿赠与，仅用于支持开源项目持续维护，不构成任何软件售卖、商业授权或技术支持合同，请量力而行。</sub>

</div>

---

## 📦 开源组件与致谢清单

本项目构建于以下优秀开源项目与社区生态之上。**所有第三方开源组件均保留其原始版权与开源许可协议**，不包含或覆盖在本项目原创代码的 GPL-3.0 声明之外：

### 1. Web 服务、前端与数据库（`apps/booking-site`）

| 开源项目 | 许可证 | 用途说明 |
|---|---|---|
| [Express](https://github.com/expressjs/express) | MIT | 预约站、选图 H5、终审页与内部任务队列 HTTP 服务 |
| [express-basic-auth](https://github.com/LionC/express-basic-auth) | MIT | 预约后台管理页基础认证 |
| [Multer](https://github.com/expressjs/multer) | MIT | 作品图库、参考图、成片与修订产物上传处理 |
| [node-sqlite3](https://github.com/TryGhost/node-sqlite3) | BSD-3-Clause | 订单、资产批次、修图任务与版本链本地持久化数据库 |
| [node-qrcode](https://github.com/soldair/node-qrcode) | MIT | 客户预约二维码与现场场记二维码生成 |
| [uuid](https://github.com/uuidjs/uuid) | MIT | 唯一 Token 与任务标识生成 |
| [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | MIT | 预约站与展示页前端样式 |

### 2. Python 后端、网络、加密与 AI SDK（`control-center` / `im-gateway` / `retouch-engine`）

| 开源项目 | 许可证 | 用途说明 |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | 统一可视化中控台 BFF 与 IM 统一网关 API 框架 |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | FastAPI 异步 ASGI 运行服务器 |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | 配置与 API 请求/响应数据模型校验 |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause | 服务间内网通信、Gemini/OpenAI 视觉 API 与公网 Worker 同步客户端 |
| [Requests](https://github.com/psf/requests) | Apache-2.0 | 拷卡与聚类结果事务回写 HTTP 客户端 |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | NapCatQQ OneBot v11 WebSocket 双向实时事件通道 |
| [openai-python](https://github.com/openai/openai-python) | Apache-2.0 | 调用 DeepSeek 云端 API 及 LM Studio 本地 OpenAI 兼容推理接口 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause | 本地 `.env` 环境变量加载 |
| [pyca/cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD | 企业微信客服回调加解密与安全验签 |

### 3. 图像处理、RAW 解码与计算机视觉（`ingest-cull-cluster` / `retouch-engine` / `sandbox_retouch`）

| 开源项目 | 许可证 | 用途说明 |
|---|---|---|
| [OpenCV (`opencv-python`)](https://github.com/opencv/opencv-python) | Apache-2.0 | 场记二维码检测、曝光/虚焦废片预筛、HSL/曲线预览渲染与图像编解码 |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | 2048px WebP 预览图生成、EXIF 方向校正与局部 Patch 羽化叠回 |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | float32 高精度 XMP 调色运算、LUT 曲线映射与向量余弦聚类回退 |
| [rawpy](https://github.com/letmaik/rawpy) / [LibRaw](https://github.com/LibRaw/LibRaw) | MIT / LGPL-2.1 | Sony `.ARW` / Canon `.CR3` 原始 RAW 16-bit 无损解码 |
| [ExifRead](https://github.com/ianare/exifread) | BSD-3-Clause | RAW/JPEG EXIF 拍摄时间与机型元数据提取回退方案 |
| [ExifTool](https://exiftool.org/) *(可选外部工具)* | Artistic / GPL-1.0+ | 高速提取 RAW 内嵌预览图与完整 EXIF 元数据 |
| [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar) / [ZBar](https://github.com/mchehab/zbar) *(可选)* | MIT / LGPL-2.1 | 场记二维码兼容解码增强 |

### 4. GPU 多模态角色聚类（`ingest-cull-cluster`，可选增强依赖）

| 开源项目 | 许可证 | 用途说明 |
|---|---|---|
| [PyTorch & TorchVision](https://github.com/pytorch/pytorch) | BSD-3-Clause | CUDA GPU 自动探测与深度学习推理加速 |
| [Hugging Face Transformers](https://github.com/huggingface/transformers) | Apache-2.0 | SigLIP / 视觉多模态服饰与外观特征提取 |
| [OpenCLIP (`open-clip-torch`)](https://github.com/mlfoundations/open_clip) | MIT | 开源 CLIP 视觉表征推理 |
| [InsightFace](https://github.com/deepinsight/insightface) | MIT *(部分模型权重仅限非商业研究)* | 多人人脸特征提取与合影多标签角色匹配 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT | GPU/CPU 跨平台模型推理运行时 |
| [HDBSCAN](https://github.com/scikit-learn-contrib/hdbscan) | BSD-3-Clause | 高维角色特征向量自动密度聚类 |

### 5. IM 协议适配、桌面自动化与部署基础设施

| 开源项目 | 许可证 | 用途说明 |
|---|---|---|
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) *(外部独立部署)* | AGPL-3.0 | QQ OneBot v11 协议适配底座（本仓库不分发其安装包与账号数据） |
| [wxauto](https://github.com/cluic/wxauto) / `wxauto4` *(可选)* | Apache-2.0 / 对应上游协议 | Windows PC 微信桌面端消息监听与收发适配 |
| [pywin32](https://github.com/mhammond/pywin32) *(Windows 可选)* | PSF | Windows COM 零干预驱动本地 Adobe Photoshop 执行 ExtendScript |
| [mikechambers/adb-mcp](https://github.com/mikechambers/adb-mcp) *(可选独立部署)* | MIT | Photoshop UXP + Socket.IO MCP 代理与插件底座（配套 `adb_mcp_client.py` 与一键安装脚本） |
| [Model Context Protocol Python SDK (`mcp`)](https://github.com/modelcontextprotocol/python-sdk) | MIT | 构建标准 `FastMCP` Photoshop 原子工具服务（`ps_mcp_server.py`） |
| [python-socketio](https://github.com/miguelgrinberg/python-socketio) | MIT | 连接 `adb-mcp` Socket.IO Proxy 驱动 Photoshop UXP 插件 |
| [frp (`fatedier/frp`)](https://github.com/fatedier/frp) | Apache-2.0 | 公网 VPS 安全内网穿透（TCP 预约站/中控台 + XTCP P2P 通道） |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0 *(含 Bootloader 例外)* | 打包 Windows 随身修图机桌面客户端（`apps/win-launcher`） |
| [Gradle](https://github.com/gradle/gradle) | Apache-2.0 | 构建 Android 移动中控台客户端（`apps/android-console`） |
| 开源本地小模型（如 [Qwen2.5](https://github.com/QwenLM/Qwen2.5) / [DeepSeek-R1-Distill](https://github.com/deepseek-ai/DeepSeek-R1)） | Apache-2.0 / MIT | 搭配 LM Studio 承担 0 Token 本地前置闲聊守门 |

---

## ⚖️ 许可协议与免责声明

- **开源协议**：本项目原创代码与文档基于 **[GNU General Public License v3.0 or later (GPL-3.0-or-later)](LICENSE)** 开源发布。任何修改或衍生作品在分发时须遵循相同 GPL-3.0 协议并保留版权声明；上述第三方开源依赖及商业闭源软件均适用其各自原始协议，不在本项目授权范围内。
- **免责声明**：完整法律风险提示与责任限制请务必阅读 **[`DISCLAIMER.md`](DISCLAIMER.md)**。本项目为**个人、非官方、非商业**的开源效率研究工具，按「现状（AS-IS）」提供，不提供任何明示或暗示担保；与 Adobe、腾讯（微信/QQ/企业微信）、DeepSeek、Google 等厂商无任何隶属或授权关系。
- **权利人通知与 72 小时友好响应**：若您是任何相关软件、商标、平台或内容的权利人，认为本仓库存在侵权或不当之处，请直接发邮件至 **[2243511192@qq.com](mailto:2243511192@qq.com)**，我们承诺在 **72 小时内**核实并配合删除或修改，**无需律师函或正式法律文书**。

<div align="center">
<sub>ZERO STUDIO Automation · GPL-3.0-or-later · 持续迭代中，欢迎点个 ⭐ Star 关注后续更新</sub>
</div>
