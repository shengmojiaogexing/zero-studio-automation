#!/usr/bin/env python3
"""
ZERO STUDIO 统一可视化中控台 (Control Center BFF - 127.0.0.1:3080)

安全架构：
- 所有页面与业务 API 强制校验 HttpOnly Signed Session Cookie（复用 .env 中 ADMIN_USER / ADMIN_PASSWORD）；
- 未登录 API 请求严格返回 401，未登录页面访问重定向至 /login；
- 所有对 booking-site (/api/internal/*) 与 im-gateway (/api/gateway/*) 的调用均在服务端通过 127.0.0.1 内网完成，
  前端浏览器永不接触 INTERNAL_API_KEY、API 密钥或内网端口。
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
STATIC_DIR = APP_DIR / "static"
ENV_PATH = WORKSPACE_ROOT / ".env"
ENV_EXAMPLE_PATH = WORKSPACE_ROOT / ".env.example"
SKILLS_DIR = WORKSPACE_ROOT / "configs" / "retouch_skills"
PRESETS_MAP_PATH = WORKSPACE_ROOT / "configs" / "presets_map.json"
PREVIEW_XMP_DIR = WORKSPACE_ROOT / "configs" / "preview_xmp"
RETOUCH_XMP_DIR = WORKSPACE_ROOT / "configs" / "retouch_xmp"
RETOUCH_REFS_DIR = WORKSPACE_ROOT / "configs" / "retouch_refs"
SANDBOX_WORK_DIR = WORKSPACE_ROOT / "work" / "sandbox"
IM_RULES_PATH = WORKSPACE_ROOT / "configs" / "im_rules.json"
STUDIO_PIDS_PATH = WORKSPACE_ROOT / "logs" / "studio_pids.json"

load_dotenv(ENV_PATH, override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [control_center] %(message)s",
)
logger = logging.getLogger("control_center")

SESSION_COOKIE_NAME = "zs_session"
SESSION_TTL_SEC = 86400 * 7  # 7 天
SECRET_KEY_PATTERN = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASS)", re.IGNORECASE)
SAFE_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+\.md(\.disabled)?$")
SAFE_PRESET_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
DRIVE_REMOVABLE = 2
# 插卡拷卡面板"手动导入"支持的图片格式与单次上限
MANUAL_IMPORT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".arw", ".cr3", ".tif", ".tiff", ".bmp"}
MANUAL_IMPORT_PREVIEW_EDGE = 2048
MANUAL_IMPORT_MAX_FILES = 300
# 现场手动建档的订单使用独立时段，不占用客户自助预约的档期容量
MANUAL_BOOKING_TIME_SLOT = "手动导入"

# ==================== Sprint 6: 按功能场景分组的 6 大设置中心元数据 ====================

SETTINGS_GROUPS_SCHEMA: List[Dict[str, Any]] = [
    {
        "id": "storage",
        "title": "存储与拷卡路径",
        "icon": "💾",
        "description": "配置 NAS 局域网原片备份路径、客户选图预览目录与正式修图工作区（原片始终只读零修改）。",
        "test_action": "test-nas",
        "test_label": "测试 NAS 连通与空间",
        "fields": [
            {"key": "NAS_UNC_PATH", "label": "NAS SMB 网络路径", "default": r"\\192.168.1.100\ZeroStudio\RAW", "desc": "拷卡优先写入的局域网 UNC 路径"},
            {"key": "NAS_DRIVE_PATH", "label": "NAS 本地映射盘符", "default": r"Z:\ZeroStudio\RAW", "desc": "当 UNC 不可达时自动回退的映射盘路径"},
            {"key": "PREVIEW_OUTPUT_DIR", "label": "选图预览图目录", "default": r"D:\zero-studio-automation\apps\booking-site\uploads\previews", "desc": "存放 2048px WebP 预览图，供客户选图 H5 读取"},
            {"key": "RETOUCH_WORK_DIR", "label": "修图本地高速缓存区", "default": r"D:\zero-studio-automation\work\retouch", "desc": "存放只读 RAW 转出的 16-bit TIFF 与 1024px 中间预览"},
            {"key": "RETOUCH_OUTPUT_DIR", "label": "正式成片输出目录", "default": r"D:\zero-studio-automation\apps\booking-site\uploads\outputs", "desc": "精修交付成品目录，挂载为 /outputs 静态路由"},
            {"key": "AUTO_WATCH_EVENT_NAME", "label": "自动插卡默认活动名", "default": "", "desc": "留空时自动按当日日期命名；插卡后自动拷卡、分类并推送客户"},
            {"key": "AUTO_PUSH_GALLERY", "label": "分类后自动推送选图链接", "default": "true", "desc": "true 时插卡分类完成后自动通过微信/QQ发送选图链接给已匹配客户"},
        ],
    },
    {
        "id": "ai",
        "title": "AI 模型与生图接口",
        "icon": "🧠",
        "description": "配置 DeepSeek 群聊拓客/私聊接单大脑、Gemini 多模态仿色引擎及局部去路人生图 API。",
        "test_action": "test-ai",
        "test_label": "真实 Ping 测试 DeepSeek / Gemini Key 有效性",
        "fields": [
            {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API Key", "default": "", "desc": "用于群聊意向评分与私聊自动接单对话"},
            {"key": "DEEPSEEK_BASE_URL", "label": "DeepSeek 接口基址", "default": "https://api.deepseek.com", "desc": "OpenAI 兼容接口根地址；填中转站地址即可走中转"},
            {"key": "VISION_BASE_URL", "label": "视觉大脑中转站地址", "default": "", "desc": "留空 = 走 Google 官方接口；填了 = 按 OpenAI 兼容协议走中转站（仿色优先使用该通道）"},
            {"key": "VISION_API_KEY", "label": "视觉大脑中转站 Key", "default": "", "desc": "中转站鉴权密钥；留空时自动回退到 Gemini Key"},
            {"key": "VISION_MODEL", "label": "视觉大脑主模型", "default": "", "desc": "中转站上的模型名（如 gpt-6.1-terra）；留空时回退到 Gemini 仿色模型"},
            {"key": "VISION_FALLBACK_MODELS", "label": "视觉大脑备用模型", "default": "", "desc": "逗号分隔，主模型失败时按顺序重试，例：gpt-6.1-sol,gpt-5.6-terra"},
            {"key": "GEMINI_API_KEY", "label": "Gemini API Key", "default": "", "desc": "用于读取原图+参考图+Skill 生成仿色 JSON 方案；未配中转站时走 Google 官方接口"},
            {"key": "GEMINI_RETOUCH_MODEL", "label": "Gemini 仿色模型", "default": "gemini-2.5-pro", "desc": "推荐 gemini-2.5-pro 或 gemini-2.5-flash；仅在未配中转站时生效"},
            {"key": "IMAGE_GEN_API_URL", "label": "局部修补/去路人生图 API", "default": "", "desc": "可选：仅裁切局部 Patch 修补杂物，严禁整图重绘"},
            {"key": "IMAGE_GEN_API_KEY", "label": "局部生图 API Key", "default": "", "desc": "局部修补接口鉴权密钥（可选）"},
        ],
    },
    {
        "id": "im",
        "title": "微信与 QQ 通道",
        "icon": "💬",
        "description": "配置 QQ NapCatQQ (OneBot v11)、PC 微信 wxauto/wxauto4 及企业微信客服连接参数与防封演练开关。",
        "test_action": "test-im",
        "test_label": "测试 NapCat / 本机微信状态",
        "fields": [
            {"key": "IM_GATEWAY_URL", "label": "IM 网关内网地址", "default": "http://127.0.0.1:8090", "desc": "仅限 127.0.0.1 内部通信，严禁公网暴露"},
            {"key": "NAPCAT_WS_URL", "label": "NapCatQQ WebSocket 地址", "default": "ws://127.0.0.1:3001", "desc": "NapCatQQ OneBot v11 正向 WS 监听地址"},
            {"key": "NAPCAT_HTTP_URL", "label": "NapCatQQ HTTP API 地址", "default": "http://127.0.0.1:3002", "desc": "NapCatQQ HTTP 接口（默认 3002，避开 booking-site 3000）"},
            {"key": "NAPCAT_TOKEN", "label": "NapCatQQ Access Token", "default": "", "desc": "若 NapCatQQ 配置了鉴权 Token 请填入"},
            {"key": "WECHAT_MODE", "label": "微信运行模式", "default": "desktop", "desc": "desktop（PC 微信 wxauto/wxauto4）或 web（预留）"},
            {"key": "DRY_RUN_ADD_FRIEND", "label": "防封安全演练开关", "default": "true", "desc": "true 时仅记录队列不真正发送加好友请求"},
            {"key": "WECOM_CORP_ID", "label": "企业微信 CorpID", "default": "", "desc": "企业微信客服企业 ID（可选）"},
            {"key": "WECOM_KF_SECRET", "label": "企微客服 Secret", "default": "", "desc": "企业微信客服应用密钥"},
            {"key": "WECOM_KF_TOKEN", "label": "企微回调 Token", "default": "", "desc": "回调签名验证 Token"},
            {"key": "WECOM_KF_ENCODING_AES_KEY", "label": "企微回调 EncodingAESKey", "default": "", "desc": "消息加解密 AES 密钥"},
            {"key": "WECOM_OPEN_KFID", "label": "企微客服账号 OpenKfId", "default": "", "desc": "默认接待客服账号 ID"},
        ],
    },
    {
        "id": "retouch",
        "title": "XMP 预设与 Photoshop 桥接",
        "icon": "🎨",
        "description": "配置客户选图预览 XMP、正式修图 XMP 预设目录，以及本机 Photoshop UXP 桥接监听端口。",
        "test_action": "test-ps",
        "test_label": "测试 PS UXP Bridge 连通",
        "fields": [
            {"key": "PREVIEW_XMP_PATH", "label": "选图预览 XMP 路径", "default": "", "desc": "拷卡生成客户选图 WebP 时套用的基础 XMP（可在修图面板直接拖拽上传）"},
            {"key": "RETOUCH_XMP_DIR", "label": "正式修图 XMP 目录", "default": r"D:\zero-studio-automation\configs\retouch_xmp", "desc": "与预览 XMP 严格分离，存放正式 16-bit 精修预设"},
            {"key": "PS_BACKEND", "label": "Photoshop 驱动模式", "default": "auto", "desc": "auto（Windows 自动 COM 零干预启停 PS，推荐）/ com / uxp"},
            {"key": "PS_EXE_PATH", "label": "Photoshop.exe 路径（可选）", "default": "", "desc": "留空自动探测本机最新版 Adobe Photoshop"},
            {"key": "PS_AUTO_CLOSE", "label": "修完空闲自动关闭 PS", "default": "true", "desc": "true 时本引擎自动拉起的 PS 在空闲后自动退出，不误关你手动打开的 PS"},
            {"key": "PS_IDLE_CLOSE_SEC", "label": "PS 空闲自动关闭秒数", "default": "60", "desc": "队列空闲超过该秒数后自动关闭 PS（默认 60 秒）"},
            {"key": "PS_UXP_BRIDGE_URL", "label": "PS UXP Bridge 地址", "default": "http://127.0.0.1:8765", "desc": "仅在 PS_BACKEND=uxp 时使用的 UXP 插件监听地址"},
            {"key": "PS_UXP_BRIDGE_TOKEN", "label": "PS UXP Bridge Token", "default": "", "desc": "Photoshop 桥接鉴权密钥"},
        ],
    },
    {
        "id": "tunnel",
        "title": "外网穿透与公网地址",
        "icon": "🌐",
        "description": "配置公网 VPS 穿透节点与 FRP/XTCP 隧道端口，保障客户手机选图与摄影师外网访问。",
        "test_action": "test-tunnel",
        "test_label": "测试外网 8080/8081 入口",
        "fields": [
            {"key": "PUBLIC_BASE_URL", "label": "客户外网选图根地址", "default": "http://156.238.228.39:8080", "desc": "自动拼装 /gallery/{token} 与 /delivery/{token} 链接"},
            {"key": "VPS_HOST", "label": "公网 VPS IP / 域名", "default": "156.238.228.39", "desc": "部署了 frps 的公网节点地址"},
            {"key": "VPS_SSH_PORT", "label": "VPS SSH 端口", "default": "24877", "desc": "部署脚本连接 VPS 使用的 SSH 端口"},
            {"key": "VPS_SSH_USER", "label": "VPS SSH 用户名", "default": "root", "desc": "部署脚本使用的 SSH 登录用户"},
            {"key": "FRP_BIND_PORT", "label": "FRP 服务端绑定端口", "default": "7000", "desc": "frps 监听的 TCP/UDP 穿透端口"},
            {"key": "FRP_TOKEN", "label": "FRP 鉴权 Token", "default": "", "desc": "本地 frpc 与远端 frps 一致的强密钥"},
            {"key": "FRP_PUBLIC_BOOKING_PORT", "label": "外网选图映射端口", "default": "8080", "desc": "映射本地 booking-site (3000) 的公网端口"},
            {"key": "FRP_PUBLIC_STUDIO_PORT", "label": "外网中控台映射端口", "default": "8081", "desc": "映射本地 control-center (3080) 的公网端口"},
            {"key": "FRP_XTCP_SECRET", "label": "XTCP P2P 直连密钥", "default": "", "desc": "外网电脑点对点直连本机的 XTCP 密钥"},
        ],
    },
    {
        "id": "security",
        "title": "安全与系统通知",
        "icon": "🔐",
        "description": "管理外网登录账密、内部微服务鉴权密钥、Cookie 签名密钥与 PushPlus 通知；保存后可一键重载服务。",
        "test_action": "reload-services",
        "test_label": "保存全部配置并重载服务",
        "fields": [
            {"key": "ADMIN_USER", "label": "管理员登录账号", "default": "admin", "desc": "中控台与预约后台统一登录用户名"},
            {"key": "ADMIN_PASSWORD", "label": "管理员登录密码", "default": "", "desc": "暴露公网时务必使用高强度复杂密码"},
            {"key": "INTERNAL_API_KEY", "label": "内网服务通信密钥", "default": "", "desc": "booking-site / im-gateway 内部接口 X-API-Key"},
            {"key": "CONTROL_CENTER_SECRET", "label": "Session Cookie 签名密钥", "default": "", "desc": "用于签发中控台 HttpOnly 登录态 Cookie"},
            {"key": "PUSHPLUS_TOKEN", "label": "PushPlus 微信推送 Token", "default": "", "desc": "可选：新订单与系统告警微信推送"},
        ],
    },
]


# ==================== 配置读取、脱敏与安全落盘 ====================

def read_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def get_runtime_config(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """合并 .env.example -> .env -> 当前进程环境变量 -> 显式注入覆盖。"""
    cfg = read_env_file(ENV_EXAMPLE_PATH)
    cfg.update(read_env_file(ENV_PATH))
    for k in list(cfg.keys()) + [
        "ADMIN_USER", "ADMIN_PASSWORD", "INTERNAL_API_KEY", "BOOKING_API_URL",
        "IM_GATEWAY_URL", "PS_UXP_BRIDGE_URL", "PS_UXP_BRIDGE_TOKEN",
        "PUBLIC_BASE_URL", "VPS_HOST", "VPS_SSH_PORT", "VPS_SSH_USER",
        "FRP_BIND_PORT", "FRP_PUBLIC_BOOKING_PORT", "FRP_PUBLIC_STUDIO_PORT",
        "NAS_UNC_PATH", "NAS_DRIVE_PATH", "CONTROL_CENTER_SECRET",
        "DRY_RUN_ADD_FRIEND", "PREVIEW_XMP_PATH", "RETOUCH_XMP_DIR",
    ]:
        ev = os.getenv(k)
        if ev is not None:
            cfg[k] = ev
    if overrides:
        cfg.update(overrides)
    return cfg


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_PATTERN.search(key))


def mask_secret_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "******"
    return f"{value[:2]}{'*' * min(10, len(value) - 4)}{value[-2:]}"


def write_env_updates(env_file: Path, updates: Dict[str, str]) -> Dict[str, str]:
    """安全更新 .env：保留注释与顺序，跳过未改动的脱敏占位符，写后原子替换。"""
    if not env_file.exists() and ENV_EXAMPLE_PATH.exists():
        env_file.write_text(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    applied: Dict[str, str] = {}
    remaining = {}

    for k, raw_val in updates.items():
        key = str(k).strip()
        if not re.match(r"^[A-Z0-9_]+$", key):
            continue
        val = str(raw_val if raw_val is not None else "").strip()
        # 脱敏值（含连续 ****）不覆盖已有明文密钥
        if is_secret_key(key) and "****" in val:
            continue
        remaining[key] = val

    new_lines: List[str] = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in remaining:
            new_lines.append(f"{k}={remaining[k]}")
            applied[k] = remaining[k]
            os.environ[k] = remaining[k]
            seen.add(k)
        else:
            new_lines.append(line)

    for k, v in remaining.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
            applied[k] = v
            os.environ[k] = v

    tmp_path = env_file.with_name(env_file.name + ".tmp")
    tmp_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, env_file)
    return applied


# ==================== Signed HttpOnly Session Cookie ====================

class SessionSigner:
    def __init__(self, secret: str):
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def create_token(self, username: str, ttl_sec: int = SESSION_TTL_SEC) -> str:
        payload = {"u": username, "exp": int(time.time()) + ttl_sec}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sig = hmac.new(self._key, b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{b64}.{sig}"

    def verify_token(self, token: Optional[str]) -> Optional[str]:
        if not token or "." not in token:
            return None
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(self._key, b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            padded = b64 + "=" * (-len(b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if int(payload.get("exp", 0)) < int(time.time()):
                return None
            return str(payload.get("u") or "")
        except Exception:
            return None


# ==================== 后台任务（拷卡/聚类/修图 Worker）实时日志管理 ====================

class BackgroundJobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def start_sequence(self, name: str, steps: List[Tuple[str, List[str], Path]]) -> str:
        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id,
            "name": name,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "logs": [f"[SYSTEM] 任务 {name} ({job_id}) 已启动"],
            "exit_code": None,
        }
        with self._lock:
            self._jobs[job_id] = job

        def _runner() -> None:
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            final_code = 0
            manifest_path_found: Optional[str] = None
            try:
                for step_label, cmd_template, cwd in steps:
                    cmd = [
                        c.replace("__MANIFEST_PATH__", manifest_path_found or "")
                        for c in cmd_template
                    ]
                    if "__MANIFEST_PATH__" in " ".join(cmd_template) and not manifest_path_found:
                        self._append(job_id, f"[ERROR] 步骤 {step_label} 缺少上一步生成的 manifest_path，已中止")
                        final_code = 1
                        break

                    self._append(job_id, f"--- 开始步骤: {step_label} ---")
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(cwd),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        clean = line.rstrip("\r\n")
                        self._append(job_id, clean)
                        # 捕获 ingest.py 输出的 JSON manifest_path
                        if '"manifest_path"' in clean:
                            try:
                                parsed = json.loads(clean)
                                if parsed.get("manifest_path"):
                                    manifest_path_found = str(parsed["manifest_path"])
                            except Exception:
                                pass
                    code = proc.wait()
                    if code != 0:
                        final_code = code
                        self._append(job_id, f"[ERROR] 步骤 {step_label} 失败 (exit={code})")
                        break
                    self._append(job_id, f"[OK] 步骤 {step_label} 完成")
            except Exception as exc:
                final_code = 1
                self._append(job_id, f"[EXCEPTION] {exc}")

            with self._lock:
                self._jobs[job_id]["exit_code"] = final_code
                self._jobs[job_id]["status"] = "done" if final_code == 0 else "failed"
                self._jobs[job_id]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        threading.Thread(target=_runner, daemon=True).start()
        return job_id

    def _append(self, job_id: str, line: str) -> None:
        with self._lock:
            logs = self._jobs[job_id]["logs"]
            logs.append(line)
            if len(logs) > 600:
                del logs[: len(logs) - 600]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(job_id)
            return {**j, "logs": list(j["logs"])} if j else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": j["id"],
                    "name": j["name"],
                    "status": j["status"],
                    "started_at": j["started_at"],
                    "finished_at": j["finished_at"],
                    "log_count": len(j["logs"]),
                }
                for j in reversed(list(self._jobs.values()))
            ][:20]


# ==================== 系统健康探针工具 ====================

def tcp_probe(host: str, port: int, timeout: float = 1.2) -> bool:
    if not host or port <= 0:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_gpu_status() -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 4:
                return {
                    "online": True,
                    "name": parts[0],
                    "vram_used_mb": int(float(parts[1])),
                    "vram_total_mb": int(float(parts[2])),
                    "utilization_pct": int(float(parts[3])),
                }
    except Exception:
        pass
    return {"online": False, "name": "CPU / 未检测到 NVIDIA GPU"}


_nas_status_cache: Dict[str, Any] = {
    "online": False,
    "active_mode": "OFFLINE",
    "path": "",
    "free_gb": 0,
    "total_gb": 0,
    "used_pct": 0,
}
_nas_cache_updated_at: float = 0.0
_nas_probe_running: bool = False
_nas_lock = threading.Lock()


def _smb_host_reachable(host: str, port: int = 445, timeout: float = 1.2) -> bool:
    """SMB 端口可达性探测（仅供参考，不用于判定 NAS 是否可用）。

    本机若装了代理软件（Clash TUN / 重定向），连不可达的 445 也会"成功"，
    得到假阳性，因此不能作为判定依据。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _nas_target_quick_ok(raw: str) -> bool:
    """在真正访问文件系统之前，先用极低成本的检查排除必然失败的路径。

    注意：不能用 socket 探测 445 端口判断 SMB 可达性 —— 本机装了代理软件
    （如 Clash 的 TUN/重定向）时，连 445 也会被"成功"接住，得到假阳性
    （实测 192.168.1.100:445 秒连成功，但 Path.exists() 报 WinError 64）。
    因此这里只做本地盘符状态判断，网络路径交给带硬超时的线程探测。
    """
    if os.name == "nt" and re.match(r"^[A-Za-z]:", raw):
        try:
            letter = f"{raw[0].upper()}:\\"
            # DRIVE_NO_ROOT_DIR(1) = 该盘符当前不存在（映射盘断开时就是这个状态）
            if ctypes.windll.kernel32.GetDriveTypeW(letter) == 1:
                return False
        except Exception:
            pass
    return True


NAS_PROBE_HARD_TIMEOUT_SEC = 6.0


def _probe_path_online(raw: str) -> bool:
    """实际访问路径判断是否在线（可能阻塞 16~20 秒，必须放在带超时的线程里调用）。"""
    p = Path(raw)
    try:
        check_target = p if p.exists() else (p.parent if p.parent.exists() else None)
        if check_target is None:
            return False
        usage = shutil.disk_usage(str(check_target))
        return usage.total > 0
    except Exception:
        return False


def _quick_disk_usage(raw: str) -> Optional[Dict[str, Any]]:
    t0 = time.time()
    p = Path(raw)
    check_target = p if p.exists() else (p.parent if p.parent.exists() else None)
    if check_target is None:
        return None
    usage = shutil.disk_usage(str(check_target))
    return {
        "free_gb": round(usage.free / (1024**3), 2),
        "total_gb": round(usage.total / (1024**3), 2),
        "used_pct": round(usage.used / max(usage.total, 1) * 100, 1),
        "elapsed": time.time() - t0,
    }


def _do_nas_probe(cfg: Dict[str, str]) -> Dict[str, Any]:
    global _nas_status_cache, _nas_cache_updated_at
    result = {
        "online": False,
        "active_mode": "OFFLINE",
        "path": cfg.get("NAS_UNC_PATH") or cfg.get("NAS_DRIVE_PATH") or "",
        "free_gb": 0,
        "total_gb": 0,
        "used_pct": 0,
    }
    for label, key in (("UNC", "NAS_UNC_PATH"), ("DRIVE", "NAS_DRIVE_PATH")):
        raw = (cfg.get(key) or "").strip()
        if not raw:
            continue
        # 本地盘符先做毫秒级的"是否存在"判断，能立刻排除断开的映射盘
        if not _nas_target_quick_ok(raw):
            continue
        # 真实文件系统访问可能阻塞十几秒，放进独立线程并设硬超时，绝不拖死探测
        box: Dict[str, Any] = {}
        done = threading.Event()

        def _worker(target_raw: str = raw) -> None:
            try:
                box["usage"] = _quick_disk_usage(target_raw)
            except Exception:
                box["usage"] = None
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        if not done.wait(NAS_PROBE_HARD_TIMEOUT_SEC):
            logger.warning("NAS 探测超时（%.0fs），判定为不可达: %s", NAS_PROBE_HARD_TIMEOUT_SEC, raw)
            continue
        usage = box.get("usage")
        if usage and usage.get("total_gb", 0) > 0:
            result = {
                "online": True,
                "active_mode": label,
                "path": raw,
                "free_gb": usage["free_gb"],
                "total_gb": usage["total_gb"],
                "used_pct": usage["used_pct"],
            }
            break
    _nas_status_cache = result
    _nas_cache_updated_at = time.time()
    return result


# NAS 探测在 SMB 主机不可达时会阻塞数十秒（Windows 连接超时），
# 绝不能让它卡住任何页面请求：首次探测放到后台线程，接口立即返回"探测中"。
NAS_PROBE_FIRST_WAIT_SEC = 1.5


def probe_nas_status(cfg: Dict[str, str]) -> Dict[str, Any]:
    """非阻塞获取 NAS 状态。

    历史实现首次调用会同步探测，当 UNC 主机不可达时整个页面要卡几十秒
    （实测 \\\\192.168.1.100 不可达时阻塞 57 秒），表现为"面板一片空白"。
    现在：首次探测放到后台线程，最多只等 NAS_PROBE_FIRST_WAIT_SEC 秒；
    等不到就如实返回 OFFLINE + probing=True，由前端提示"正在探测"。
    """
    global _nas_status_cache, _nas_cache_updated_at, _nas_probe_running
    now = time.time()

    def _spawn_probe() -> None:
        global _nas_probe_running
        _nas_probe_running = True

        def _worker() -> None:
            global _nas_probe_running
            try:
                _do_nas_probe(cfg)
            finally:
                _nas_probe_running = False

        threading.Thread(target=_worker, daemon=True).start()

    first_time = _nas_cache_updated_at == 0.0
    if first_time and not _nas_probe_running:
        with _nas_lock:
            if not _nas_probe_running and _nas_cache_updated_at == 0.0:
                _spawn_probe()
        # 只做极短暂的等待，给本地/局域网 NAS 一个即刻命中的机会
        deadline = time.time() + NAS_PROBE_FIRST_WAIT_SEC
        while time.time() < deadline and _nas_cache_updated_at == 0.0:
            time.sleep(0.05)
    elif (now - _nas_cache_updated_at > 60) and not _nas_probe_running:
        with _nas_lock:
            if not _nas_probe_running:
                _spawn_probe()

    res = dict(_nas_status_cache)
    if not res["path"]:
        res["path"] = cfg.get("NAS_UNC_PATH") or cfg.get("NAS_DRIVE_PATH") or ""
    res["probing"] = bool(_nas_probe_running) or _nas_cache_updated_at == 0.0
    return res


def is_frpc_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if "frpc" in (p.info["name"] or "").lower():
                return True
        return False
    except Exception:
        pass
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq frpc.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return "frpc.exe" in (proc.stdout or "").lower()
    except Exception:
        return False


def _is_lan_host(host: str) -> bool:
    """判断是否为局域网/本机地址（决定 NapCat 扫码入口该用内网还是公网地址）。"""
    import ipaddress

    h = (host or "").strip().lower().strip("[]")
    if h in ("localhost", "::1", ""):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # 形如 zero-2.local / 主机名，视为内网
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local


_NAPCAT_TOKEN_CACHE: Dict[str, Any] = {"token": "", "at": 0.0}


def _read_napcat_webui_token(force: bool = False) -> str:
    """从 NapCat 容器内的 webui.json 读取 WebUI token（缓存 60 秒）。

    NapCat 会把 token 写在容器里的 /app/napcat/config/webui.json，
    写死在页面上的旧 token 一旦与它不一致，打开 WebUI 就会提示密码错误。
    """
    now = time.time()
    if not force and _NAPCAT_TOKEN_CACHE["token"] and now - _NAPCAT_TOKEN_CACHE["at"] < 60:
        return str(_NAPCAT_TOKEN_CACHE["token"])

    docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
    candidates = [
        [docker, "exec", "zero-napcat", "cat", "/app/napcat/config/webui.json"],
    ]
    # 共享目录挂载的部署方式（docker 卷挂载在宿主机也能读到）
    candidates.append(["/bin/sh", "-c", f"cat {Path.home()}/zero-studio-napcat/config/webui.json"])
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
            if proc.returncode != 0 or not (proc.stdout or "").strip():
                continue
            data = json.loads(proc.stdout)
            token = str(data.get("token") or "").strip()
            if token:
                _NAPCAT_TOKEN_CACHE.update({"token": token, "at": now})
                return token
        except Exception:
            continue
    _NAPCAT_TOKEN_CACHE.update({"token": "", "at": now})
    return ""


def detect_windows_removable_drives() -> List[Dict[str, str]]:
    if os.name != "nt":
        return []
    drives: List[Dict[str, str]] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            d = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(d) == DRIVE_REMOVABLE:
                dcim = Path(d) / "DCIM"
                drives.append({
                    "drive": d,
                    "recommended_source": str(dcim) if dcim.exists() else d,
                    "has_dcim": dcim.exists(),
                })
    return drives


def diagnose_source_dir(raw: str) -> Dict[str, Any]:
    """
    精确诊断"源目录读取不到"的真实原因。

    旧实现只用 Path.exists() 判断，网络/映射盘断开、权限不足、路径写错
    全都笼统报"源目录不存在"，摄影师根本不知道卡在哪一步。
    这里逐项区分：未填 / 不存在 / 是文件而非目录 / 无权限 / 目录里没有照片。
    """
    raw = (raw or "").strip().strip('"')
    report: Dict[str, Any] = {
        "raw": raw,
        "exists": False,
        "is_dir": False,
        "readable": False,
        "image_count": 0,
        "error": "",
        "hint": "",
    }
    if not raw:
        report["error"] = "源目录未填写"
        report["hint"] = "请选择 SD 卡 / 读卡器，或填入照片所在目录。"
        return report

    p = Path(raw)
    try:
        exists = p.exists()
    except OSError as exc:
        exists = False
        report["error"] = f"路径无法访问: {exc}"
    report["exists"] = exists
    if not exists:
        # 区分"盘符不存在"和"目录不存在"，给出可操作的提示
        report["error"] = f"源目录不存在或不可访问: {raw}"
        if raw.startswith("\\\\"):
            report["hint"] = "网络路径不可达：请确认 NAS 已开机、与本机在同一网段，且共享名拼写正确。"
        elif re.match(r"^[A-Za-z]:", raw):
            letter = raw[0].upper()
            report["hint"] = (
                f"盘符 {letter}: 不存在或未连接。"
                "如果是 SD 卡：请确认读卡器已插好、卡已插入且资源管理器里能看到该盘；"
                "如果是网络映射盘（例如 NAS 的 Z:）：请在资源管理器里重新连接该网络位置。"
            )
        else:
            report["hint"] = "请检查路径拼写，或直接点「选择本机文件夹」。"
        return report

    report["is_dir"] = p.is_dir()
    if not report["is_dir"]:
        report["error"] = f"这是一个文件，不是目录: {raw}"
        report["hint"] = "请选择照片所在的文件夹，或把该文件的上级目录填进来。"
        return report

    # 真正尝试列一次目录，才能发现权限问题（exists() 对无权限目录也可能返回 True）
    try:
        probe = next(iter(os.scandir(raw)), None)
        report["readable"] = True
        del probe
    except PermissionError:
        report["error"] = f"无权限读取该目录: {raw}"
        report["hint"] = "请以管理员身份运行，或检查该网络共享的访问权限。"
        return report
    except OSError as exc:
        report["error"] = f"目录读取失败: {exc}"
        report["hint"] = "网络盘可能已断开，请重新连接后再试。"
        return report

    # 与 ingest.py 的 scan_media_files 保持一致：递归扫描，只认 .arw/.cr3/.jpg/.jpeg
    auto_exts = {".arw", ".cr3", ".jpg", ".jpeg"}
    skip_dirs = {"system volume information", "$recycle.bin", ".trashes", ".spotlight-v100", "__macosx"}
    try:
        for root, dirs, files in os.walk(raw):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in skip_dirs]
            for fname in files:
                if fname.startswith("._") or fname.startswith("."):
                    continue
                if Path(fname).suffix.lower() in auto_exts:
                    report["image_count"] += 1
    except OSError:
        pass

    if report["image_count"] == 0:
        report["error"] = f"目录里没有可识别的照片: {raw}"
        report["hint"] = (
            "自动拷卡只读取 .ARW / .CR3 / .JPG / .JPEG（会递归子文件夹）。"
            "如果卡里确实有照片，请确认选的是 SD 卡根目录或 DCIM；"
            "其它格式（PNG/WebP/TIFF）请改用下方「手动导入」。"
        )
    return report


# ==================== 插卡拷卡：手动导入 / 手动分类分人 工具函数 ====================

def _sanitize_path_token(value: str, fallback: str = "UNK", max_len: int = 24) -> str:
    """把活动名/人名清洗为可安全用于批次 ID 与目录名的 token。

    与 ingest.py 不同：这里保留中文，因为手动导入的批次 ID 会直接展示给摄影师，
    "20261121__MANUAL_小雪_BACC" 比哈希码可读得多。仅剔除 Windows/macOS 非法字符。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "", (value or "").strip())
    cleaned = cleaned.strip(".")  # 目录名不能以点结尾
    return cleaned[:max_len] or fallback


def _unique_manual_batch_id(attendee: str, event_name: str) -> str:
    """手动导入批次 ID：手工预览日期__人__活动 （同一人多场次清晰可区分）。"""
    day = time.strftime("%Y%m%d")
    person = _sanitize_path_token(attendee, fallback="NOBODY", max_len=16)
    event = _sanitize_path_token(event_name or "MANUAL", fallback="MANUAL", max_len=20)
    batch_id = f"{day}__MANUAL_{person}_{event}"
    # 极端情况下同秒重复导入：追加短哈希，绝不覆盖已有批次
    return f"{batch_id}_{uuid.uuid4().hex[:4].upper()}"


def _extract_embedded_jpeg_from_raw(raw: bytes) -> Optional[bytes]:
    """从 .CR3 / .ARW 字节流中快速提取最大内嵌 JPEG 预览图（纯 Python 零依赖兜底）。"""
    best: Optional[bytes] = None
    best_len = 0
    pos = 0
    n = len(raw)
    while pos < n:
        start = raw.find(b"\xff\xd8\xff", pos)
        if start < 0:
            break
        end = raw.find(b"\xff\xd9", start + 3)
        if end < 0:
            break
        length = end + 2 - start
        if length > best_len and length > 8192:
            best_len = length
            best = raw[start : end + 2]
        pos = end + 2
    return best


def _save_pil_as_webp(img: Any, target_path: Path, max_edge: int) -> None:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    longest = max(img.size)
    if longest > max_edge:
        ratio = max_edge / float(longest)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size, Image.LANCZOS)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(target_path, format="WEBP", quality=86, method=4)


def _write_webp_preview_from_bytes(
    raw: bytes,
    target_path: Path,
    max_edge: int = MANUAL_IMPORT_PREVIEW_EDGE,
    source_path: Optional[Path] = None,
) -> str:
    """
    为手动导入的图片生成客户选图 WebP 预览图。
    支持普通图片（JPG/PNG/WebP/TIFF/BMP）及相机 RAW（Canon .CR3 / Sony .ARW）：
    依次尝试 Pillow -> macOS 原生 sips -> exiftool 内嵌预览 -> rawpy -> 纯 Python 内嵌 JPEG 提取。
    """
    try:
        from PIL import Image
    except ImportError:
        return "skipped:no-pillow"

    # 1. 普通图片直接用 Pillow 解码
    try:
        with Image.open(io.BytesIO(raw)) as img:
            _save_pil_as_webp(img, target_path, max_edge)
        return "pillow"
    except Exception:
        pass

    # 2. macOS 原生 sips（对 Canon R5 .CR3 / Sony .ARW 零依赖原生支持）
    if sys.platform == "darwin" and source_path and source_path.exists():
        tmp_jpg = target_path.with_suffix(".sips.jpg")
        try:
            proc = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", str(max_edge), str(source_path), "--out", str(tmp_jpg)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and tmp_jpg.exists() and tmp_jpg.stat().st_size > 1024:
                with Image.open(tmp_jpg) as img:
                    _save_pil_as_webp(img, target_path, max_edge)
                return "macos-sips"
        except Exception:
            pass
        finally:
            try:
                tmp_jpg.unlink(missing_ok=True)
            except OSError:
                pass

    # 3. exiftool 提取 RAW 内嵌高精度预览图
    exiftool_bin = shutil.which("exiftool")
    if exiftool_bin and source_path and source_path.exists():
        try:
            proc = subprocess.run(
                [exiftool_bin, "-b", "-PreviewImage", str(source_path)],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0 and len(proc.stdout) > 8192:
                with Image.open(io.BytesIO(proc.stdout)) as img:
                    _save_pil_as_webp(img, target_path, max_edge)
                return "exiftool-preview"
        except Exception:
            pass

    # 4. rawpy 解码（若已安装）
    try:
        import rawpy  # type: ignore

        with rawpy.imread(io.BytesIO(raw)) as rp:
            rgb = rp.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
        with Image.fromarray(rgb, mode="RGB") as img:
            _save_pil_as_webp(img, target_path, max_edge)
        return "rawpy"
    except Exception:
        pass

    # 5. 纯 Python 零依赖提取 CR3/ARW 内嵌 JPEG 预览流
    try:
        emb = _extract_embedded_jpeg_from_raw(raw)
        if emb:
            with Image.open(io.BytesIO(emb)) as img:
                _save_pil_as_webp(img, target_path, max_edge)
            return "embedded-jpeg"
    except Exception:
        pass

    return "skipped"


def _extract_manual_captured_at(raw: bytes) -> Optional[str]:
    """尽力读取拍摄时间（EXIF DateTimeOriginal）；读不到返回 None，由数据库按导入时间兜底。"""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            exif = img.getexif()
            if not exif:
                return None
            # 拍摄时间也可能只存在于 EXIF 子 IFD（ExifOffset）中，需要再探一层
            try:
                exif_ifd = exif.get_ifd(0x8769) or {}
            except Exception:
                exif_ifd = {}
            candidates = list(exif_ifd.items()) + list(exif.items())
            for tag_id, val in candidates:
                tag_name = TAGS.get(tag_id)
                if tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime") and isinstance(val, str):
                    raw_val = val.replace("\x00", "").strip()
                    # EXIF 格式 "2026:11:20 10:05:00" -> "2026-11-20 10:05:00"
                    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw_val)
                    if m:
                        return "{}-{}-{} {}:{}:{}".format(*m.groups())
    except Exception:
        return None
    return None


def _iter_manual_import_files(items: List[str]) -> List[Path]:
    """把手动导入的路径列表展开成图片文件列表（目录递归展开；跳过系统垃圾目录）。"""
    skip_names = {"system volume information", "$recycle.bin", ".trashes", ".spotlight-v100", "__macosx"}
    result: List[Path] = []
    for item in items:
        p = Path(str(item).strip().strip('"'))
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in MANUAL_IMPORT_EXTENSIONS:
                    if any(part.lower() in skip_names for part in child.parts):
                        continue
                    result.append(child)
        elif p.is_file() and p.suffix.lower() in MANUAL_IMPORT_EXTENSIONS:
            result.append(p)
    # 去重并保持顺序
    seen = set()
    unique: List[Path] = []
    for f in result:
        key = str(f.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    if len(unique) > MANUAL_IMPORT_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单次手动导入最多 {MANUAL_IMPORT_MAX_FILES} 张，当前 {len(unique)} 张，请分批导入",
        )
    return unique


# ==================== Pydantic 请求模型 ====================

class LoginRequest(BaseModel):
    username: str
    password: str


class IngestStartRequest(BaseModel):
    source_dir: str
    event_name: str
    venue: str = ""
    preview_xmp_path: str = ""
    run_cluster: bool = True


class BindClusterRequest(BaseModel):
    batch_id: str
    cluster_id: str = ""
    booking_id: int
    push_gallery: bool = False


class CheckSourceRequest(BaseModel):
    source_dir: str


class ManualImportItem(BaseModel):
    """暂存导入项：浏览器上传时带 token；本机路径导入时带 path（二选一）。"""

    token: str = ""
    path: str = ""
    name: str = ""


class ManualImportRequest(BaseModel):
    """插卡拷卡面板：手动导入（服务器本地路径 / 拖拽上传落盘路径）。

    指定 attendee + booking_id 时属于"手动分类"：导入即归属到该客户；
    不指定时仅入库，之后在素材网格里逐张/批量分人。
    """

    sources: List[ManualImportItem] = []
    file_paths: List[str] = []
    attendee: str = ""
    booking_id: Optional[int] = None
    event_name: str = ""
    create_booking: bool = False
    character: str = ""
    shoot_date: str = ""


class ImportUploadItem(BaseModel):
    token: str
    name: str = ""


class BindAssetsRequest(BaseModel):
    asset_ids: List[int] = []
    booking_id: Optional[int] = None
    mode: str = "replace"  # replace=换人 / add=合影加人 / remove=取消归属


class CreateBookingForAssetsRequest(BaseModel):
    asset_ids: List[int] = []
    cn: str
    character: str = ""
    event_name: str = ""
    date: str = ""
    time: str = ""
    mode: str = "replace"


class PushGalleryRequest(BaseModel):
    booking_id: int
    platform: str
    user_id: str
    cn: str = ""
    character: str = ""
    event_name: str = ""
    gallery_token: str


class SkillSaveRequest(BaseModel):
    content: str


class EnvSaveRequest(BaseModel):
    updates: Dict[str, str]


# ==================== FastAPI 应用工厂 ====================

def create_app(
    config_overrides: Optional[Dict[str, str]] = None,
    env_file_path: Optional[Path] = None,
    skills_dir_path: Optional[Path] = None,
    presets_map_file: Optional[Path] = None,
    preview_xmp_dir: Optional[Path] = None,
    retouch_xmp_dir: Optional[Path] = None,
    retouch_refs_dir: Optional[Path] = None,
    sandbox_dir: Optional[Path] = None,
    im_rules_file: Optional[Path] = None,
    pid_file_path: Optional[Path] = None,
) -> FastAPI:
    active_env_path = Path(env_file_path or ENV_PATH)
    active_skills_dir = Path(skills_dir_path or SKILLS_DIR)
    active_presets_path = Path(presets_map_file or PRESETS_MAP_PATH)
    active_preview_xmp_dir = Path(preview_xmp_dir or PREVIEW_XMP_DIR)
    active_retouch_xmp_dir = Path(retouch_xmp_dir or RETOUCH_XMP_DIR)
    active_retouch_refs_dir = Path(retouch_refs_dir or RETOUCH_REFS_DIR)
    active_sandbox_dir = Path(sandbox_dir or SANDBOX_WORK_DIR)
    active_im_rules_path = Path(im_rules_file or IM_RULES_PATH)
    active_pid_file = Path(pid_file_path or STUDIO_PIDS_PATH)
    jobs = BackgroundJobManager()

    def _cfg() -> Dict[str, str]:
        base = read_env_file(ENV_EXAMPLE_PATH)
        for k in list(base.keys()) + [
            "ADMIN_USER", "ADMIN_PASSWORD", "INTERNAL_API_KEY", "BOOKING_API_URL",
            "IM_GATEWAY_URL", "IM_GATEWAY_PORT", "NAPCAT_HTTP_URL", "NAPCAT_WS_URL",
            "PS_UXP_BRIDGE_URL", "PUBLIC_BASE_URL",
            "VPS_HOST", "FRP_BIND_PORT", "FRP_PUBLIC_BOOKING_PORT", "FRP_PUBLIC_STUDIO_PORT",
            "CONTROL_CENTER_SECRET", "WECHAT_GATEWAY_URL",
        ]:
            ev = os.getenv(k)
            if ev is not None:
                base[k] = ev
        if active_env_path.exists():
            base.update(read_env_file(active_env_path))
        if config_overrides:
            base.update(config_overrides)
        return base

    def _signer() -> SessionSigner:
        c = _cfg()
        secret = c.get("CONTROL_CENTER_SECRET") or f"{c.get('INTERNAL_API_KEY', 'zs')}:{c.get('ADMIN_PASSWORD', 'adm')}"
        return SessionSigner(secret)

    def _wechat_gateway_url() -> str:
        """微信请求单独走 Windows 虚拟机 IM 网关（若配置），否则回退到本地 IM 网关。"""
        c = _cfg()
        return (c.get("WECHAT_GATEWAY_URL") or c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")

    def _get_current_user(request: Request) -> Optional[str]:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        return _signer().verify_token(token)

    def require_session(request: Request) -> str:
        user = _get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized: login required")
        return user

    app = FastAPI(title="ZERO STUDIO Control Center", version="5.0.0")

    # ==================== 页面路由（未登录强制跳 /login） ====================

    @app.get("/login")
    async def login_page(request: Request):
        if _get_current_user(request):
            return RedirectResponse(url="/", status_code=302)
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/")
    async def index_page(request: Request):
        if not _get_current_user(request):
            return RedirectResponse(url="/login", status_code=302)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/mobile")
    async def mobile_page(request: Request):
        if not _get_current_user(request):
            return RedirectResponse(url="/login?next=/mobile", status_code=302)
        return FileResponse(STATIC_DIR / "mobile.html")

    # ==================== 鉴权接口 ====================

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> Dict[str, Any]:
        user = _get_current_user(request)
        return {"authenticated": bool(user), "username": user}

    @app.post("/api/auth/login")
    async def auth_login(payload: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
        c = _cfg()
        expected_user = c.get("ADMIN_USER") or "admin"
        expected_pass = c.get("ADMIN_PASSWORD") or "change-me"
        ok_u = hmac.compare_digest(payload.username.strip(), expected_user)
        ok_p = hmac.compare_digest(payload.password, expected_pass)
        if not (ok_u and ok_p):
            raise HTTPException(status_code=401, detail="账号或密码错误")

        token = _signer().create_token(expected_user)
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_TTL_SEC,
            httponly=True,
            samesite="lax",
            secure=is_https,
            path="/",
        )
        return {"status": "ok", "username": expected_user}

    @app.post("/api/auth/logout")
    async def auth_logout(response: Response) -> Dict[str, Any]:
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        return {"status": "ok"}

    # ==================== 面板 1：总览、健康聚合与全链路订单看板 ====================

    @app.get("/api/overview/health", dependencies=[Depends(require_session)])
    async def overview_health() -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        ps_url = (c.get("PS_UXP_BRIDGE_URL") or "http://127.0.0.1:8765").rstrip("/")
        vps_host = (c.get("VPS_HOST") or "").strip()
        frp_booking_port = int(c.get("FRP_PUBLIC_BOOKING_PORT") or 8080)
        frp_studio_port = int(c.get("FRP_PUBLIC_STUDIO_PORT") or 8081)
        public_base = (c.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or (
            f"http://{vps_host}:{frp_booking_port}" if vps_host else booking_url
        )
        public_studio_url = f"http://{vps_host}:{frp_studio_port}" if vps_host else ""

        async def _http_ok(url: str) -> Dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                    r = await client.get(url)
                    return {"online": r.status_code < 500, "status_code": r.status_code, "url": url}
            except Exception as exc:
                return {"online": False, "error": str(exc), "url": url}

        booking_st = await _http_ok(f"{booking_url}/api/settings")
        gateway_st = await _http_ok(f"{gateway_url}/health")

        def _blocking_probes():
            ps_parsed = urlparse(ps_url)
            ps_online = tcp_probe(ps_parsed.hostname or "127.0.0.1", ps_parsed.port or 8765, timeout=0.3)
            nas_st = probe_nas_status(c)
            gpu_st = probe_gpu_status()
            frpc_proc = is_frpc_running()
            return {
                "ps_st": {"online": ps_online, "url": ps_url},
                "nas_st": nas_st,
                "gpu_st": gpu_st,
                "tunnel": {
                    "frpc_running": frpc_proc,
                    "vps_host": vps_host,
                    "booking_port_reachable": frpc_proc,
                    "studio_port_reachable": frpc_proc,
                    "online": frpc_proc,
                    "public_booking_url": public_base,
                    "public_studio_url": public_studio_url,
                    "p2p_visitor_port": 13080,
                },
            }

        probe_res = await asyncio.to_thread(_blocking_probes)

        return {
            "status": "ok",
            "services": {
                "booking_site": booking_st,
                "im_gateway": gateway_st,
                "ps_uxp_bridge": probe_res["ps_st"],
            },
            "nas": probe_res["nas_st"],
            "gpu": probe_res["gpu_st"],
            "tunnel": probe_res["tunnel"],
        }

    @app.get("/api/bookings", dependencies=[Depends(require_session)])
    async def list_bookings() -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        public_base = (c.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or booking_url
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.get(f"{booking_url}/api/bookings", auth=auth_pair)
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"booking-site 返回 {r.status_code}")
            rows = r.json()

        enriched = []
        for b in rows:
            token = b.get("gallery_token") or ""
            enriched.append(
                {
                    **b,
                    "status": b.get("status") or "BOOKED",
                    "public_gallery_url": f"{public_base}/gallery/{token}" if token else "",
                    "public_delivery_url": b.get("delivery_url") or (f"{public_base}/delivery/{token}" if token else ""),
                }
            )
        return {"status": "ok", "public_base_url": public_base, "bookings": enriched}

    @app.get("/api/bookings/{booking_id}/slate-qr", dependencies=[Depends(require_session)])
    async def proxy_slate_qr(booking_id: int) -> Response:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.get(
                f"{booking_url}/api/internal/bookings/{booking_id}/slate-qr",
                headers={"X-API-Key": key},
            )
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="获取场记二维码失败")
            return Response(content=r.content, media_type="image/png")

    # ==================== 面板 2：IM 拓客线索与冷却队列 ====================

    @app.get("/api/im/leads", dependencies=[Depends(require_session)])
    async def get_im_leads() -> Dict[str, Any]:
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                r = await client.get(f"{gateway_url}/api/gateway/leads", headers={"X-API-Key": key})
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {
            "status": "ok",
            "offline": True,
            "stats": {
                "dry_run": (c.get("DRY_RUN_ADD_FRIEND", "true").lower() in ("1", "true", "yes")),
                "hourly_used": 0,
                "max_per_hour": 3,
                "dedup_days": 30,
            },
            "leads": [],
        }

    @app.post("/api/im/dry-run", dependencies=[Depends(require_session)])
    async def toggle_im_dry_run(request: Request) -> Dict[str, Any]:
        body = await request.json()
        dry_run = bool(body.get("dry_run", True))
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""

        write_env_updates(active_env_path, {"DRY_RUN_ADD_FRIEND": "true" if dry_run else "false"})
        gw_synced = False
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                r = await client.post(
                    f"{gateway_url}/api/gateway/config/dry-run",
                    headers={"X-API-Key": key},
                    json={"dry_run": dry_run},
                )
                gw_synced = r.status_code == 200
        except Exception:
            pass
        return {"status": "ok", "dry_run": dry_run, "gateway_synced": gw_synced}

    @app.post("/api/im/leads/{queue_id}/action", dependencies=[Depends(require_session)])
    async def im_lead_action(queue_id: int, request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.post(
                f"{gateway_url}/api/gateway/leads/{queue_id}/action",
                headers={"X-API-Key": key},
                json={"action": body.get("action")},
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    # ---- Sprint 6: 微信 & QQ 真实连接工作台 & 群监控规则持久化 ----

    DEFAULT_PRIV_PROMPT = (
        "你是 ZERO STUDIO Cosplay 摄影工作室的私聊自动接单客服，语气热情、自然、二次元友好、简明高效。\n"
        "你需要一边自然回复客户的咨询（如报价、风格、档期等），一边通过多轮对话从客户消息中提取预约所需字段：\n"
        "- cn: 客户称呼 / CN\n"
        "- character: 拍摄角色或风格\n"
        "- event_name: 漫展或企划活动名称\n"
        "- date: 拍摄日期\n"
        "- time: 拍摄时段\n"
        "- remarks: 特殊要求或备注（可选）\n"
        "当仍有必填字段缺失时，结合对方问题自然回复并顺带追问缺项。"
    )

    def _read_local_im_rules() -> Dict[str, Any]:
        default_rules = {
            "qq_groups": [],
            "wechat_groups": [],
            "wechat_private_chats": [],
            "wechat_auto_reply_all_private": True,
            "private_only_reply_related": True,
            "wechat_private_blacklist": [],
            "keywords": ["约拍", "场照", "找摄", "摄影", "漫展", "后勤", "返图", "正片", "棚拍", "外景", "cos摄", "蹲摄", "扩列约拍", "接拍"],
            "intent_threshold": 80,
            "private_system_prompt": DEFAULT_PRIV_PROMPT,
            "group_system_prompt": "",
        }
        if not active_im_rules_path.exists():
            return default_rules
        try:
            data = json.loads(active_im_rules_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "qq_groups": [str(x).strip() for x in (data.get("qq_groups") or []) if str(x).strip()],
                    "wechat_groups": [str(x).strip() for x in (data.get("wechat_groups") or []) if str(x).strip()],
                    "wechat_private_chats": [str(x).strip() for x in (data.get("wechat_private_chats") or []) if str(x).strip()],
                    "wechat_auto_reply_all_private": bool(data.get("wechat_auto_reply_all_private", True)),
                    "private_only_reply_related": bool(data.get("private_only_reply_related", True)),
                    "wechat_private_blacklist": [str(x).strip() for x in (data.get("wechat_private_blacklist") or []) if str(x).strip()],
                    "keywords": [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()] or default_rules["keywords"],
                    "intent_threshold": max(0, min(100, int(data.get("intent_threshold", 80)))),
                    "private_system_prompt": str(data.get("private_system_prompt") or "").strip() or DEFAULT_PRIV_PROMPT,
                    "group_system_prompt": str(data.get("group_system_prompt") or "").strip(),
                }
        except Exception:
            pass
        return default_rules

    def _write_local_im_rules(body: Dict[str, Any]) -> Dict[str, Any]:
        active_im_rules_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = {
            "qq_groups": list(dict.fromkeys(str(x).strip() for x in (body.get("qq_groups") or []) if str(x).strip())),
            "wechat_groups": list(dict.fromkeys(str(x).strip() for x in (body.get("wechat_groups") or []) if str(x).strip())),
            "wechat_private_chats": list(dict.fromkeys(str(x).strip() for x in (body.get("wechat_private_chats") or []) if str(x).strip())),
            "wechat_auto_reply_all_private": bool(body.get("wechat_auto_reply_all_private", True)),
            "private_only_reply_related": bool(body.get("private_only_reply_related", True)),
            "wechat_private_blacklist": list(dict.fromkeys(str(x).strip() for x in (body.get("wechat_private_blacklist") or []) if str(x).strip())),
            "keywords": list(dict.fromkeys(str(x).strip() for x in (body.get("keywords") or []) if str(x).strip())) or ["约拍", "场照", "找摄", "摄影", "漫展"],
            "intent_threshold": max(0, min(100, int(body.get("intent_threshold", 80)))),
            "private_system_prompt": str(body.get("private_system_prompt") or "").strip() or DEFAULT_PRIV_PROMPT,
            "group_system_prompt": str(body.get("group_system_prompt") or "").strip(),
        }
        active_im_rules_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return normalized

    @app.get("/api/im/rules", dependencies=[Depends(require_session)])
    async def get_im_rules() -> Dict[str, Any]:
        return {"status": "ok", "rules": _read_local_im_rules()}

    @app.put("/api/im/rules", dependencies=[Depends(require_session)])
    async def save_im_rules_api(request: Request) -> Dict[str, Any]:
        body = await request.json()
        saved = _write_local_im_rules(body if isinstance(body, dict) else {})
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        gw_synced = False
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                r = await client.put(f"{gateway_url}/api/gateway/rules", headers={"X-API-Key": key}, json=saved)
                gw_synced = r.status_code == 200
        except Exception:
            pass
        return {"status": "ok", "rules": saved, "gateway_synced": gw_synced}

    @app.get("/api/im/channels/status", dependencies=[Depends(require_session)])
    async def get_im_channels_status() -> Dict[str, Any]:
        c = _cfg()
        local_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        wechat_url = _wechat_gateway_url()
        key = c.get("INTERNAL_API_KEY") or ""
        headers = {"X-API-Key": key}
        data: Dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                # 1) 本地网关拿 QQ / 企业微信 / 基础状态
                try:
                    r = await client.get(f"{local_url}/api/gateway/channels/status", headers=headers)
                    if r.status_code == 200:
                        data = r.json()
                except Exception as exc:
                    logger.debug("本地 IM 网关状态获取失败: %s", exc)
                # 2) 若微信走独立网关，用其微信状态覆盖本地
                if wechat_url != local_url:
                    try:
                        r2 = await client.get(f"{wechat_url}/api/gateway/channels/status", headers=headers)
                        if r2.status_code == 200:
                            remote = r2.json()
                            data["wechat"] = remote.get("wechat", data.get("wechat", {}))
                    except Exception as exc:
                        logger.debug("远程微信网关状态获取失败: %s", exc)
                if data:
                    data["rules"] = _read_local_im_rules()
                    return data
        except Exception as exc:
            logger.debug("获取 IM 通道状态失败: %s", exc)
        return {
            "status": "ok",
            "gateway_offline": True,
            "qq": {
                "online": False,
                "http_url": c.get("NAPCAT_HTTP_URL") or "http://127.0.0.1:3002",
                "ws_url": c.get("NAPCAT_WS_URL") or "ws://127.0.0.1:3001",
                "webhook_url": f"{local_url}/webhook/napcat",
                "reverse_ws_url": local_url.replace("http://", "ws://") + "/ws/napcat",
                "error": "IM 网关未响应",
            },
            "wechat": {"online": False, "monitor_running": False, "mode": c.get("WECHAT_MODE", "desktop"), "error": "IM 网关未响应"},
            "wecom_kf": {
                "online": bool(c.get("WECOM_CORP_ID") and c.get("WECOM_KF_SECRET") and c.get("WECOM_OPEN_KFID")),
                "configured": bool(c.get("WECOM_CORP_ID") and c.get("WECOM_KF_SECRET")),
            },
            "rules": _read_local_im_rules(),
        }

    @app.post("/api/im/qq/test-send", dependencies=[Depends(require_session)])
    async def im_qq_test_send(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                r = await client.post(f"{gateway_url}/api/gateway/qq/test-send", headers={"X-API-Key": key}, json=body)
                if r.status_code >= 400:
                    return {"status": "error", "error": r.text}
                return r.json()
        except Exception as exc:
            return {"status": "error", "error": f"调用 IM 网关失败: {exc}"}

    @app.get("/api/im/napcat/webui", dependencies=[Depends(require_session)])
    async def im_napcat_webui(request: Request) -> Dict[str, Any]:
        """按「你此刻访问中控台用的地址」返回 NapCat 扫码入口。

        - 用内网 IP / localhost 打开中控台 -> 返回内网地址
        - 用公网 IP 打开中控台 -> 返回公网地址
        token 从 NapCat 容器里的 webui.json 实时读取，避免写死导致密码不对。
        """
        c = _cfg()
        host_header = request.headers.get("host", "") or "127.0.0.1:3080"
        host_only = host_header.split(":")[0]
        # 反代/隧道场景下以 X-Forwarded-Host 为准
        fwd = request.headers.get("x-forwarded-host")
        if fwd:
            host_only = fwd.split(",")[0].strip().split(":")[0]

        is_local = _is_lan_host(host_only)

        # 公网入口：优先用 FRP 映射的中控台端口（8081），其次 PUBLIC_BASE_URL
        frp_host = (c.get("VPS_HOST") or "").strip()
        frp_port = (c.get("FRP_PUBLIC_STUDIO_PORT") or "8081").strip()
        if is_local:
            webui_host = host_only
            port = (c.get("NAPCAT_WEBUI_PORT") or "6100").strip()
        else:
            webui_host = frp_host or host_only
            port = frp_port

        # NapCat WebUI token：优先读容器内配置，其次读 .env
        token = (c.get("NAPCAT_WEBUI_TOKEN") or "").strip()
        if not token:
            token = _read_napcat_webui_token()

        base = f"http://{webui_host}:{port}/webui"
        url = f"{base}?token={token}" if token else base
        return {
            "status": "ok",
            "mode": "local" if is_local else "public",
            "host": webui_host,
            "port": port,
            "token": token,
            "has_token": bool(token),
            "webui_url": url,
        }

    @app.get("/api/im/wechat/status", dependencies=[Depends(require_session)])
    async def im_wechat_status() -> Dict[str, Any]:
        c = _cfg()
        gateway_url = _wechat_gateway_url()
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
                r = await client.get(f"{gateway_url}/api/gateway/wechat/status", headers={"X-API-Key": key})
                if r.status_code == 200:
                    return r.json()
        except Exception as exc:
            return {"status": "error", "wechat": {"online": False, "error": str(exc)}}
        return {"status": "error", "wechat": {"online": False, "error": "IM 网关未响应"}}

    @app.post("/api/im/wechat/monitor", dependencies=[Depends(require_session)])
    async def im_wechat_monitor(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        gateway_url = _wechat_gateway_url()
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                r = await client.post(f"{gateway_url}/api/gateway/wechat/monitor", headers={"X-API-Key": key}, json=body)
                if r.status_code >= 400:
                    return {"status": "error", "error": r.text}
                return r.json()
        except Exception as exc:
            return {"status": "error", "error": f"控制微信监控失败: {exc}"}

    @app.post("/api/im/wechat/test-send", dependencies=[Depends(require_session)])
    async def im_wechat_test_send(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        gateway_url = _wechat_gateway_url()
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                r = await client.post(f"{gateway_url}/api/gateway/wechat/test-send", headers={"X-API-Key": key}, json=body)
                if r.status_code >= 400:
                    return {"status": "error", "error": r.text}
                return r.json()
        except Exception as exc:
            return {"status": "error", "error": f"调用微信测试发送失败: {exc}"}

    @app.get("/api/im/qq/groups", dependencies=[Depends(require_session)])
    async def im_qq_groups() -> Dict[str, Any]:
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.get(f"{gateway_url}/api/gateway/qq/groups", headers={"X-API-Key": key})
                if r.status_code == 200:
                    return r.json()
        except Exception as exc:
            return {"status": "error", "groups": [], "error": str(exc)}
        return {"status": "error", "groups": [], "error": "IM 网关未响应"}

    @app.get("/api/im/wechat/sessions", dependencies=[Depends(require_session)])
    async def im_wechat_sessions() -> Dict[str, Any]:
        c = _cfg()
        gateway_url = _wechat_gateway_url()
        key = c.get("INTERNAL_API_KEY") or ""
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.get(f"{gateway_url}/api/gateway/wechat/sessions", headers={"X-API-Key": key})
                if r.status_code == 200:
                    return r.json()
        except Exception as exc:
            return {"status": "error", "sessions": [], "error": str(exc)}
        return {"status": "error", "sessions": [], "error": "IM 网关未响应"}

    # ==================== 消息中心：微信/QQ 双网关永久会话档案 ====================

    def _conversation_source_url(source: str) -> str:
        c = _cfg()
        local_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        return _wechat_gateway_url() if source == "wechat" else local_url

    @app.get("/api/im/conversations", dependencies=[Depends(require_session)])
    async def im_conversations(
        status: str = Query(default="all"),
        q: str = Query(default=""),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        c = _cfg()
        local_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        wechat_url = _wechat_gateway_url()
        sources = {"local": local_url}
        if wechat_url != local_url:
            sources["wechat"] = wechat_url
        key = c.get("INTERNAL_API_KEY") or ""

        async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
            async def fetch_source(source: str, gateway_url: str) -> Dict[str, Any]:
                try:
                    response = await client.get(
                        f"{gateway_url}/api/gateway/conversations",
                        headers={"X-API-Key": key},
                        params={"status": status, "q": q, "limit": limit},
                    )
                    if response.status_code != 200:
                        return {"source": source, "error": f"网关返回 {response.status_code}", "items": []}
                    data = response.json()
                    for item in data.get("items") or []:
                        item["source"] = source
                    data["source"] = source
                    return data
                except Exception as exc:
                    return {"source": source, "error": str(exc), "items": []}

            results = await asyncio.gather(*(fetch_source(source, url) for source, url in sources.items()))

        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        stats = {"total": 0, "needs_reply": 0, "replied": 0, "no_reply": 0}
        for result in results:
            items.extend(result.get("items") or [])
            for field in stats:
                stats[field] += int((result.get("stats") or {}).get(field) or 0)
            if result.get("error"):
                errors.append({"source": str(result.get("source") or ""), "error": str(result["error"])})
        items.sort(key=lambda item: float(item.get("last_activity") or 0), reverse=True)
        return {
            "status": "partial" if errors and items else ("error" if errors else "ok"),
            "items": items[:limit],
            "total": sum(int(result.get("total") or 0) for result in results),
            "stats": stats,
            "errors": errors,
        }

    @app.get("/api/im/conversations/detail", dependencies=[Depends(require_session)])
    async def im_conversation_detail(
        source: str = Query(default="wechat"),
        platform: str = Query(...),
        chat_id: str = Query(...),
        limit: int = Query(default=500, ge=1, le=1000),
        before_id: Optional[int] = Query(default=None),
    ) -> Dict[str, Any]:
        if source not in {"local", "wechat"}:
            raise HTTPException(status_code=400, detail="未知消息网关")
        c = _cfg()
        params: Dict[str, Any] = {"platform": platform, "chat_id": chat_id, "limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                response = await client.get(
                    f"{_conversation_source_url(source)}/api/gateway/conversations/detail",
                    headers={"X-API-Key": c.get("INTERNAL_API_KEY") or ""},
                    params=params,
                )
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail=f"消息网关返回 {response.status_code}")
            data = response.json()
            data.setdefault("conversation", {})["source"] = source
            return data
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取会话失败: {exc}") from exc

    @app.post("/api/im/conversations/reply", dependencies=[Depends(require_session)])
    async def im_conversation_reply(request: Request) -> Dict[str, Any]:
        body = await request.json()
        source = str(body.pop("source", "wechat"))
        if source not in {"local", "wechat"}:
            raise HTTPException(status_code=400, detail="未知消息网关")
        c = _cfg()
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                response = await client.post(
                    f"{_conversation_source_url(source)}/api/gateway/conversations/reply",
                    headers={"X-API-Key": c.get("INTERNAL_API_KEY") or ""},
                    json=body,
                )
            data = response.json()
            if response.status_code >= 400:
                raise HTTPException(status_code=response.status_code, detail=data.get("detail") or data)
            return data
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"人工回复失败: {exc}") from exc

    # ==================== 面板 3：自动拷卡、聚类控制台与手动绑单 ====================

    @app.get("/api/ingest/drives", dependencies=[Depends(require_session)])
    async def list_removable_drives() -> Dict[str, Any]:
        c = _cfg()
        nas = probe_nas_status(c)
        return {
            "status": "ok",
            "drives": detect_windows_removable_drives(),
            "default_preview_xmp": c.get("PREVIEW_XMP_PATH") or "",
            "nas": {
                "online": bool(nas.get("online")),
                "path": nas.get("path") or "",
                "active_mode": nas.get("active_mode") or "OFFLINE",
                "probing": bool(nas.get("probing")),
            },
        }

    @app.post("/api/ingest/check-source", dependencies=[Depends(require_session)])
    async def check_source_dir(payload: CheckSourceRequest) -> Dict[str, Any]:
        """只读诊断源目录，让前端在点「开始拷卡」前就能看到具体是哪里读不到。"""
        diag = diagnose_source_dir(payload.source_dir)
        nas = probe_nas_status(_cfg())
        return {
            "status": "ok",
            "source": diag,
            "ok": not diag["error"],
            "nas": {
                "online": bool(nas.get("online")),
                "path": nas.get("path") or "",
                "active_mode": nas.get("active_mode") or "OFFLINE",
                "probing": bool(nas.get("probing")),
            },
        }

    @app.post("/api/ingest/start", dependencies=[Depends(require_session)])
    async def start_ingest_pipeline(payload: IngestStartRequest) -> Dict[str, Any]:
        # 源目录精确诊断：把"读取不到"的真实原因直接告诉用户，而不是笼统的"不存在"
        diag = diagnose_source_dir(payload.source_dir)
        if diag["error"]:
            raise HTTPException(
                status_code=400,
                detail=f"{diag['error']}" + (f"（{diag['hint']}）" if diag["hint"] else ""),
            )
        src = Path(diag["raw"])

        # NAS 落盘目标预检：源卡能读但 NAS 写不进去，同样要在开始前拦住。
        # 但探测结果尚未返回时不能误拦（否则刚启动就点会被无故拒绝），交给 ingest.py 自己再校验。
        nas = probe_nas_status(_cfg())
        if not nas.get("online") and not nas.get("probing"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"NAS 备份目标不可达（当前配置: {nas.get('path') or '未配置'}），"
                    "拷卡会失败。请先在「系统设置 → 存储与拷卡路径」里填好可访问的 NAS 路径，"
                    "或在资源管理器里重新连接该网络位置后再试。"
                ),
            )

        pipeline_dir = WORKSPACE_ROOT / "pipelines" / "ingest-cull-cluster"
        ingest_cmd = [
            sys.executable,
            str(pipeline_dir / "ingest.py"),
            "--source", str(src),
            "--event", payload.event_name.strip(),
            "--venue", payload.venue.strip(),
        ]
        if payload.preview_xmp_path.strip():
            ingest_cmd.extend(["--preview-xmp", payload.preview_xmp_path.strip()])

        steps: List[Tuple[str, List[str], Path]] = [("自动拷卡与流式双校验", ingest_cmd, pipeline_dir)]
        if payload.run_cluster:
            steps.append(
                (
                    "废片预筛与角色聚类回写",
                    [sys.executable, str(pipeline_dir / "cull_and_cluster.py"), "--manifest", "__MANIFEST_PATH__"],
                    pipeline_dir,
                )
            )
        job_id = jobs.start_sequence(f"拷卡聚类: {payload.event_name}", steps)
        return {"status": "ok", "job_id": job_id}

    @app.get("/api/ingest/unassigned-clusters", dependencies=[Depends(require_session)])
    async def get_unassigned_clusters() -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.get(
                f"{booking_url}/api/internal/clusters/unassigned",
                headers={"X-API-Key": key},
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/ingest/bind-cluster", dependencies=[Depends(require_session)])
    async def bind_cluster(payload: BindClusterRequest) -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(
                f"{booking_url}/api/internal/clusters/bind",
                headers={"X-API-Key": key},
                json={
                    "batch_id": payload.batch_id,
                    "cluster_id": payload.cluster_id,
                    "booking_id": payload.booking_id,
                },
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    # ---------- 插卡拷卡：手动导入 + 手动分类分人 ----------

    def _internal_headers() -> tuple:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        return booking_url, {"X-API-Key": c.get("INTERNAL_API_KEY") or ""}

    def _preview_output_dir() -> Path:
        c = _cfg()
        raw = (c.get("PREVIEW_OUTPUT_DIR") or "").strip()
        # 若 Mac/Linux 误继承了 Windows 盘符路径（如 D:\...），自动回退到本机 booking-site/uploads/previews
        if raw and (os.name == "nt" or not re.match(r"^[A-Za-z]:[\\/]", raw)):
            return Path(raw)
        return WORKSPACE_ROOT / "apps" / "booking-site" / "uploads" / "previews"

    async def _resolve_manual_attendee(client: "httpx.AsyncClient", payload: ManualImportRequest) -> tuple:
        """确定手动导入的目标客户：已选订单直接复用；勾选新建则创建订单；都没有则不分类。"""
        booking_url, headers = _internal_headers()
        event_name = payload.event_name.strip()

        if payload.booking_id:
            r = await client.get(f"{booking_url}/api/internal/bookings/{int(payload.booking_id)}", headers=headers)
            if r.status_code >= 400:
                raise HTTPException(status_code=404, detail=f"所选客户订单不存在 (#{payload.booking_id})")
            booking = r.json().get("booking") or {}
            return booking, booking.get("cn") or payload.attendee.strip() or f"客户{payload.booking_id}"

        attendee = payload.attendee.strip()
        if payload.create_booking and attendee:
            body = {
                "cn": attendee,
                "character": payload.character.strip(),
                "event_name": event_name,
                "date": payload.shoot_date.strip() or time.strftime("%Y-%m-%d"),
                "time": MANUAL_BOOKING_TIME_SLOT,
                "platform": "internal",
                "remarks": "插卡拷卡面板手动导入时自动建档",
                # 现场客户已到场，不应被客户自助预约的时段容量挡住
                "skip_capacity_check": True,
            }
            r = await client.post(f"{booking_url}/api/internal/bookings", headers=headers, json=body)
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("message") or detail
                except Exception:
                    pass
                raise HTTPException(status_code=r.status_code, detail=f"为客户 {attendee} 建档失败: {detail}")
            created = r.json()
            return created.get("booking") or {}, attendee

        if not attendee:
            return None, ""
        # 只填了人名但没建档：按姓名在现有订单里找（同场次优先），找不到就只入库不分人
        params = {"event_name": event_name} if event_name else {}
        r = await client.get(f"{booking_url}/api/internal/bookings/by-event", headers=headers, params=params)
        if r.status_code == 200:
            for b in r.json().get("bookings") or []:
                if str(b.get("cn") or "").strip() == attendee:
                    return b, attendee
        return None, attendee

    @app.post("/api/ingest/manual-upload", dependencies=[Depends(require_session)])
    async def manual_upload_temp(file: UploadFile = File(...)) -> Dict[str, Any]:
        """手动导入第 1 步：浏览器直接把图片/RAW 上传到控制中心的临时暂存区。"""
        orig_name = Path(file.filename or "photo.jpg").name
        ext = Path(orig_name).suffix.lower()
        if ext not in MANUAL_IMPORT_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的图片格式 {ext or '(无扩展名)'}，支持: {', '.join(sorted(MANUAL_IMPORT_EXTENSIONS))}",
            )
        tmp_root = active_sandbox_dir / "manual_import"
        tmp_root.mkdir(parents=True, exist_ok=True)
        token = f"{uuid.uuid4().hex}{ext}"
        target = tmp_root / token
        size = 0
        with open(target, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                fh.write(chunk)
        return {
            "status": "ok",
            "token": token,
            "name": orig_name,
            "size_bytes": size,
            "path": str(target),
        }

    @app.post("/api/ingest/manual-import", dependencies=[Depends(require_session)])
    async def manual_import(payload: ManualImportRequest) -> Dict[str, Any]:
        """手动导入第 2 步：把暂存文件/本地路径拷入预览目录并登记为独立批次（可同时完成手动分类）。"""
        booking_url, headers = _internal_headers()

        # 1) 解析出真实的源文件路径：暂存 token 优先，其次本地路径/目录
        local_files: List[Path] = []
        staged: List[Dict[str, Any]] = []
        tmp_root = active_sandbox_dir / "manual_import"
        for item in payload.sources:
            if item.token:
                token_name = Path(str(item.token)).name
                candidate = tmp_root / token_name
                if not candidate.is_file():
                    raise HTTPException(status_code=400, detail=f"暂存文件已失效，请重新选择: {item.name or token_name}")
                staged.append({"path": candidate, "name": item.name or token_name})
                local_files.append(candidate)
            elif item.path:
                extra = _iter_manual_import_files([item.path])
                local_files.extend(extra)
        path_pool = [s.path.strip() for s in payload.file_paths if s and s.strip()]
        if path_pool:
            local_files.extend(_iter_manual_import_files(path_pool))
        if not local_files:
            raise HTTPException(status_code=400, detail="没有可导入的图片，请选择文件或拖入图片后再导入")

        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            # 2) 先确定客户（手动分类：导入即归属给指定的人）
            booking, attendee = await _resolve_manual_attendee(client, payload)
            event_name = payload.event_name.strip() or (booking or {}).get("event_name") or "手动导入"
            booking_id = int(booking["id"]) if booking and booking.get("id") else None

            # 3) 拷入预览目录 + 生成 2048px WebP，构建回写资产清单
            batch_id = _unique_manual_batch_id(attendee, event_name)
            preview_root = _preview_output_dir()
            preview_dir = preview_root / batch_id
            preview_dir.mkdir(parents=True, exist_ok=True)
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            used_names: Dict[str, int] = {}
            assets_payload: List[Dict[str, Any]] = []
            skipped: List[str] = []

            for src in local_files:
                if not src.is_file():
                    skipped.append(src.name)
                    continue
                try:
                    raw = src.read_bytes()
                except OSError:
                    skipped.append(src.name)
                    continue

                display_name = next((s["name"] for s in staged if s["path"] == src), src.name)
                lower = display_name.lower()
                if lower in used_names:
                    used_names[lower] += 1
                    stem = Path(display_name).stem
                    display_name = f"{stem}_{used_names[lower]}{Path(display_name).suffix}"
                else:
                    used_names[lower] = 1

                # 原图一并留存，保证 RAW 也有可回溯的原始文件
                stored_raw = preview_dir / f"src_{display_name}"
                stored_raw.write_bytes(raw)
                preview_name = f"{Path(display_name).stem}.webp"
                preview_backend = _write_webp_preview_from_bytes(raw, preview_dir / preview_name, source_path=stored_raw)
                if preview_backend.startswith("skipped"):
                    preview_url = f"/previews/{batch_id}/{stored_raw.name}"
                else:
                    preview_url = f"/previews/{batch_id}/{preview_name}"

                assets_payload.append(
                    {
                        "filename": display_name,
                        "nas_raw_path": str(stored_raw),
                        "preview_url": preview_url,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "is_rejected": 0,
                        "reject_reason": None,
                        "cluster_id": "manual" if booking_id else None,
                        "captured_at": _extract_manual_captured_at(raw) or now_str,
                        "booking_ids": [booking_id] if booking_id else [],
                        "source": "manual",
                    }
                )

            if not assets_payload:
                raise HTTPException(status_code=400, detail="所选图片均无法读取，已取消导入")

            # 4) 回写批次（source=manual 便于中控台区分源）
            r = await client.post(
                f"{booking_url}/api/internal/batches",
                headers=headers,
                json={
                    "batch_id": batch_id,
                    "camera_model": "手动导入",
                    "event_name": event_name,
                    "venue": payload.event_name.strip(),
                    "nas_path": str(preview_dir),
                    "total_files": len(assets_payload),
                    "verified_files": len(assets_payload),
                    "source": "manual",
                    "imported_at": now_str,
                    "assets": assets_payload,
                },
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=f"登记导入批次失败: {r.text}")
            sync = r.json()

        # 暂存文件用完即清理（导入后的原图已随批次留存）
        for s in staged:
            try:
                s["path"].unlink(missing_ok=True)
            except OSError:
                pass

        return {
            "status": "ok",
            "batch_id": batch_id,
            "imported": len(assets_payload),
            "skipped": skipped,
            "attendee": attendee,
            "booking_id": booking_id,
            "booking_created": bool(payload.create_booking and booking and not payload.booking_id),
            "event_name": event_name,
            "gallery_token": (booking or {}).get("gallery_token") or "",
            "synced": {
                "inserted_assets": sync.get("inserted_assets"),
                "total_links": sync.get("total_links"),
            },
        }

    @app.get("/api/ingest/batches", dependencies=[Depends(require_session)])
    async def list_ingest_batches(limit: int = 60, source: str = "") -> Dict[str, Any]:
        booking_url, headers = _internal_headers()
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 60), 300))}
        if source in ("auto", "manual"):
            params["source"] = source
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(f"{booking_url}/api/internal/batches", headers=headers, params=params)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.get("/api/ingest/batches/{batch_id}/assets", dependencies=[Depends(require_session)])
    async def list_batch_assets(batch_id: str, include_rejected: bool = False) -> Dict[str, Any]:
        booking_url, headers = _internal_headers()
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            r = await client.get(
                f"{booking_url}/api/internal/batches/{batch_id}/assets",
                headers=headers,
                params={"include_rejected": "1" if include_rejected else "0"},
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/ingest/bind-assets", dependencies=[Depends(require_session)])
    async def bind_assets_to_booking(payload: BindAssetsRequest) -> Dict[str, Any]:
        """手动分人：把选中的素材换人 / 合影加人 / 取消归属。"""
        if not payload.asset_ids:
            raise HTTPException(status_code=400, detail="请先选择要归属的照片")
        mode = payload.mode if payload.mode in ("replace", "add", "remove") else "replace"
        if mode != "remove" and not payload.booking_id:
            raise HTTPException(status_code=400, detail="请选择要归属的客户订单")
        booking_url, headers = _internal_headers()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.post(
                f"{booking_url}/api/internal/assets/bind-bookings",
                headers=headers,
                json={"asset_ids": payload.asset_ids, "booking_id": payload.booking_id, "mode": mode},
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/ingest/new-customer-for-assets", dependencies=[Depends(require_session)])
    async def create_customer_for_assets(payload: CreateBookingForAssetsRequest) -> Dict[str, Any]:
        """手动分人：现场直接新建客户档案，并把这批照片一次性分给该客户。

        注意：现场手动建档不占用客户自助预约的时段容量，默认写入「手动导入」时段，
        避免把正常预约档期挤满；如需指定时段，前端传入 time 即可。
        """
        cn = payload.cn.strip()
        if not cn:
            raise HTTPException(status_code=400, detail="请先填写客户 CN")
        booking_url, headers = _internal_headers()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            body = {
                "cn": cn,
                "character": payload.character.strip(),
                "event_name": payload.event_name.strip(),
                "date": payload.date.strip() or time.strftime("%Y-%m-%d"),
                "time": payload.time.strip() or MANUAL_BOOKING_TIME_SLOT,
                "platform": "internal",
                "remarks": "插卡拷卡面板手动分人时新建",
                # 现场客户已到场，不应被客户自助预约的时段容量挡住
                "skip_capacity_check": True,
            }
            r = await client.post(f"{booking_url}/api/internal/bookings", headers=headers, json=body)
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("message") or detail
                except Exception:
                    pass
                raise HTTPException(status_code=r.status_code, detail=f"新建客户失败: {detail}")
            created = r.json()
            booking = created.get("booking") or {}
            booking_id = created.get("booking_id") or booking.get("id")

            bound = None
            if payload.asset_ids:
                mode = payload.mode if payload.mode in ("replace", "add") else "replace"
                rb = await client.post(
                    f"{booking_url}/api/internal/assets/bind-bookings",
                    headers=headers,
                    json={"asset_ids": payload.asset_ids, "booking_id": booking_id, "mode": mode},
                )
                if rb.status_code >= 400:
                    raise HTTPException(status_code=rb.status_code, detail=f"客户已建档但分人失败: {rb.text}")
                bound = rb.json()

        return {
            "status": "ok",
            "booking_id": booking_id,
            "cn": booking.get("cn") or cn,
            "gallery_token": created.get("gallery_token") or booking.get("gallery_token") or "",
            "bound": bound,
        }

    @app.post("/api/ingest/push-gallery", dependencies=[Depends(require_session)])
    async def push_gallery_link(payload: PushGalleryRequest) -> Dict[str, Any]:
        c = _cfg()
        gateway_url = (c.get("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
        public_base = (c.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or (
            c.get("BOOKING_API_URL") or "http://127.0.0.1:3000"
        ).rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(
                f"{gateway_url}/api/gateway/notify-gallery",
                headers={"X-API-Key": key},
                json={
                    **payload.model_dump(),
                    "gallery_url": f"{public_base}/gallery/{payload.gallery_token}",
                },
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    # ==================== 后台任务日志查询 ====================

    @app.get("/api/jobs", dependencies=[Depends(require_session)])
    async def list_jobs() -> Dict[str, Any]:
        return {"status": "ok", "jobs": jobs.list_all()}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_session)])
    async def get_job_detail(job_id: str) -> Dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "ok", "job": job}

    # ==================== 面板 4：修图队列、终审放行与 Skill 编辑器 ====================

    @app.get("/api/retouch/review-queue", dependencies=[Depends(require_session)])
    async def get_retouch_and_review() -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            rev_r = await client.get(f"{booking_url}/api/admin/review-queue", auth=auth_pair)
            pend_r = await client.get(f"{booking_url}/api/internal/retouch/pending", headers={"X-API-Key": key})

        review_data = rev_r.json() if rev_r.status_code == 200 else {"bookings": [], "pending_count": 0}
        pending_data = pend_r.json() if pend_r.status_code == 200 else {"bookings": [], "count": 0}
        return {
            "status": "ok",
            "pending_retouch": pending_data.get("bookings") or [],
            "review_bookings": review_data.get("bookings") or [],
            "review_pending_count": review_data.get("pending_count", 0),
        }

    @app.post("/api/retouch/run-once", dependencies=[Depends(require_session)])
    async def trigger_retouch_once() -> Dict[str, Any]:
        retouch_dir = WORKSPACE_ROOT / "pipelines" / "retouch-engine"
        job_id = jobs.start_sequence(
            "正式修图引擎 worker --once",
            [("执行修图队列 (worker.py --once)", [sys.executable, str(retouch_dir / "worker.py"), "--once"], retouch_dir)],
        )
        return {"status": "ok", "job_id": job_id}

    @app.post("/api/retouch/bookings/{booking_id}/retry", dependencies=[Depends(require_session)])
    async def retry_booking_retouch(booking_id: int) -> Dict[str, Any]:
        """重置某订单未完成的精修任务，使随身修图机立即重新拾取。"""
        booking_url, headers = _internal_headers()
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/internal/retouch/bookings/{booking_id}/retry", headers=headers)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/retouch/bookings/{booking_id}/approve", dependencies=[Depends(require_session)])
    async def approve_delivery_proxy(booking_id: int) -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/admin/bookings/{booking_id}/approve-delivery", auth=auth_pair)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/retouch/jobs/{job_id}/feedback", dependencies=[Depends(require_session)])
    async def submit_retouch_feedback(job_id: int, request: Request) -> Dict[str, Any]:
        """摄影师看图后在图片下方提交自然语言修改意见，由后台 Worker 自动驱动 AI + PS 在原 PSD 上叠加修改。"""
        body = await request.json()
        feedback = str((body or {}).get("feedback") or "").strip()
        if not feedback:
            raise HTTPException(status_code=400, detail="请填写具体修改意见")
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            r = await client.post(
                f"{booking_url}/api/retouch/jobs/{job_id}/feedback",
                auth=auth_pair,
                json={"feedback": feedback},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("message") or detail
                except Exception:
                    pass
                raise HTTPException(status_code=r.status_code, detail=detail)
            return r.json()

    @app.get("/api/retouch/jobs/{job_id}/revisions", dependencies=[Depends(require_session)])
    async def list_retouch_revisions(job_id: int) -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        key = c.get("INTERNAL_API_KEY") or ""
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(
                f"{booking_url}/api/internal/retouch/jobs/{job_id}/revisions",
                headers={"X-API-Key": key},
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.get("/api/retouch/ps-status", dependencies=[Depends(require_session)])
    async def get_ps_automation_status() -> Dict[str, Any]:
        """检查零干预 Windows COM Photoshop 自动化与远程 Windows 随身修图机在线状态。"""
        c = _cfg()
        com_ready = False
        exe_str = ""
        running = False
        if os.name == "nt":
            retouch_dir = str(WORKSPACE_ROOT / "pipelines" / "retouch-engine")
            if retouch_dir not in sys.path:
                sys.path.insert(0, retouch_dir)
            try:
                from ps_com_bridge import find_photoshop_exe, is_com_available, _is_photoshop_process_running

                exe = find_photoshop_exe(c.get("PS_EXE_PATH") or None)
                com_ok = is_com_available()
                running = _is_photoshop_process_running()
                exe_str = str(exe) if exe else ""
                com_ready = bool(com_ok and (exe is not None or running))
            except Exception:
                pass

        remote_worker: Dict[str, Any] = {"online": False}
        try:
            booking_url, headers = _internal_headers()
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                r = await client.get(f"{booking_url}/api/internal/worker/status", headers=headers)
                if r.status_code == 200:
                    remote_worker = r.json().get("worker") or {"online": False}
        except Exception:
            pass

        return {
            "status": "ok",
            "backend": (c.get("PS_BACKEND") or "auto").strip().lower(),
            "com_ready": bool(com_ready or remote_worker.get("online")),
            "remote_worker_online": bool(remote_worker.get("online")),
            "remote_worker": remote_worker,
            "photoshop_exe": exe_str,
            "photoshop_running": running,
            "auto_close": str(c.get("PS_AUTO_CLOSE", "true")).lower() != "false",
        }

    @app.get("/api/proxy/outputs/{subpath:path}", dependencies=[Depends(require_session)])
    async def proxy_outputs(subpath: str) -> Response:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(f"{booking_url}/outputs/{subpath}")
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Output image not found")
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))

    @app.get("/api/proxy/previews/{subpath:path}", dependencies=[Depends(require_session)])
    async def proxy_previews(subpath: str) -> Response:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(f"{booking_url}/previews/{subpath}")
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Preview image not found")
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/webp"))

    @app.get("/api/proxy-site/", dependencies=[Depends(require_session)])
    async def proxy_public_site_root():
        return RedirectResponse(url="/api/proxy-site/index.html")

    @app.get("/api/proxy-site/{subpath:path}", dependencies=[Depends(require_session)])
    async def proxy_public_site(subpath: str):
        # 中控台里直接预览客户官网（含 /api/* 与静态资源），避免额外开端口
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, follow_redirects=True) as client:
            r = await client.get(f"{booking_url}/{subpath}")
        media_type = r.headers.get("content-type")
        headers = {}
        location = r.headers.get("location")
        if location:
            headers["Location"] = location
        return Response(content=r.content, status_code=r.status_code, media_type=media_type, headers=headers)

    @app.get("/api/proxy/uploads/{subpath:path}", dependencies=[Depends(require_session)])
    async def proxy_uploads(subpath: str) -> Response:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.get(f"{booking_url}/uploads/{subpath}")
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Uploaded image not found")
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))

    # ---- 单页整合：接单网站后台（公告/价格/例图上传/订单编辑删除/留言管理） ----

    @app.get("/api/site/summary", dependencies=[Depends(require_session)])
    async def get_site_admin_summary() -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r_set, r_port, r_ph, r_msg = await asyncio.gather(
                client.get(f"{booking_url}/api/settings"),
                client.get(f"{booking_url}/api/portal_settings"),
                client.get(f"{booking_url}/api/photos"),
                client.get(f"{booking_url}/api/messages", auth=auth_pair),
                return_exceptions=True,
            )
        return {
            "status": "ok",
            "settings": r_set.json() if not isinstance(r_set, Exception) and r_set.status_code == 200 else {},
            "portal_settings": r_port.json() if not isinstance(r_port, Exception) and r_port.status_code == 200 else {},
            "photos": r_ph.json() if not isinstance(r_ph, Exception) and r_ph.status_code == 200 else [],
            "messages": r_msg.json() if not isinstance(r_msg, Exception) and r_msg.status_code == 200 else [],
        }

    @app.post("/api/site/settings", dependencies=[Depends(require_session)])
    async def save_site_settings(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/settings", auth=auth_pair, json=body)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    # ---- 官网套餐卡片（四张卡全部字段可改） ----

    async def _booking_get(path: str):
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            return await client.get(f"{booking_url}{path}", auth=auth_pair)

    async def _booking_post(path: str, payload: Dict[str, Any]):
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}{path}", auth=auth_pair, json=payload)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.get("/api/site/services", dependencies=[Depends(require_session)])
    async def get_site_services() -> Dict[str, Any]:
        r = await _booking_get("/api/services")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return {"status": "ok", "services": r.json()}

    @app.post("/api/site/services", dependencies=[Depends(require_session)])
    async def save_site_services(request: Request) -> Dict[str, Any]:
        body = await request.json()
        services = body.get("services")
        if not isinstance(services, list):
            raise HTTPException(status_code=400, detail="services 必须是数组")
        result = await _booking_post("/api/portal_settings", {"services": services})
        return {"status": "ok", "result": result}

    # ---- 活动企划（锁档 / 归档同样允许修改） ----

    @app.get("/api/site/events", dependencies=[Depends(require_session)])
    async def get_site_events() -> Dict[str, Any]:
        r = await _booking_get("/api/portal_config")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()
        return {"status": "ok", "events": data.get("events", []), "slot_capacity": data.get("slot_capacity", 1)}

    @app.post("/api/site/events", dependencies=[Depends(require_session)])
    async def save_site_events(request: Request) -> Dict[str, Any]:
        body = await request.json()
        events = body.get("events")
        if not isinstance(events, list):
            raise HTTPException(status_code=400, detail="events 必须是数组")
        payload: Dict[str, Any] = {"events": events}
        if body.get("slot_capacity") is not None:
            try:
                payload["slot_capacity"] = max(1, int(body["slot_capacity"]))
            except (TypeError, ValueError):
                payload["slot_capacity"] = 1
        result = await _booking_post("/api/portal_settings", payload)
        return {"status": "ok", "result": result}

    # ---- 订单二维码（发给 AI 用） ----

    @app.get("/api/site/bookings/{booking_id}/qr", dependencies=[Depends(require_session)])
    async def get_booking_qr(booking_id: int) -> Dict[str, Any]:
        r = await _booking_get(f"/api/admin/bookings/{booking_id}/qr")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()

    @app.post("/api/site/upload-photo", dependencies=[Depends(require_session)])
    async def upload_site_sample_photo(
        photo: UploadFile = File(...),
        title: str = Form(""),
        category: str = Form("场照"),
        display_location: str = Form("双端展示"),
    ) -> Dict[str, Any]:
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        raw = await photo.read()
        files = {"photo": (photo.filename or "sample.jpg", raw, photo.content_type or "image/jpeg")}
        data = {"title": title, "category": category, "display_location": display_location}
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, follow_redirects=False) as client:
            r = await client.post(f"{booking_url}/api/upload", auth=auth_pair, files=files, data=data)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        return {"status": "ok"}

    @app.post("/api/site/update-photo", dependencies=[Depends(require_session)])
    async def update_site_sample_photo(request: Request) -> Dict[str, Any]:
        body = await request.json()
        result = await _booking_post("/api/photos/update", body)
        return {"status": "ok", "result": result}

    @app.post("/api/site/delete-photo", dependencies=[Depends(require_session)])
    async def delete_site_sample_photo(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/photos/delete", auth=auth_pair, json={"id": body.get("id")})
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/site/bookings/edit", dependencies=[Depends(require_session)])
    async def edit_site_booking(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/bookings/edit", auth=auth_pair, json=body)
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    @app.post("/api/site/bookings/delete", dependencies=[Depends(require_session)])
    async def delete_site_booking(request: Request) -> Dict[str, Any]:
        body = await request.json()
        c = _cfg()
        booking_url = (c.get("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        auth_pair = (c.get("ADMIN_USER") or "admin", c.get("ADMIN_PASSWORD") or "change-me")
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            r = await client.post(f"{booking_url}/api/bookings/delete", auth=auth_pair, json={"id": body.get("id")})
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()

    # ---- 热插拔修图 Skill Markdown 编辑器（支持拖拽上传 .md/.txt 与启用/停用开关） ----

    def _safe_skill_path(filename: str) -> Path:
        name = Path(filename).name
        if name != filename or not SAFE_SKILL_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="非法 Skill 文件名（仅允许字母数字中文下划线短横线且以 .md 或 .md.disabled 结尾）")
        active_skills_dir.mkdir(parents=True, exist_ok=True)
        return active_skills_dir / name

    @app.get("/api/skills", dependencies=[Depends(require_session)])
    async def list_skills() -> Dict[str, Any]:
        active_skills_dir.mkdir(parents=True, exist_ok=True)
        files = []
        candidates = list(active_skills_dir.glob("*.md")) + list(active_skills_dir.glob("*.md.disabled"))
        for f in sorted(candidates, key=lambda p: p.name.lower()):
            st = f.stat()
            enabled = f.name.endswith(".md") and not f.name.endswith(".md.disabled")
            files.append({
                "filename": f.name,
                "enabled": enabled,
                "size_bytes": st.st_size,
                "updated_at": int(st.st_mtime),
            })
        return {"status": "ok", "skills": files}

    @app.post("/api/skills/upload", dependencies=[Depends(require_session)])
    async def upload_skill_file(file: UploadFile = File(...)) -> Dict[str, Any]:
        orig_name = Path(file.filename or "custom_skill.md").name
        ext = Path(orig_name).suffix.lower()
        if ext not in (".md", ".txt"):
            raise HTTPException(status_code=400, detail="仅支持上传 .md 或 .txt 修图规则文件")
        stem = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", Path(orig_name).stem).strip("_") or f"skill_{uuid.uuid4().hex[:6]}"
        target_name = f"{stem}.md"
        target_path = _safe_skill_path(target_name)
        raw_bytes = await file.read()
        content = raw_bytes.decode("utf-8", errors="replace")
        target_path.write_text(content, encoding="utf-8")
        return {"status": "ok", "filename": target_name, "enabled": True, "size_bytes": target_path.stat().st_size}

    @app.post("/api/skills/{filename}/toggle", dependencies=[Depends(require_session)])
    async def toggle_skill(filename: str) -> Dict[str, Any]:
        p = _safe_skill_path(filename)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Skill 文件不存在")
        if p.name.endswith(".md.disabled"):
            new_name = p.name[: -len(".disabled")]
            new_path = p.with_name(new_name)
            enabled = True
        else:
            new_name = p.name + ".disabled"
            new_path = p.with_name(new_name)
            enabled = False
        p.rename(new_path)
        return {"status": "ok", "old_filename": filename, "filename": new_name, "enabled": enabled}

    @app.get("/api/skills/{filename}", dependencies=[Depends(require_session)])
    async def read_skill(filename: str) -> Dict[str, Any]:
        p = _safe_skill_path(filename)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Skill 文件不存在")
        return {
            "status": "ok",
            "filename": p.name,
            "enabled": not p.name.endswith(".md.disabled"),
            "content": p.read_text(encoding="utf-8"),
        }

    @app.put("/api/skills/{filename}", dependencies=[Depends(require_session)])
    async def save_skill(filename: str, payload: SkillSaveRequest) -> Dict[str, Any]:
        p = _safe_skill_path(filename)
        p.write_text(payload.content, encoding="utf-8")
        return {"status": "ok", "filename": p.name, "size_bytes": p.stat().st_size}

    @app.delete("/api/skills/{filename}", dependencies=[Depends(require_session)])
    async def delete_skill(filename: str) -> Dict[str, Any]:
        p = _safe_skill_path(filename)
        if p.name in ("default_cosplay_skill.md", "default_cosplay_skill.md.disabled"):
            raise HTTPException(status_code=400, detail="默认基线规则文件 default_cosplay_skill.md 不可删除")
        if not p.exists():
            raise HTTPException(status_code=404, detail="Skill 文件不存在")
        p.unlink()
        return {"status": "ok", "deleted": p.name}

    # ---- Sprint 6: XMP 预设可视化上传、参考图绑定与 presets_map 管理 ----

    def _load_presets_map_dict() -> Dict[str, Any]:
        if not active_presets_path.exists():
            return {}
        try:
            data = json.loads(active_presets_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_presets_map_dict(data: Dict[str, Any]) -> None:
        active_presets_path.parent.mkdir(parents=True, exist_ok=True)
        active_presets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @app.get("/api/presets-map", dependencies=[Depends(require_session)])
    async def read_presets_map() -> Dict[str, Any]:
        return {"status": "ok", "data": _load_presets_map_dict()}

    @app.put("/api/presets-map", dependencies=[Depends(require_session)])
    async def write_presets_map(request: Request) -> Dict[str, Any]:
        body = await request.json()
        data = body.get("data") if isinstance(body, dict) and "data" in body else body
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="presets_map.json 必须是合法 JSON 对象")
        _save_presets_map_dict(data)
        return {"status": "ok"}

    @app.get("/api/presets/list", dependencies=[Depends(require_session)])
    async def list_presets_and_xmps() -> Dict[str, Any]:
        c = _cfg()
        active_preview_xmp_dir.mkdir(parents=True, exist_ok=True)
        active_retouch_xmp_dir.mkdir(parents=True, exist_ok=True)
        active_retouch_refs_dir.mkdir(parents=True, exist_ok=True)
        preview_files = sorted([p.name for p in active_preview_xmp_dir.glob("*.xmp")])
        retouch_files = sorted([p.name for p in active_retouch_xmp_dir.glob("*.xmp")])
        pmap = _load_presets_map_dict()
        enriched: Dict[str, Any] = {}
        for code, entry in pmap.items():
            if not isinstance(entry, dict):
                continue
            ref_raw = str(entry.get("reference_image") or "").strip()
            ref_url = f"/api/presets/ref-image/{Path(ref_raw).name}" if ref_raw else ""
            xmp_name = str(entry.get("xmp_file") or "").strip()
            xmp_exists = bool(xmp_name and (active_retouch_xmp_dir / Path(xmp_name).name).exists())
            enriched[code] = {**entry, "ref_preview_url": ref_url, "xmp_exists": xmp_exists}
        return {
            "status": "ok",
            "preview_xmp_path": c.get("PREVIEW_XMP_PATH") or "",
            "preview_xmp_files": preview_files,
            "retouch_xmp_files": retouch_files,
            "presets": enriched,
        }

    @app.post("/api/presets/upload-xmp", dependencies=[Depends(require_session)])
    async def upload_xmp_preset(
        file: UploadFile = File(...),
        category: str = Form("retouch"),
        preset_code: str = Form(""),
        name: str = Form(""),
        default_notes: str = Form(""),
    ) -> Dict[str, Any]:
        orig_name = Path(file.filename or "preset.xmp").name
        if Path(orig_name).suffix.lower() != ".xmp":
            raise HTTPException(status_code=400, detail="仅支持上传 .xmp 调色预设文件")
        safe_stem = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", Path(orig_name).stem).strip("_") or f"preset_{uuid.uuid4().hex[:6]}"
        safe_filename = f"{safe_stem}.xmp"
        data_bytes = await file.read()
        cat = (category or "retouch").strip().lower()

        if cat == "preview":
            active_preview_xmp_dir.mkdir(parents=True, exist_ok=True)
            dest = active_preview_xmp_dir / safe_filename
            dest.write_bytes(data_bytes)
            write_env_updates(active_env_path, {"PREVIEW_XMP_PATH": str(dest)})
            return {
                "status": "ok",
                "category": "preview",
                "filename": safe_filename,
                "saved_path": str(dest),
                "preview_xmp_path": str(dest),
            }

        active_retouch_xmp_dir.mkdir(parents=True, exist_ok=True)
        dest = active_retouch_xmp_dir / safe_filename
        dest.write_bytes(data_bytes)

        code = re.sub(r"[^A-Za-z0-9_\-]+", "_", (preset_code or safe_stem).strip()).strip("_") or f"preset_{uuid.uuid4().hex[:6]}"
        pmap = _load_presets_map_dict()
        existing = pmap.get(code) if isinstance(pmap.get(code), dict) else {}
        pmap[code] = {
            "name": name.strip() or existing.get("name") or safe_stem,
            "xmp_file": safe_filename,
            "reference_image": existing.get("reference_image") or "",
            "ps_action_set": existing.get("ps_action_set") or "ZERO_STUDIO",
            "ps_action_name": existing.get("ps_action_name") or "",
            "default_notes": default_notes.strip() or existing.get("default_notes") or "",
        }
        _save_presets_map_dict(pmap)
        return {
            "status": "ok",
            "category": "retouch",
            "filename": safe_filename,
            "saved_path": str(dest),
            "preset_code": code,
            "preset": pmap[code],
        }

    @app.post("/api/presets/upload-ref", dependencies=[Depends(require_session)])
    async def upload_preset_ref_image(
        file: UploadFile = File(...),
        preset_code: str = Form(...),
    ) -> Dict[str, Any]:
        code = preset_code.strip()
        if not code or not SAFE_PRESET_CODE_RE.match(code):
            raise HTTPException(status_code=400, detail="无效的 preset_code")
        ext = Path(file.filename or "ref.jpg").suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(status_code=400, detail="参考图仅支持 JPG / PNG / WebP 格式")
        active_retouch_refs_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"ref_{code}_{uuid.uuid4().hex[:6]}{ext}"
        dest = active_retouch_refs_dir / safe_name
        dest.write_bytes(await file.read())

        try:
            rel_path = str(dest.relative_to(WORKSPACE_ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = str(dest)

        pmap = _load_presets_map_dict()
        existing = pmap.get(code) if isinstance(pmap.get(code), dict) else {
            "name": code, "xmp_file": "", "ps_action_set": "ZERO_STUDIO", "ps_action_name": "", "default_notes": ""
        }
        existing["reference_image"] = rel_path
        pmap[code] = existing
        _save_presets_map_dict(pmap)
        return {
            "status": "ok",
            "preset_code": code,
            "reference_image": rel_path,
            "ref_preview_url": f"/api/presets/ref-image/{safe_name}",
        }

    @app.get("/api/presets/ref-image/{filename}", dependencies=[Depends(require_session)])
    async def get_preset_ref_image(filename: str) -> FileResponse:
        safe = Path(filename).name
        p = active_retouch_refs_dir / safe
        if not p.exists():
            raise HTTPException(status_code=404, detail="参考图不存在")
        return FileResponse(p)

    @app.post("/api/presets/save-map", dependencies=[Depends(require_session)])
    async def save_presets_map_post(request: Request) -> Dict[str, Any]:
        body = await request.json()
        data = body.get("presets") if isinstance(body, dict) and "presets" in body else body
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="presets 数据格式错误")
        cleaned: Dict[str, Any] = {}
        for code, entry in data.items():
            c_clean = str(code).strip()
            if not c_clean or not isinstance(entry, dict):
                continue
            cleaned[c_clean] = {
                "name": str(entry.get("name") or c_clean).strip(),
                "xmp_file": str(entry.get("xmp_file") or "").strip(),
                "reference_image": str(entry.get("reference_image") or "").strip(),
                "ps_action_set": str(entry.get("ps_action_set") or "ZERO_STUDIO").strip(),
                "ps_action_name": str(entry.get("ps_action_name") or "").strip(),
                "default_notes": str(entry.get("default_notes") or "").strip(),
            }
        _save_presets_map_dict(cleaned)
        return {"status": "ok", "count": len(cleaned)}

    @app.delete("/api/presets/{code}", dependencies=[Depends(require_session)])
    async def delete_preset_entry(code: str) -> Dict[str, Any]:
        pmap = _load_presets_map_dict()
        if code not in pmap:
            raise HTTPException(status_code=404, detail="预设不存在")
        del pmap[code]
        _save_presets_map_dict(pmap)
        return {"status": "ok", "deleted": code}

    # ---- Sprint 6: 单图仿色与修图实验室 (Color-Match & Retouch Sandbox) ----

    @app.post("/api/retouch/sandbox", dependencies=[Depends(require_session)])
    async def run_retouch_sandbox(
        image: UploadFile = File(...),
        preset_code: str = Form(""),
        send_to_ps: bool = Form(False),
        reference_image: Optional[UploadFile] = File(None),
    ) -> Dict[str, Any]:
        from sandbox_retouch import run_sandbox_pipeline

        orig_ext = Path(image.filename or "test.jpg").suffix.lower()
        if orig_ext not in (".jpg", ".jpeg", ".png", ".webp", ".arw", ".cr3"):
            raise HTTPException(status_code=400, detail="测试图仅支持 JPG / PNG / WebP / ARW / CR3 格式")

        run_id = f"sb_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        run_dir = active_sandbox_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        src_path = run_dir / f"input{orig_ext}"
        src_path.write_bytes(await image.read())

        ref_path: Optional[Path] = None
        if reference_image is not None and reference_image.filename:
            ref_ext = Path(reference_image.filename).suffix.lower()
            if ref_ext in (".jpg", ".jpeg", ".png", ".webp"):
                ref_path = run_dir / f"uploaded_ref{ref_ext}"
                ref_path.write_bytes(await reference_image.read())

        c = _cfg()
        try:
            res = await asyncio.to_thread(
                run_sandbox_pipeline,
                src_path,
                run_dir,
                preset_code.strip(),
                ref_path,
                bool(send_to_ps),
                c,
                active_skills_dir,
                active_presets_path,
                active_retouch_xmp_dir,
            )
        except Exception as exc:
            logger.exception("单图仿色实验室运行失败")
            raise HTTPException(status_code=500, detail=f"仿色试修失败: {exc}") from exc

        res["run_id"] = run_id
        res["original_url"] = f"/api/retouch/sandbox/files/{run_id}/{res['original_file']}"
        res["result_url"] = f"/api/retouch/sandbox/files/{run_id}/{res['result_file']}"
        res["reference_url"] = f"/api/retouch/sandbox/files/{run_id}/{res['reference_file']}" if res.get("reference_file") else ""
        return res

    @app.get("/api/retouch/sandbox/files/{run_id}/{filename}", dependencies=[Depends(require_session)])
    async def get_sandbox_file(run_id: str, filename: str) -> FileResponse:
        safe_run = Path(run_id).name
        safe_file = Path(filename).name
        p = active_sandbox_dir / safe_run / safe_file
        if not p.exists():
            raise HTTPException(status_code=404, detail="沙盒图片不存在")
        return FileResponse(p)

    # ==================== 面板 5：按功能场景分组的配置中心（全脱敏）与连通性测试 ====================

    @app.get("/api/settings/env", dependencies=[Depends(require_session)])
    async def get_masked_env() -> Dict[str, Any]:
        c = _cfg()
        masked_items: List[Dict[str, Any]] = []
        for k, v in c.items():
            secret = is_secret_key(k)
            masked_items.append(
                {
                    "key": k,
                    "value": mask_secret_value(v) if secret else v,
                    "is_secret": secret,
                    "is_set": bool(v),
                }
            )
        return {"status": "ok", "items": masked_items}

    @app.get("/api/settings/groups", dependencies=[Depends(require_session)])
    async def get_settings_groups() -> Dict[str, Any]:
        c = _cfg()
        groups_out: List[Dict[str, Any]] = []
        for g in SETTINGS_GROUPS_SCHEMA:
            fields_out: List[Dict[str, Any]] = []
            for f in g["fields"]:
                k = f["key"]
                raw_val = c.get(k, "")
                secret = is_secret_key(k)
                fields_out.append({
                    "key": k,
                    "label": f["label"],
                    "desc": f["desc"],
                    "default": f["default"],
                    "value": mask_secret_value(raw_val) if secret else raw_val,
                    "is_secret": secret,
                    "is_set": bool(raw_val),
                    # 当前生效值是否与推荐默认值一致：设置中心据此提示"仍在使用默认值"
                    "is_default": (not raw_val) or str(raw_val).strip() == str(f.get("default") or "").strip(),
                })
            groups_out.append({
                "id": g["id"],
                "title": g["title"],
                "icon": g["icon"],
                "description": g["description"],
                "test_action": g["test_action"],
                "test_label": g["test_label"],
                "fields": fields_out,
            })
        return {"status": "ok", "groups": groups_out}

    @app.put("/api/settings/env", dependencies=[Depends(require_session)])
    async def update_env(payload: EnvSaveRequest) -> Dict[str, Any]:
        applied = write_env_updates(active_env_path, payload.updates)
        return {"status": "ok", "updated_keys": sorted(applied.keys())}

    @app.post("/api/settings/test-nas", dependencies=[Depends(require_session)])
    async def test_nas_conn() -> Dict[str, Any]:
        return {"status": "ok", "nas": probe_nas_status(_cfg())}

    @app.post("/api/settings/test-ai", dependencies=[Depends(require_session)])
    async def test_ai_keys() -> Dict[str, Any]:
        c = _cfg()
        ds_key = (c.get("DEEPSEEK_API_KEY") or "").strip()
        ds_base = (c.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        gm_key = (c.get("GEMINI_API_KEY") or "").strip()
        gm_model = (c.get("GEMINI_RETOUCH_MODEL") or "gemini-2.5-pro").strip()
        # 视觉大脑（仿色）实际调用链：中转站 > GEMINI_API_KEY
        vision_base = (c.get("VISION_BASE_URL") or "").strip().rstrip("/")
        vision_key = (c.get("VISION_API_KEY") or "").strip()
        vision_model = (
            c.get("VISION_MODEL") or c.get("GEMINI_RETOUCH_MODEL") or "gemini-2.5-pro"
        ).strip()
        vision_fallbacks = [
            m.strip() for m in (c.get("VISION_FALLBACK_MODELS") or "").split(",") if m.strip()
        ]

        ds_res: Dict[str, Any] = {"configured": bool(ds_key), "ok": False, "message": "未填写 DEEPSEEK_API_KEY"}
        if ds_key:
            try:
                async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
                    r = await client.get(f"{ds_base}/models", headers={"Authorization": f"Bearer {ds_key}"})
                    ds_res["status_code"] = r.status_code
                    ds_res["ok"] = r.status_code == 200
                    ds_res["message"] = "DeepSeek Key 验证通过" if ds_res["ok"] else f"DeepSeek 返回 HTTP {r.status_code}"
            except Exception as exc:
                ds_res["message"] = f"DeepSeek 连通失败: {exc}"

        # 视觉大脑：优先验证中转站，未配置中转站时才验证 Google 原生接口
        vision_res: Dict[str, Any] = {
            "channel": "relay" if vision_base else "google",
            "base_url": vision_base or "https://generativelanguage.googleapis.com",
            "model": vision_model,
            "fallbacks": vision_fallbacks,
            "configured": bool(vision_key or gm_key),
            "ok": False,
            "message": "",
        }
        if vision_base:
            if not vision_key:
                vision_res["message"] = "已填中转站地址但未填 VISION_API_KEY"
            else:
                try:
                    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                        r = await client.get(
                            f"{vision_base}/models",
                            headers={"Authorization": f"Bearer {vision_key}"},
                        )
                        vision_res["status_code"] = r.status_code
                        vision_res["ok"] = r.status_code == 200
                        if not vision_res["ok"]:
                            vision_res["message"] = f"中转站返回 HTTP {r.status_code}（地址或 Key 有误）"
                        else:
                            # 校验模型名是否在可用列表中，避免"连得上但模型名写错"
                            listed: List[str] = []
                            try:
                                payload = r.json()
                                rows = payload.get("data") if isinstance(payload, dict) else None
                                if isinstance(rows, list):
                                    listed = [str((row or {}).get("id") or "") for row in rows if isinstance(row, dict)]
                            except Exception:
                                listed = []
                            model_ok = (not listed) or (vision_model in listed)
                            vision_res["ok"] = model_ok
                            if listed and not model_ok:
                                vision_res["message"] = (
                                    f"中转站连通，但模型 {vision_model} 不在可用列表中"
                                    f"（共 {len(listed)} 个模型，请核对模型名）"
                                )
                            else:
                                vision_res["message"] = f"中转站连通，模型 {vision_model} 可用"
                except Exception as exc:
                    vision_res["message"] = f"中转站连通失败: {exc}"
        elif gm_key:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.get(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{gm_model}",
                        params={"key": gm_key},
                    )
                    vision_res["status_code"] = r.status_code
                    vision_res["ok"] = r.status_code == 200
                    vision_res["message"] = (
                        f"Gemini ({gm_model}) Key 验证通过" if vision_res["ok"]
                        else f"Gemini 返回 HTTP {r.status_code}"
                    )
            except Exception as exc:
                vision_res["message"] = f"Gemini 连通失败: {exc}"
        else:
            vision_res["message"] = "未配置中转站，也未填写 GEMINI_API_KEY"

        # 兼容旧字段：gemini 现在代表"仿色视觉通道"的真实结果
        return {"status": "ok", "deepseek": ds_res, "vision": vision_res, "gemini": vision_res}

    @app.post("/api/settings/test-im", dependencies=[Depends(require_session)])
    async def test_im_channels() -> Dict[str, Any]:
        return await get_im_channels_status()

    @app.post("/api/settings/test-ps", dependencies=[Depends(require_session)])
    async def test_ps_bridge() -> Dict[str, Any]:
        c = _cfg()
        ps_stat = await get_ps_automation_status()
        if ps_stat.get("remote_worker_online"):
            rw = ps_stat.get("remote_worker") or {}
            ago = rw.get("last_seen_sec_ago", 0)
            return {
                "status": "ok",
                "ps_bridge": {
                    "url": "Windows 随身修图机 (COM 零干预)",
                    "online": True,
                    "message": f"Windows 随身修图机在线（{ago}秒前心跳，本机 COM 自动启停 Photoshop，无需安装任何 PS 插件）",
                },
            }
        if ps_stat.get("com_ready"):
            return {
                "status": "ok",
                "ps_bridge": {
                    "url": ps_stat.get("photoshop_exe") or "Windows COM",
                    "online": True,
                    "message": "本机 Windows COM 零干预模式已就绪（有任务自动启停 Photoshop，无需安装 PS 插件）",
                },
            }
        ps_url = (c.get("PS_UXP_BRIDGE_URL") or "http://127.0.0.1:8765").rstrip("/")
        parsed = urlparse(ps_url)
        tcp_ok = tcp_probe(parsed.hostname or "127.0.0.1", parsed.port or 8765, timeout=1.0)
        return {
            "status": "ok",
            "ps_bridge": {
                "url": ps_url,
                "online": tcp_ok,
                "message": (
                    "Photoshop UXP Bridge 端口可达"
                    if tcp_ok
                    else "当前未检测到在线的 Windows 随身修图机（请在 Windows 笔记本上打开「ZeroStudio随身修图机.exe」启动，无需安装 PS 插件）"
                ),
            },
        }

    @app.post("/api/settings/test-tunnel", dependencies=[Depends(require_session)])
    async def test_tunnel_conn() -> Dict[str, Any]:
        c = _cfg()
        vps_host = (c.get("VPS_HOST") or "").strip()
        bind_port = int(c.get("FRP_BIND_PORT") or 7000)
        booking_port = int(c.get("FRP_PUBLIC_BOOKING_PORT") or 8080)
        studio_port = int(c.get("FRP_PUBLIC_STUDIO_PORT") or 8081)
        public_base = (c.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")

        bind_ok = await asyncio.to_thread(tcp_probe, vps_host, bind_port, 1.5) if vps_host else False
        booking_ok = await asyncio.to_thread(tcp_probe, vps_host, booking_port, 1.5) if vps_host else False
        frpc_ok = is_frpc_running()
        http_ok = False
        if public_base:
            try:
                async with httpx.AsyncClient(timeout=2.5, trust_env=False) as client:
                    r = await client.get(f"{public_base}/api/settings")
                    http_ok = r.status_code == 200
            except Exception:
                http_ok = False

        return {
            "status": "ok",
            "tunnel": {
                "vps_host": vps_host,
                "frpc_running": frpc_ok,
                "frp_bind_port_ok": bind_ok,
                "public_booking_port_ok": booking_ok,
                "public_studio_port_ok": frpc_ok,
                "public_http_ok": http_ok,
            },
        }

    @app.post("/api/settings/reload-services", dependencies=[Depends(require_session)])
    async def reload_managed_services(request: Request) -> Dict[str, Any]:
        """
        安全重载受 studio_supervisor.py 托管的子服务：
        终止指定子服务 PID，由 studio_supervisor.py 自动读取最新 .env 并重新拉起。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        targets = body.get("services") if isinstance(body, dict) and body.get("services") else ["im-gateway", "booking-site"]
        allowed = {"booking-site", "im-gateway", "frpc-tunnel"}
        reloaded: List[str] = []
        skipped: List[str] = []

        # 先刷新当前 control-center 进程的环境变量
        if active_env_path.exists():
            for k, v in read_env_file(active_env_path).items():
                os.environ[k] = v

        if active_pid_file.exists():
            try:
                pids = json.loads(active_pid_file.read_text(encoding="utf-8"))
                import psutil
                for svc in targets:
                    if svc not in allowed:
                        skipped.append(svc)
                        continue
                    pid = pids.get(svc)
                    if pid and int(pid) != os.getpid():
                        try:
                            p = psutil.Process(int(pid))
                            p.terminate()
                            reloaded.append(svc)
                        except Exception:
                            skipped.append(svc)
                    else:
                        skipped.append(svc)
            except Exception as exc:
                logger.warning("读取 studio_pids.json 失败: %s", exc)
        else:
            skipped.extend(targets)

        return {
            "status": "ok",
            "env_reloaded_in_control_center": True,
            "restarted_services": reloaded,
            "skipped_services": skipped,
            "message": "配置已热重载" + (f"，已通知守护进程自动重启: {', '.join(reloaded)}" if reloaded else "（未检测到托管子服务 PID，已立即刷新中控台内存配置）"),
        }

    from nas_routes import register_nas_routes
    register_nas_routes(app, require_session)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("CONTROL_CENTER_PORT", "3080"))
    uvicorn.run(app, host="127.0.0.1", port=port)
